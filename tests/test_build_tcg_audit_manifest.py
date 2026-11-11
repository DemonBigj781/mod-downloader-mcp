import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build-tcg-audit-manifest.py"
SPEC = importlib.util.spec_from_file_location("build_tcg_audit_manifest", SCRIPT)
assert SPEC and SPEC.loader
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def test_discover_download_inventory_maps_numeric_staged_and_override_archives(tmp_path):
    downloads = tmp_path / "downloads"
    batch = downloads / "batch"
    archives = batch / "archives"
    archives.mkdir(parents=True)
    numeric = archives / "10-20.zip"
    numeric.write_bytes(b"numeric")
    write_json(batch / "10-mod.json", {"name": "Numeric Mod", "author": "Author"})
    write_json(batch / "10-files.json", {
        "files": [{
            "file_id": 20,
            "name": "Numeric File",
            "file_name": "numeric-official.zip",
            "version": "2.0",
            "category_name": "MAIN",
            "uploaded_timestamp": 200,
        }],
        "file_updates": [],
    })

    staged = downloads / "staged"
    (staged / "install-review" / "archives").mkdir(parents=True)
    (staged / "Named-1.0.zip").write_bytes(b"staged")
    (staged / "install-review" / "archives" / "Named-1.0.zip").write_bytes(b"staged")
    (staged / "STAGED.md").write_text(
        "| Mod ID | File ID | Archive |\n"
        "| --- | --- | --- |\n"
        "| 30 | 40 | Named-1.0.zip |\n",
        encoding="utf-8",
    )

    custom = downloads / "custom"
    custom.mkdir()
    (custom / "Custom-1.2.zip").write_bytes(b"custom")
    overrides = tmp_path / "overrides.json"
    write_json(overrides, [{
        "path": "custom/Custom-1.2.zip",
        "mod_id": 50,
        "file_id": 60,
        "mod_name": "Custom Mod",
        "version": "1.2",
        "category": "MAIN",
    }])

    manifest = builder.discover_download_inventory(downloads, overrides)

    assert [(item["mod_id"], item["file_id"]) for item in manifest] == [(10, 20), (30, 40), (50, 60)]
    by_identity = {(item["mod_id"], item["file_id"]): item for item in manifest}
    assert by_identity[(10, 20)]["version"] == "2.0"
    assert by_identity[(10, 20)]["mod_name"] == "Numeric Mod"
    assert len(by_identity[(30, 40)]["paths"]) == 2
    assert by_identity[(50, 60)]["version"] == "1.2"


def test_discover_download_inventory_rejects_unknown_named_archives(tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "mystery.zip").write_bytes(b"unknown")
    overrides = tmp_path / "overrides.json"
    write_json(overrides, [])

    with pytest.raises(ValueError, match="Unmapped archive.*mystery.zip"):
        builder.discover_download_inventory(downloads, overrides)


def test_discover_download_inventory_excludes_selected_relative_directories(tmp_path):
    downloads = tmp_path / "downloads"
    historical = downloads / "historical"
    candidates = downloads / "new-candidates"
    historical.mkdir(parents=True)
    candidates.mkdir(parents=True)
    (historical / "10-20.zip").write_bytes(b"historical")
    (candidates / "30-40.zip").write_bytes(b"candidate")
    overrides = tmp_path / "overrides.json"
    write_json(overrides, [])

    manifest = builder.discover_download_inventory(
        downloads,
        overrides,
        excluded_relative_dirs={"new-candidates"},
    )

    assert [(item["mod_id"], item["file_id"]) for item in manifest] == [(10, 20)]


def test_merge_manifests_deduplicates_remote_identity_and_preserves_local_copies(tmp_path):
    first_archive = tmp_path / "first.zip"
    second_archive = tmp_path / "second.zip"
    first_archive.write_bytes(b"same")
    second_archive.write_bytes(b"same")
    digest = builder.sha256_file(first_archive)
    first = tmp_path / "first-manifest.json"
    second = tmp_path / "second-manifest.json"
    write_json(first, [{
        "mod_id": 10,
        "file_id": 20,
        "path": str(first_archive),
        "size": 4,
        "sha256": digest,
        "file_name": "first.zip",
    }])
    write_json(second, [{
        "mod_id": 10,
        "file_id": 20,
        "path": str(second_archive),
        "size": 4,
        "sha256": digest,
        "file_name": "second.zip",
        "version": "2.0",
    }])

    merged = builder.merge_manifests([first, second])

    assert len(merged) == 1
    assert merged[0]["version"] == "2.0"
    assert merged[0]["paths"] == sorted([str(first_archive), str(second_archive)])
    assert merged[0]["source_manifests"] == [str(first.resolve()), str(second.resolve())]


def test_merge_manifests_rejects_conflicting_remote_artifacts(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    write_json(first, [{"mod_id": 10, "file_id": 20, "path": "a.zip", "size": 1, "sha256": "a" * 64}])
    write_json(second, [{"mod_id": 10, "file_id": 20, "path": "b.zip", "size": 1, "sha256": "b" * 64}])

    with pytest.raises(ValueError, match="Conflicting SHA-256.*10:20"):
        builder.merge_manifests([first, second])


def test_build_partial_metadata_marks_snapshot_incomplete():
    metadata = builder.build_partial_metadata([{
        "mod_id": 10,
        "file_id": 20,
        "mod_name": "Fixture Mod",
        "file_name": "fixture.zip",
        "version": "2.0",
        "category": "MAIN",
    }])

    mod, files = metadata[10]
    assert mod == {"name": "Fixture Mod"}
    assert files["_snapshot_complete"] is False
    assert files["files"][0]["file_id"] == 20
