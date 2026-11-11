#!/usr/bin/env python3
"""Audit TCG mod artifacts through the local mod-downloader MCP server.

The lineage audit is metadata-only: it never downloads archives, launches a game,
or writes into a game installation. The legacy metadata/download mode remains
available when ``--manifest`` is omitted.
"""

import argparse
import json
import os
import re
import subprocess
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LEGACY_MOD_IDS = (1592, 1754, 1609, 1492, 1474, 1729, 1632, 1777, 875, 1116, 1431)
GAME = "tcgcardshopsimulator"
ACTIVE_CATEGORIES = {"MAIN", "UPDATE", "OPTIONAL", "MISCELLANEOUS"}
OLD_CATEGORIES = {"OLD_VERSION", "ARCHIVED", "DELETED"}
HIGHLIGHTED_AUTHOR_TERMS = ("curio", "ever")


def call_tools(
    calls: Iterable[tuple[str, str, dict[str, Any]]], *, allow_tool_errors: bool = False
) -> dict[str, Any]:
    calls = list(calls)
    requests = [
        {
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "tcg-update-audit", "version": "2"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]
    for identifier, tool, arguments in calls:
        requests.append(
            {
                "jsonrpc": "2.0",
                "id": identifier,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        )
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("NEXUS_API_KEY", "NEXUSMODS_API_KEY", "nexus_api_key", "nexux_api_key")
    }
    env["NEXUS_MCP_MAX_RESULT_CHARS"] = "2000000"
    result = subprocess.run(
        [str(ROOT / "scripts/nexusmods-mcp")],
        input="".join(json.dumps(request) + "\n" for request in requests),
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("MCP server exited; no credentials or raw stderr displayed")
    return parse_tool_responses(
        result.stdout, {call[0] for call in calls}, allow_tool_errors=allow_tool_errors
    )


def parse_tool_responses(
    stdout: str, expected: set[str], *, allow_tool_errors: bool = False
) -> dict[str, Any]:
    """Parse stdio MCP responses without echoing provider error bodies."""

    responses: dict[str, Any] = {}
    for line in stdout.splitlines():
        message = json.loads(line)
        if message.get("id") == "init" or "id" not in message:
            continue
        if "error" in message:
            code = message["error"].get("code")
            raise RuntimeError(f"MCP request {message.get('id')} failed with code {code}")
        identifier = str(message["id"])
        result = message.get("result") or {}
        content = result.get("content") or []
        text = content[0].get("text") if content and isinstance(content[0], dict) else None
        if not isinstance(text, (str, bytes, bytearray)):
            if allow_tool_errors and result.get("isError"):
                responses[identifier] = {"_tool_error": True}
                continue
            raise RuntimeError(
                f"MCP request {identifier} returned non-JSON tool content "
                f"(isError={bool(result.get('isError'))})"
            )
        try:
            responses[identifier] = json.loads(text)
        except json.JSONDecodeError as exc:
            if allow_tool_errors and result.get("isError"):
                responses[identifier] = {"_tool_error": True}
                continue
            raise RuntimeError(
                f"MCP request {identifier} returned non-JSON tool content "
                f"(isError={bool(result.get('isError'))})"
            ) from exc
    if set(responses) != expected:
        missing = sorted(expected - set(responses))
        raise RuntimeError(f"Incomplete MCP response set; missing {missing}")
    return responses


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc


def _file_summary(record: Mapping[str, Any] | None, file_id: int | None = None) -> dict[str, Any] | None:
    if record is None and file_id is None:
        return None
    source = record or {}
    return {
        "file_id": _integer(source.get("file_id", file_id), "file_id"),
        "name": source.get("name"),
        "file_name": source.get("file_name"),
        "version": source.get("version"),
        "category": source.get("category_name"),
        "uploaded_timestamp": source.get("uploaded_timestamp"),
        "uploaded_time": source.get("uploaded_time"),
        "size_in_bytes": source.get("size_in_bytes"),
    }


def _file_index(files_payload: Mapping[str, Any]) -> dict[int, dict[str, Any]]:
    index: dict[int, dict[str, Any]] = {}
    for record in files_payload.get("files") or []:
        if not isinstance(record, dict) or record.get("file_id") is None:
            continue
        file_id = _integer(record["file_id"], "file_id")
        index[file_id] = record
    return index


def _update_map(
    files_payload: Mapping[str, Any],
) -> tuple[dict[int, int], dict[int, list[int]], list[dict[str, Any]]]:
    file_index = _file_index(files_payload)
    candidates: dict[int, list[tuple[int, int]]] = defaultdict(list)
    anomalies: list[dict[str, Any]] = []
    for record in files_payload.get("file_updates") or []:
        if not isinstance(record, dict):
            continue
        old_id = _integer(record.get("old_file_id"), "old_file_id")
        new_id = _integer(record.get("new_file_id"), "new_file_id")
        uploaded = _integer(record.get("uploaded_timestamp") or 0, "uploaded_timestamp")
        old_uploaded = _integer(file_index.get(old_id, {}).get("uploaded_timestamp") or 0, "uploaded_timestamp")
        new_uploaded = _integer(file_index.get(new_id, {}).get("uploaded_timestamp") or uploaded, "uploaded_timestamp")
        if old_uploaded and max(uploaded, new_uploaded) <= old_uploaded:
            anomalies.append(
                {
                    "old_file_id": old_id,
                    "new_file_id": new_id,
                    "reason": "replacement_not_newer",
                }
            )
            continue
        candidates[old_id].append((uploaded, new_id))

    edges: dict[int, int] = {}
    ambiguities: dict[int, list[int]] = {}
    for old_id, replacements in candidates.items():
        replacements.sort()
        unique_ids = sorted({new_id for _, new_id in replacements})
        if len(unique_ids) > 1:
            ambiguities[old_id] = unique_ids
        edges[old_id] = replacements[-1][1]
    return edges, ambiguities, anomalies


def follow_lineage(start_file_id: int, updates: Mapping[int, int]) -> tuple[list[int], str | None]:
    """Follow Nexus file_updates until a terminal file or a malformed cycle."""

    lineage = [start_file_id]
    seen = {start_file_id}
    while lineage[-1] in updates:
        next_file_id = updates[lineage[-1]]
        lineage.append(next_file_id)
        if next_file_id in seen:
            return lineage, "cycle"
        seen.add(next_file_id)
    return lineage, None


def _is_active_file(record: Mapping[str, Any]) -> bool:
    category = str(record.get("category_name") or "").upper()
    return category in ACTIVE_CATEGORIES


def audit_mod(
    *,
    mod_id: int,
    selected_records: list[dict[str, Any]],
    previous_mod: Mapping[str, Any],
    previous_files: Mapping[str, Any],
    current_mod: Mapping[str, Any],
    current_files: Mapping[str, Any],
) -> dict[str, Any]:
    """Classify every selected archive on one multi-file Nexus mod page."""

    current_index = _file_index(current_files)
    previous_index = _file_index(previous_files)
    updates, ambiguities, metadata_anomalies = _update_map(current_files)
    file_metadata_available = not bool(current_files.get("_tool_error"))
    mod_metadata_available = not bool(current_mod.get("_tool_error"))
    artifacts: list[dict[str, Any]] = []
    selected_lineage_ids: set[int] = set()
    selected_ids = {_integer(item["file_id"], "file_id") for item in selected_records}

    for selected in sorted(selected_records, key=lambda item: _integer(item["file_id"], "file_id")):
        file_id = _integer(selected["file_id"], "file_id")
        lineage, lineage_error = follow_lineage(file_id, updates)
        selected_lineage_ids.update(lineage)
        terminal_id = lineage[-1]
        current_record = current_index.get(file_id)
        terminal_record = current_index.get(terminal_id)

        if not file_metadata_available:
            status = "metadata_unavailable"
        elif lineage_error:
            status = "lineage_error"
        elif any(item in ambiguities for item in lineage[:-1]):
            status = "lineage_ambiguous"
        elif len(lineage) > 1 and terminal_record is not None and terminal_id in selected_ids:
            status = "superseded_in_manifest"
        elif len(lineage) > 1 and terminal_record is not None:
            status = "update_available"
        elif len(lineage) > 1:
            status = "replacement_missing"
        elif current_record is None:
            status = "missing_unmapped"
        elif str(current_record.get("category_name") or "").upper() in OLD_CATEGORIES:
            status = "unmapped_old"
        else:
            status = "current"

        artifact = {
            "file_id": file_id,
            "file_name": selected.get("file_name"),
            "version": selected.get("version"),
            "category": selected.get("category"),
            "size": selected.get("size"),
            "sha256": selected.get("sha256"),
            "path": selected.get("path"),
            "status": status,
            "lineage": lineage,
            "lineage_error": lineage_error,
            "current_file": _file_summary(current_record),
            "replacement": _file_summary(terminal_record, terminal_id) if len(lineage) > 1 else None,
        }
        ambiguous_steps = {str(item): ambiguities[item] for item in lineage if item in ambiguities}
        if ambiguous_steps:
            artifact["ambiguous_replacements"] = ambiguous_steps
        artifacts.append(artifact)

    sibling_comparison_available = bool(
        previous_files.get("_snapshot_complete", True) and file_metadata_available
    )
    new_sibling_files = []
    if sibling_comparison_available:
        previous_ids = set(previous_index)
        for file_id, record in sorted(current_index.items()):
            if file_id in previous_ids or file_id in selected_lineage_ids or not _is_active_file(record):
                continue
            new_sibling_files.append(_file_summary(record))

    summary = dict(sorted(Counter(item["status"] for item in artifacts).items()))
    previous_updated = previous_mod.get("updated_timestamp")
    current_updated = current_mod.get("updated_timestamp") if mod_metadata_available else None
    page_changed = (
        previous_updated != current_updated
        if mod_metadata_available and previous_updated is not None
        else None
    )
    artifact_changed = any(item["status"] != "current" for item in artifacts) or bool(new_sibling_files)
    metadata_errors = []
    if not mod_metadata_available:
        metadata_errors.append("mod_metadata_unavailable")
    if not file_metadata_available:
        metadata_errors.append("file_metadata_unavailable")

    return {
        "mod_id": mod_id,
        "name": current_mod.get("name") or previous_mod.get("name") or selected_records[0].get("mod_name"),
        "author": (
            current_mod.get("author")
            or current_mod.get("uploaded_by")
            or previous_mod.get("author")
            or previous_mod.get("uploaded_by")
            or "Unknown"
        ),
        "version": current_mod.get("version") if mod_metadata_available else None,
        "previous_updated_timestamp": previous_updated,
        "current_updated_timestamp": current_updated,
        "current_updated_time": current_mod.get("updated_time"),
        "page_changed": page_changed,
        "page_metadata_only": bool(page_changed and not artifact_changed),
        "metadata_errors": metadata_errors,
        "summary": summary,
        "artifacts": artifacts,
        "new_sibling_comparison_available": sibling_comparison_available,
        "new_sibling_files": new_sibling_files,
        "metadata_anomalies": metadata_anomalies,
    }


def build_audit(
    manifest: list[dict[str, Any]],
    previous_metadata: Mapping[int, tuple[Mapping[str, Any], Mapping[str, Any]]],
    current_metadata: Mapping[int, tuple[Mapping[str, Any], Mapping[str, Any]]],
    *,
    game: str,
) -> dict[str, Any]:
    selected_by_mod: dict[int, list[dict[str, Any]]] = defaultdict(list)
    identities: set[tuple[int, int]] = set()
    for record in manifest:
        mod_id = _integer(record.get("mod_id"), "mod_id")
        file_id = _integer(record.get("file_id"), "file_id")
        identity = (mod_id, file_id)
        if identity in identities:
            raise ValueError(f"Duplicate manifest artifact {mod_id}:{file_id}")
        identities.add(identity)
        selected_by_mod[mod_id].append(record)

    missing_previous = sorted(set(selected_by_mod) - set(previous_metadata))
    missing_current = sorted(set(selected_by_mod) - set(current_metadata))
    if missing_previous or missing_current:
        raise ValueError(
            f"Missing metadata; previous={missing_previous or 'none'}, current={missing_current or 'none'}"
        )

    mods = []
    for mod_id in sorted(selected_by_mod):
        previous_mod, previous_files = previous_metadata[mod_id]
        current_mod, current_files = current_metadata[mod_id]
        mods.append(
            audit_mod(
                mod_id=mod_id,
                selected_records=selected_by_mod[mod_id],
                previous_mod=previous_mod,
                previous_files=previous_files,
                current_mod=current_mod,
                current_files=current_files,
            )
        )

    summary: Counter[str] = Counter()
    for mod in mods:
        summary.update(mod["summary"])
    author_variants: dict[str, Counter[str]] = defaultdict(Counter)
    for mod in mods:
        author = str(mod["author"])
        author_variants[author.casefold()][author] += 1
    canonical_authors = {
        key: min(variants.items(), key=lambda item: (-item[1], item[0]))[0]
        for key, variants in author_variants.items()
    }
    for mod in mods:
        mod["author_group"] = canonical_authors[str(mod["author"]).casefold()]
    authors = sorted(canonical_authors.values(), key=str.casefold)
    highlighted = [
        author
        for author in authors
        if any(term in author.casefold() for term in HIGHLIGHTED_AUTHOR_TERMS)
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "game": game,
        "manifest_artifact_count": len(manifest),
        "mod_count": len(mods),
        "summary": dict(sorted(summary.items())),
        "highlighted_authors": highlighted,
        "mods": mods,
    }


def build_actionable_replacements(
    result: Mapping[str, Any],
    current_metadata: Mapping[int, tuple[Mapping[str, Any], Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    """Build one verified download record for every unique terminal replacement."""

    replacements: dict[tuple[int, int], dict[str, Any]] = {}
    for mod in result.get("mods") or []:
        mod_id = _integer(mod.get("mod_id"), "mod_id")
        if mod_id not in current_metadata:
            raise ValueError(f"Missing current metadata for Nexus mod {mod_id}")
        _mod_payload, files_payload = current_metadata[mod_id]
        file_index = _file_index(files_payload)
        for artifact in mod.get("artifacts") or []:
            if artifact.get("status") != "update_available":
                continue
            replacement_summary = artifact.get("replacement") or {}
            replacement_id = _integer(replacement_summary.get("file_id"), "replacement.file_id")
            file_record = file_index.get(replacement_id)
            if file_record is None:
                raise ValueError(f"Missing replacement metadata for Nexus artifact {mod_id}:{replacement_id}")
            extension = str(file_record.get("detected_file_extension") or "").casefold()
            if not extension:
                extension = Path(str(file_record.get("file_name") or "")).suffix.lstrip(".").casefold()
            if extension not in {"zip", "rar", "7z"}:
                extension = "download"
            virus_scan_url = str(file_record.get("external_virus_scan_url") or "")
            hash_match = re.search(r"/file/([0-9a-fA-F]{64})(?:/|$)", virus_scan_url)
            raw_size = file_record.get("size_in_bytes")
            key = (mod_id, replacement_id)
            record = replacements.setdefault(
                key,
                {
                    "game_domain_name": result.get("game"),
                    "mod_id": mod_id,
                    "mod_name": mod.get("name"),
                    "file_id": replacement_id,
                    "file_name": file_record.get("file_name"),
                    "version": file_record.get("version"),
                    "category": file_record.get("category_name"),
                    "size_in_bytes": (
                        None if raw_size in (None, "", 0, "0") else _integer(raw_size, "size_in_bytes")
                    ),
                    "official_sha256": hash_match.group(1).lower() if hash_match else None,
                    "archive_name": f"{mod_id}-{replacement_id}.{extension}",
                    "replaces_file_ids": [],
                },
            )
            record["replaces_file_ids"].append(_integer(artifact.get("file_id"), "file_id"))

    for record in replacements.values():
        record["replaces_file_ids"] = sorted(set(record["replaces_file_ids"]))
    return [replacements[key] for key in sorted(replacements)]


def _format_counts(counts: Mapping[str, int]) -> str:
    return ", ".join(f"{name}={count}" for name, count in sorted(counts.items())) or "none"


def render_report(result: Mapping[str, Any]) -> str:
    lines = [
        "# TCG Nexus Artifact Update Audit",
        "",
        f"Generated: {result['generated_at']}",
        f"Game domain: `{result['game']}`",
        f"Coverage: {result['manifest_artifact_count']} selected artifacts across {result['mod_count']} mod pages.",
        f"Artifact status: {_format_counts(result['summary'])}.",
    ]
    highlighted = result.get("highlighted_authors") or []
    if highlighted:
        lines.extend(["", f"Priority authors: {', '.join(highlighted)}."])

    mods = list(result.get("mods") or [])
    highlighted_set = {str(item).casefold() for item in highlighted}
    mods.sort(
        key=lambda mod: (
            str(mod.get("author_group", mod.get("author", ""))).casefold() not in highlighted_set,
            str(mod.get("author_group", mod.get("author", ""))).casefold(),
            str(mod.get("name", "")).casefold(),
            int(mod.get("mod_id", 0)),
        )
    )
    current_author = None
    for mod in mods:
        author = str(mod.get("author_group") or mod.get("author") or "Unknown")
        author_key = author.casefold()
        if author_key != current_author:
            lines.extend(["", f"## {author}"])
            current_author = author_key
        page_note = "page metadata only" if mod.get("page_metadata_only") else (
            "page comparison unavailable" if mod.get("page_changed") is None else (
                "page changed" if mod.get("page_changed") else "page unchanged"
            )
        )
        lines.extend(
            [
                "",
                f"### {mod['name']} (Nexus {mod['mod_id']})",
                "",
                f"- {_format_counts(mod['summary'])}; {page_note}.",
            ]
        )
        for artifact in mod.get("artifacts") or []:
            if artifact.get("status") == "current":
                continue
            lineage = " -> ".join(str(item) for item in artifact.get("lineage") or [])
            replacement = artifact.get("replacement") or {}
            replacement_note = ""
            if replacement:
                replacement_note = (
                    f"; replacement {replacement.get('file_id')}"
                    f" ({replacement.get('version') or 'unknown version'})"
                )
            lines.append(
                f"- `{artifact.get('file_name')}` [{artifact['status']}]: {lineage}{replacement_note}."
            )
        for sibling in mod.get("new_sibling_files") or []:
            lines.append(
                f"- New sibling file {sibling.get('file_id')}: `{sibling.get('name') or sibling.get('file_name')}` "
                f"({sibling.get('category') or 'unknown category'}, {sibling.get('version') or 'unknown version'})."
            )
        if not mod.get("new_sibling_comparison_available", True):
            lines.append("- New-sibling comparison unavailable: the historical metadata snapshot is partial.")
        for metadata_error in mod.get("metadata_errors") or []:
            lines.append(f"- Metadata error: {metadata_error}.")
        for anomaly in mod.get("metadata_anomalies") or []:
            lines.append(
                f"- Nexus metadata anomaly ignored: {anomaly.get('old_file_id')} -> "
                f"{anomaly.get('new_file_id')} ({anomaly.get('reason')})."
            )
    lines.append("")
    return "\n".join(lines)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_outputs(result: Mapping[str, Any], output: Path) -> tuple[Path, Path]:
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "update-audit.json"
    report_path = output / "update-audit.md"
    _write_json(json_path, result)
    report_path.write_text(render_report(result), encoding="utf-8")
    return json_path, report_path


def load_manifest(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("Manifest must be a JSON list of objects")
    return value


def load_metadata(
    directory: Path, mod_ids: Iterable[int]
) -> dict[int, tuple[dict[str, Any], dict[str, Any]]]:
    metadata = {}
    for mod_id in sorted(set(mod_ids)):
        mod_path = directory / f"mod-{mod_id}.json"
        files_path = directory / f"files-{mod_id}.json"
        if not mod_path.is_file() or not files_path.is_file():
            raise FileNotFoundError(f"Missing metadata for Nexus mod {mod_id} under {directory}")
        metadata[mod_id] = (
            json.loads(mod_path.read_text(encoding="utf-8")),
            json.loads(files_path.read_text(encoding="utf-8")),
        )
    return metadata


def fetch_current_metadata(
    mod_ids: Iterable[int], output: Path, *, game: str
) -> dict[int, tuple[dict[str, Any], dict[str, Any]]]:
    output.mkdir(parents=True, exist_ok=True)
    calls = []
    for mod_id in sorted(set(mod_ids)):
        arguments = {"game_domain_name": game, "mod_id": mod_id}
        calls.extend(
            [
                (f"{mod_id}-mod", "get_mod", arguments),
                (f"{mod_id}-files", "list_mod_files", arguments),
            ]
        )
    results = call_tools(calls, allow_tool_errors=True)
    for mod_id in sorted(set(mod_ids)):
        _write_json(output / f"mod-{mod_id}.json", results[f"{mod_id}-mod"])
        _write_json(output / f"files-{mod_id}.json", results[f"{mod_id}-files"])
    return load_metadata(output, mod_ids)


def inspect_manifest_archives(manifest_path: Path, manifest: list[dict[str, Any]]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    total_bytes = 0
    details = []
    archive_copy_count = 0
    for record in manifest:
        raw_paths = list(record.get("paths") or [])
        if not raw_paths:
            raw_paths = [record.get("path") or ""]
        for raw_path in dict.fromkeys(str(path) for path in raw_paths):
            archive_copy_count += 1
            relative = Path(raw_path)
            candidates = [manifest_path.parent / relative, manifest_path.parent / "downloads" / relative]
            archive = next((candidate for candidate in candidates if candidate.is_file()), None)
            expected_size = _integer(record.get("size") or 0, "size")
            if archive is None:
                status = "missing"
                actual_size = None
            else:
                actual_size = archive.stat().st_size
                status = "present" if actual_size == expected_size else "size_mismatch"
                total_bytes += actual_size
            counts[status] += 1
            if status != "present":
                details.append(
                    {
                        "mod_id": record.get("mod_id"),
                        "file_id": record.get("file_id"),
                        "path": str(relative),
                        "status": status,
                        "expected_size": expected_size,
                        "actual_size": actual_size,
                    }
                )
    return {
        "summary": dict(sorted(counts.items())),
        "present_bytes": total_bytes,
        "problems": details,
        "artifact_count": len(manifest),
        "archive_copy_count": archive_copy_count,
    }


def run_lineage_audit(args: argparse.Namespace) -> None:
    manifest_path = args.manifest.resolve()
    manifest = load_manifest(manifest_path)
    mod_ids = sorted({_integer(item.get("mod_id"), "mod_id") for item in manifest})
    previous_directory = (
        args.previous_metadata.resolve()
        if args.previous_metadata
        else manifest_path.parent / "previous-metadata"
    )
    previous = load_metadata(previous_directory, mod_ids)
    if args.current_metadata:
        current = load_metadata(args.current_metadata.resolve(), mod_ids)
    else:
        current = fetch_current_metadata(mod_ids, args.output.resolve() / "current-metadata", game=args.game)
    result = build_audit(manifest, previous, current, game=args.game)
    result["manifest"] = str(manifest_path)
    result["previous_metadata"] = str(previous_directory)
    result["archive_validation"] = inspect_manifest_archives(manifest_path, manifest)
    json_path, report_path = write_outputs(result, args.output.resolve())
    replacements = build_actionable_replacements(result, current)
    replacements_path = args.output.resolve() / "actionable-replacements.json"
    _write_json(replacements_path, replacements)
    print(f"Audited {result['manifest_artifact_count']} artifacts across {result['mod_count']} mod pages")
    print(f"Artifact status: {_format_counts(result['summary'])}")
    print(f"Archive validation: {_format_counts(result['archive_validation']['summary'])}")
    print(f"Actionable replacements: {len(replacements)}")
    print(f"JSON: {json_path}")
    print(f"Report: {report_path}")
    print(f"Replacements: {replacements_path}")


def run_legacy_mode(args: argparse.Namespace) -> None:
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.download:
        calls = []
        for pair in args.download:
            mod_id, file_id = map(int, pair.split(":"))
            if mod_id not in LEGACY_MOD_IDS or file_id <= 0:
                raise ValueError("Download is outside the requested legacy mod set")
            calls.append(
                (
                    pair,
                    "download_mod_file",
                    {
                        "game_domain_name": args.game,
                        "mod_id": mod_id,
                        "file_id": file_id,
                        "output_dir": str(output / "archives"),
                        "filename": f"{mod_id}-{file_id}.zip",
                    },
                )
            )
        results = call_tools(calls)
        with (output / "downloads.jsonl").open("a", encoding="utf-8") as stream:
            for pair, result in results.items():
                stream.write(json.dumps({"id": pair, **result}) + "\n")
                print(pair, result["bytes"], result["downloaded"])
        return

    calls = []
    for mod_id in LEGACY_MOD_IDS:
        arguments = {"game_domain_name": args.game, "mod_id": mod_id}
        calls.extend(
            [
                (f"{mod_id}-mod", "get_mod", arguments),
                (f"{mod_id}-files", "list_mod_files", arguments),
            ]
        )
    results = call_tools(calls)
    for identifier, data in results.items():
        _write_json(output / f"{identifier}.json", data)
    for mod_id in LEGACY_MOD_IDS:
        mod = results[f"{mod_id}-mod"]
        print(f"{mod_id}: {mod.get('name')} | listed version {mod.get('version')}")
        for record in results[f"{mod_id}-files"]["files"]:
            if record.get("category_name") in ACTIVE_CATEGORIES:
                print(
                    " ",
                    json.dumps(
                        {
                            key: record.get(key)
                            for key in (
                                "file_id",
                                "name",
                                "version",
                                "category_name",
                                "file_name",
                                "size_in_bytes",
                            )
                        },
                        ensure_ascii=False,
                    ),
                )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--game", default=GAME)
    parser.add_argument("--manifest", type=Path, help="Audit every artifact in this download manifest")
    parser.add_argument(
        "--previous-metadata",
        type=Path,
        help="Historical metadata directory (default: previous-metadata beside the manifest)",
    )
    parser.add_argument(
        "--current-metadata",
        type=Path,
        help="Use an already-fetched metadata directory instead of calling the MCP server",
    )
    parser.add_argument("--download", nargs="+", metavar="MOD:FILE")
    args = parser.parse_args()
    if args.manifest and args.download:
        parser.error("--download cannot be combined with --manifest")
    if args.current_metadata and not args.manifest:
        parser.error("--current-metadata requires --manifest")
    return args


def main() -> None:
    args = parse_args()
    if args.manifest:
        run_lineage_audit(args)
    else:
        run_legacy_mode(args)


if __name__ == "__main__":
    main()
