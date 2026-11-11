#!/usr/bin/env python3
"""Build deterministic TCG Nexus artifact manifests from local archive inventories."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

ARCHIVE_PATTERN = re.compile(
    r"(?P<mod_id>[1-9][0-9]*)-(?P<file_id>[1-9][0-9]*)\.(?:zip|rar|7z)",
    re.IGNORECASE,
)
ARCHIVE_SUFFIXES = {".zip", ".rar", ".7z"}
DEFAULT_OVERRIDES = Path(__file__).with_name("tcg-audit-legacy-overrides.json")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_overrides(path: Path) -> dict[str, dict[str, Any]]:
    value = load_json(path)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("Override file must contain a JSON list of objects")
    overrides = {}
    for item in value:
        relative = str(item.get("path") or "")
        if not relative or relative in overrides:
            raise ValueError(f"Invalid or duplicate override path: {relative!r}")
        overrides[relative] = dict(item)
    return overrides


def staged_archive_mappings(root: Path) -> dict[str, tuple[int, int]]:
    mappings: dict[str, tuple[int, int]] = {}
    for staged in root.rglob("STAGED.md"):
        for line in staged.read_text(encoding="utf-8").splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) < 3 or not cells[0].isdigit() or not cells[1].isdigit():
                continue
            archive_name = cells[2]
            identity = (int(cells[0]), int(cells[1]))
            previous = mappings.get(archive_name)
            if previous is not None and previous != identity:
                raise ValueError(f"Conflicting STAGED.md mapping for {archive_name}")
            mappings[archive_name] = identity
    return mappings


def find_nearby_metadata(
    archive: Path, root: Path, mod_id: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    root = root.resolve()
    directory = archive.parent.resolve()
    while True:
        mod_path = directory / f"{mod_id}-mod.json"
        files_path = directory / f"{mod_id}-files.json"
        if mod_path.is_file() and files_path.is_file():
            return load_json(mod_path), load_json(files_path)
        if directory == root or root not in directory.parents:
            return {}, {}
        directory = directory.parent


def metadata_file(files_payload: Mapping[str, Any], file_id: int) -> dict[str, Any]:
    for item in files_payload.get("files") or []:
        if isinstance(item, dict) and int(item.get("file_id") or 0) == file_id:
            return item
    return {}


def merge_artifact_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[int, int], dict[str, Any]] = {}
    for source in records:
        mod_id = int(source["mod_id"])
        file_id = int(source["file_id"])
        identity = (mod_id, file_id)
        record = merged.setdefault(identity, {"mod_id": mod_id, "file_id": file_id})
        incoming_hash = str(source.get("sha256") or "")
        existing_hash = str(record.get("sha256") or "")
        if incoming_hash and existing_hash and incoming_hash != existing_hash:
            raise ValueError(f"Conflicting SHA-256 for Nexus artifact {mod_id}:{file_id}")
        incoming_size = source.get("size")
        existing_size = record.get("size")
        if incoming_size is not None and existing_size is not None and int(incoming_size) != int(existing_size):
            raise ValueError(f"Conflicting size for Nexus artifact {mod_id}:{file_id}")
        for field in (
            "mod_name",
            "author",
            "file_name",
            "version",
            "category",
            "uploaded_timestamp",
            "size",
            "sha256",
        ):
            if record.get(field) in (None, "") and source.get(field) not in (None, ""):
                record[field] = source[field]
        paths = {
            str(Path(path).expanduser().resolve())
            for path in [
                *(record.get("paths") or []),
                *(source.get("paths") or []),
                record.get("path"),
                source.get("path"),
            ]
            if path
        }
        record["paths"] = sorted(paths)
        if paths:
            record["path"] = min(paths)
        source_manifests = {
            str(path)
            for path in [*(record.get("source_manifests") or []), *(source.get("source_manifests") or [])]
            if path
        }
        if source_manifests:
            record["source_manifests"] = sorted(source_manifests)
    return [merged[identity] for identity in sorted(merged)]


def normalize_excluded_relative_dirs(values: Iterable[str]) -> set[str]:
    excluded = set()
    for value in values:
        path = Path(value)
        if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError(f"Excluded directory must be a safe relative path: {value!r}")
        excluded.add(path.as_posix().rstrip("/"))
    return excluded


def path_is_excluded(relative: str, excluded_relative_dirs: set[str]) -> bool:
    return any(relative == directory or relative.startswith(directory + "/") for directory in excluded_relative_dirs)


def discover_download_inventory(
    downloads_root: Path,
    overrides_path: Path,
    excluded_relative_dirs: Iterable[str] = (),
) -> list[dict[str, Any]]:
    root = downloads_root.expanduser().resolve()
    overrides = load_overrides(overrides_path.expanduser().resolve())
    excluded = normalize_excluded_relative_dirs(excluded_relative_dirs)
    staged = staged_archive_mappings(root)
    records = []
    mapped_overrides: set[str] = set()
    for archive in sorted(
        path for path in root.rglob("*") if path.is_file() and path.suffix.casefold() in ARCHIVE_SUFFIXES
    ):
        relative = archive.relative_to(root).as_posix()
        if path_is_excluded(relative, excluded):
            continue
        override = overrides.get(relative, {})
        match = ARCHIVE_PATTERN.fullmatch(archive.name)
        if override:
            mod_id = int(override["mod_id"])
            file_id = int(override["file_id"])
            mapped_overrides.add(relative)
        elif match:
            mod_id = int(match.group("mod_id"))
            file_id = int(match.group("file_id"))
        elif archive.name in staged:
            mod_id, file_id = staged[archive.name]
        else:
            raise ValueError(f"Unmapped archive: {relative}")

        mod_payload, files_payload = find_nearby_metadata(archive, root, mod_id)
        file_payload = metadata_file(files_payload, file_id)
        records.append(
            {
                "mod_id": mod_id,
                "mod_name": override.get("mod_name") or mod_payload.get("name"),
                "author": override.get("author") or mod_payload.get("author") or mod_payload.get("uploaded_by"),
                "file_id": file_id,
                "file_name": override.get("file_name") or file_payload.get("file_name") or archive.name,
                "version": override.get("version") or file_payload.get("version"),
                "category": override.get("category") or file_payload.get("category_name"),
                "uploaded_timestamp": override.get("uploaded_timestamp") or file_payload.get("uploaded_timestamp"),
                "size": archive.stat().st_size,
                "sha256": sha256_file(archive),
                "path": str(archive),
                "paths": [str(archive)],
            }
        )
    required_overrides = {
        relative for relative in overrides if not path_is_excluded(relative, excluded)
    }
    missing_overrides = sorted(required_overrides - mapped_overrides)
    if missing_overrides:
        raise FileNotFoundError(f"Override archives not found: {missing_overrides}")
    return merge_artifact_records(records)


def resolve_manifest_paths(manifest_path: Path, record: Mapping[str, Any]) -> list[str]:
    raw_paths = list(record.get("paths") or [])
    if record.get("path"):
        raw_paths.append(record["path"])
    resolved = set()
    for raw in raw_paths:
        path = Path(str(raw)).expanduser()
        if path.is_absolute():
            resolved.add(str(path.resolve()))
            continue
        candidates = [manifest_path.parent / path, manifest_path.parent / "downloads" / path]
        selected = next((candidate for candidate in candidates if candidate.is_file()), candidates[0])
        resolved.add(str(selected.resolve()))
    return sorted(resolved)


def merge_manifests(manifest_paths: Iterable[Path]) -> list[dict[str, Any]]:
    records = []
    for raw_path in manifest_paths:
        manifest_path = raw_path.expanduser().resolve()
        value = load_json(manifest_path)
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise ValueError(f"Manifest must be a JSON list of objects: {manifest_path}")
        for item in value:
            record = dict(item)
            paths = resolve_manifest_paths(manifest_path, record)
            record["paths"] = paths
            record["path"] = paths[0] if paths else None
            record["source_manifests"] = [str(manifest_path)]
            records.append(record)
    return merge_artifact_records(records)


def build_partial_metadata(
    manifest: Iterable[Mapping[str, Any]],
) -> dict[int, tuple[dict[str, Any], dict[str, Any]]]:
    by_mod: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for record in manifest:
        by_mod[int(record["mod_id"])].append(record)
    result = {}
    for mod_id, records in sorted(by_mod.items()):
        first = records[0]
        mod = {key: first[key] for key in ("mod_name", "author") if first.get(key)}
        if "mod_name" in mod:
            mod["name"] = mod.pop("mod_name")
        files = {
            "_snapshot_complete": False,
            "files": [
                {
                    "file_id": int(record["file_id"]),
                    "name": record.get("file_name"),
                    "file_name": record.get("file_name"),
                    "version": record.get("version"),
                    "category_name": record.get("category"),
                    "uploaded_timestamp": record.get("uploaded_timestamp"),
                    "size_in_bytes": record.get("size"),
                }
                for record in sorted(records, key=lambda item: int(item["file_id"]))
            ],
            "file_updates": [],
        }
        result[mod_id] = (mod, files)
    return result


def write_inventory(output: Path, manifest: list[dict[str, Any]]) -> None:
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "download-manifest.json", manifest)
    metadata = build_partial_metadata(manifest)
    for mod_id, (mod, files) in metadata.items():
        write_json(output / "previous-metadata" / f"mod-{mod_id}.json", mod)
        write_json(output / "previous-metadata" / f"files-{mod_id}.json", files)
    write_json(
        output / "inventory-summary.json",
        {
            "artifact_count": len(manifest),
            "archive_copy_count": sum(len(record.get("paths") or []) for record in manifest),
            "mod_count": len(metadata),
            "total_bytes_unique_artifacts": sum(int(record.get("size") or 0) for record in manifest),
        },
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    discover = subparsers.add_parser("discover", help="Build a manifest from a download archive tree")
    discover.add_argument("downloads_root", type=Path)
    discover.add_argument("output", type=Path)
    discover.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES)
    discover.add_argument(
        "--exclude-dir",
        action="append",
        default=[],
        help="Relative directory under downloads_root to exclude; repeat as needed",
    )
    merge = subparsers.add_parser("merge", help="Merge manifests by Nexus mod/file identity")
    merge.add_argument("output", type=Path)
    merge.add_argument("manifests", type=Path, nargs="+")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "discover":
        manifest = discover_download_inventory(
            args.downloads_root,
            args.overrides,
            excluded_relative_dirs=args.exclude_dir,
        )
    else:
        manifest = merge_manifests(args.manifests)
    write_inventory(args.output, manifest)
    copies = sum(len(record.get("paths") or []) for record in manifest)
    print(f"Wrote {len(manifest)} Nexus artifact identities with {copies} local archive copies")
    print(f"Manifest: {args.output.expanduser().resolve() / 'download-manifest.json'}")


if __name__ == "__main__":
    main()
