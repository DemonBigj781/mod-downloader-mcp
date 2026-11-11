import argparse
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "tcg-update-audit.py"
SPEC = importlib.util.spec_from_file_location("tcg_update_audit", SCRIPT)
assert SPEC and SPEC.loader
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def file_record(file_id, name, category="MAIN", version="1.0"):
    return {
        "file_id": file_id,
        "name": name,
        "file_name": f"{name}.zip",
        "category_name": category,
        "version": version,
        "uploaded_timestamp": file_id * 10,
        "uploaded_time": f"2026-01-{file_id % 28 + 1:02d}T00:00:00.000+00:00",
        "size_in_bytes": file_id * 100,
    }


def update(old_file_id, new_file_id):
    return {
        "old_file_id": old_file_id,
        "new_file_id": new_file_id,
        "uploaded_timestamp": new_file_id * 10,
        "uploaded_time": f"2026-02-{new_file_id % 28 + 1:02d}T00:00:00.000+00:00",
    }


def selected(file_id, name="Selected"):
    return {
        "mod_id": 10,
        "mod_name": "Fixture Mod",
        "file_id": file_id,
        "file_name": f"{name}.zip",
        "version": "1.0",
        "category": "MAIN",
        "size": file_id * 100,
        "sha256": "a" * 64,
        "path": f"{name}.zip",
    }


def test_audit_mod_follows_explicit_replacement_chain():
    result = audit.audit_mod(
        mod_id=10,
        selected_records=[selected(1)],
        previous_mod={"updated_timestamp": 10},
        previous_files={"files": [file_record(1, "Selected")], "file_updates": []},
        current_mod={"name": "Fixture Mod", "author": "Curio", "updated_timestamp": 30},
        current_files={
            "files": [
                file_record(1, "Selected", "OLD_VERSION"),
                file_record(2, "Selected", "OLD_VERSION", "2.0"),
                file_record(3, "Selected", "MAIN", "3.0"),
            ],
            "file_updates": [update(1, 2), update(2, 3)],
        },
    )

    artifact = result["artifacts"][0]
    assert artifact["status"] == "update_available"
    assert artifact["lineage"] == [1, 2, 3]
    assert artifact["replacement"]["file_id"] == 3
    assert artifact["replacement"]["version"] == "3.0"
    assert result["summary"] == {"update_available": 1}


def test_audit_mod_does_not_request_replacement_already_in_manifest():
    result = audit.audit_mod(
        mod_id=10,
        selected_records=[selected(1, "Old"), selected(3, "Current")],
        previous_mod={"updated_timestamp": 10},
        previous_files={"files": [file_record(1, "Old"), file_record(3, "Current")], "file_updates": []},
        current_mod={"name": "Fixture Mod", "author": "Curio", "updated_timestamp": 30},
        current_files={
            "files": [file_record(1, "Old", "OLD_VERSION"), file_record(3, "Current", "MAIN", "3.0")],
            "file_updates": [update(1, 3)],
        },
    )

    statuses = {item["file_id"]: item["status"] for item in result["artifacts"]}
    assert statuses == {1: "superseded_in_manifest", 3: "current"}


def test_audit_mod_ignores_chronologically_backward_update_edges():
    old = file_record(9, "Current", "MAIN", "3.0")
    old["uploaded_timestamp"] = 300
    archived = file_record(4, "Archived", "ARCHIVED", "2.0")
    archived["uploaded_timestamp"] = 200
    bad_update = update(9, 4)
    bad_update["uploaded_timestamp"] = 200

    result = audit.audit_mod(
        mod_id=10,
        selected_records=[selected(9, "Current")],
        previous_mod={"updated_timestamp": 300},
        previous_files={"files": [old, archived], "file_updates": []},
        current_mod={"name": "Fixture Mod", "author": "Curio", "updated_timestamp": 300},
        current_files={"files": [old, archived], "file_updates": [bad_update]},
    )

    assert result["artifacts"][0]["status"] == "current"
    assert result["metadata_anomalies"] == [{
        "old_file_id": 9,
        "new_file_id": 4,
        "reason": "replacement_not_newer",
    }]


def test_audit_mod_distinguishes_current_unmapped_and_missing_artifacts():
    result = audit.audit_mod(
        mod_id=10,
        selected_records=[selected(1, "Current"), selected(2, "Old"), selected(3, "Missing")],
        previous_mod={"updated_timestamp": 10},
        previous_files={
            "files": [file_record(1, "Current"), file_record(2, "Old"), file_record(3, "Missing")],
            "file_updates": [],
        },
        current_mod={"name": "Fixture Mod", "author": "Ever", "updated_timestamp": 20},
        current_files={
            "files": [file_record(1, "Current"), file_record(2, "Old", "OLD_VERSION")],
            "file_updates": [],
        },
    )

    statuses = {item["file_id"]: item["status"] for item in result["artifacts"]}
    assert statuses == {1: "current", 2: "unmapped_old", 3: "missing_unmapped"}
    assert result["summary"] == {"current": 1, "missing_unmapped": 1, "unmapped_old": 1}


def test_audit_mod_reports_only_genuinely_new_sibling_files():
    result = audit.audit_mod(
        mod_id=10,
        selected_records=[selected(1)],
        previous_mod={"updated_timestamp": 10},
        previous_files={
            "files": [file_record(1, "Selected"), file_record(8, "Known Optional", "OPTIONAL")],
            "file_updates": [],
        },
        current_mod={"name": "Fixture Mod", "author": "Curio", "updated_timestamp": 20},
        current_files={
            "files": [
                file_record(1, "Selected", "OLD_VERSION"),
                file_record(2, "Selected", "MAIN", "2.0"),
                file_record(8, "Known Optional", "OPTIONAL"),
                file_record(9, "Brand New Optional", "OPTIONAL"),
                file_record(10, "New Old Version", "OLD_VERSION"),
            ],
            "file_updates": [update(1, 2)],
        },
    )

    assert result["artifacts"][0]["status"] == "update_available"
    assert [item["file_id"] for item in result["new_sibling_files"]] == [9]


def test_audit_mod_disables_sibling_claims_for_partial_snapshots():
    result = audit.audit_mod(
        mod_id=10,
        selected_records=[selected(1)],
        previous_mod={"updated_timestamp": 10},
        previous_files={
            "files": [file_record(1, "Selected")],
            "file_updates": [],
            "_snapshot_complete": False,
        },
        current_mod={"name": "Fixture Mod", "author": "Curio", "updated_timestamp": 20},
        current_files={
            "files": [file_record(1, "Selected"), file_record(9, "Unproven New Sibling", "OPTIONAL")],
            "file_updates": [],
        },
    )

    assert result["new_sibling_comparison_available"] is False
    assert result["new_sibling_files"] == []
    assert result["page_metadata_only"] is True


def test_audit_mod_marks_page_only_change_when_artifacts_are_unchanged():
    result = audit.audit_mod(
        mod_id=10,
        selected_records=[selected(1)],
        previous_mod={"updated_timestamp": 10},
        previous_files={"files": [file_record(1, "Selected")], "file_updates": []},
        current_mod={"name": "Fixture Mod", "author": "Someone", "updated_timestamp": 20},
        current_files={"files": [file_record(1, "Selected")], "file_updates": []},
    )

    assert result["page_changed"] is True
    assert result["page_metadata_only"] is True


def test_audit_mod_does_not_invent_page_change_without_previous_timestamp():
    result = audit.audit_mod(
        mod_id=10,
        selected_records=[selected(1)],
        previous_mod={"name": "Fixture Mod"},
        previous_files={
            "files": [file_record(1, "Selected")],
            "file_updates": [],
            "_snapshot_complete": False,
        },
        current_mod={"name": "Fixture Mod", "author": "Someone", "updated_timestamp": 20},
        current_files={"files": [file_record(1, "Selected")], "file_updates": []},
    )

    assert result["page_changed"] is None
    assert result["page_metadata_only"] is False


def test_follow_lineage_rejects_cycles():
    lineage, error = audit.follow_lineage(1, {1: 2, 2: 1})
    assert lineage == [1, 2, 1]
    assert error == "cycle"


def test_build_audit_summarizes_all_manifest_records():
    manifest = [selected(1), {**selected(2), "mod_id": 20, "mod_name": "Other Mod"}]
    previous = {
        10: ({"updated_timestamp": 1}, {"files": [file_record(1, "Selected")], "file_updates": []}),
        20: ({"updated_timestamp": 1}, {"files": [file_record(2, "Selected")], "file_updates": []}),
    }
    current = {
        10: (
            {"name": "Fixture Mod", "author": "Curio", "updated_timestamp": 2},
            {"files": [file_record(1, "Selected", "OLD_VERSION"), file_record(3, "Selected", "MAIN")],
             "file_updates": [update(1, 3)]},
        ),
        20: (
            {"name": "Other Mod", "author": "Ever", "updated_timestamp": 1},
            {"files": [file_record(2, "Selected")], "file_updates": []},
        ),
    }

    result = audit.build_audit(manifest, previous, current, game="fixture")

    assert result["manifest_artifact_count"] == 2
    assert result["mod_count"] == 2
    assert result["summary"] == {"current": 1, "update_available": 1}
    assert result["highlighted_authors"] == ["Curio", "Ever"]


def test_build_audit_groups_author_capitalization_variants():
    manifest = [selected(1), {**selected(2), "mod_id": 20, "mod_name": "Other Mod"}]
    previous = {
        10: ({"updated_timestamp": 1}, {"files": [file_record(1, "Selected")], "file_updates": []}),
        20: ({"updated_timestamp": 1}, {"files": [file_record(2, "Selected")], "file_updates": []}),
    }
    current = {
        10: ({"name": "Fixture Mod", "author": "MyCurio", "updated_timestamp": 1},
             {"files": [file_record(1, "Selected")], "file_updates": []}),
        20: ({"name": "Other Mod", "author": "Mycurio", "updated_timestamp": 1},
             {"files": [file_record(2, "Selected")], "file_updates": []}),
    }

    result = audit.build_audit(manifest, previous, current, game="fixture")

    assert result["highlighted_authors"] == ["MyCurio"]
    assert {mod["author_group"] for mod in result["mods"]} == {"MyCurio"}


def test_build_actionable_replacements_deduplicates_terminal_files_and_extracts_hash():
    replacement = file_record(3, "Replacement", "MAIN", "3.0")
    replacement["detected_file_extension"] = "zip"
    replacement["external_virus_scan_url"] = "https://www.virustotal.com/gui/file/" + "b" * 64
    result = {
        "game": "fixture",
        "mods": [{
            "mod_id": 10,
            "name": "Fixture Mod",
            "artifacts": [
                {"file_id": 1, "status": "update_available", "replacement": {"file_id": 3}},
                {"file_id": 2, "status": "update_available", "replacement": {"file_id": 3}},
            ],
        }],
    }

    records = audit.build_actionable_replacements(
        result,
        {10: ({"name": "Fixture Mod"}, {"files": [replacement]})},
    )

    assert records == [{
        "game_domain_name": "fixture",
        "mod_id": 10,
        "mod_name": "Fixture Mod",
        "file_id": 3,
        "file_name": "Replacement.zip",
        "version": "3.0",
        "category": "MAIN",
        "size_in_bytes": 300,
        "official_sha256": "b" * 64,
        "archive_name": "10-3.zip",
        "replaces_file_ids": [1, 2],
    }]


def test_build_actionable_replacements_preserves_unavailable_exact_size():
    replacement = file_record(3, "Replacement", "MAIN", "3.0")
    replacement["size_in_bytes"] = 0
    replacement["external_virus_scan_url"] = "https://www.virustotal.com/gui/file/" + "b" * 64
    result = {
        "game": "fixture",
        "mods": [{
            "mod_id": 10,
            "name": "Fixture Mod",
            "artifacts": [
                {"file_id": 1, "status": "update_available", "replacement": {"file_id": 3}},
            ],
        }],
    }

    records = audit.build_actionable_replacements(
        result,
        {10: ({"name": "Fixture Mod"}, {"files": [replacement]})},
    )

    assert records[0]["size_in_bytes"] is None


def test_write_outputs_creates_machine_and_human_reports(tmp_path):
    result = {
        "generated_at": "2026-11-11T00:00:00+00:00",
        "game": "fixture",
        "manifest_artifact_count": 1,
        "mod_count": 1,
        "summary": {"update_available": 1},
        "highlighted_authors": ["Curio"],
        "mods": [{
            "mod_id": 10,
            "name": "Fixture Mod",
            "author": "Curio",
            "summary": {"update_available": 1},
            "page_changed": True,
            "page_metadata_only": False,
            "new_sibling_files": [],
            "artifacts": [{
                "file_id": 1,
                "file_name": "Selected.zip",
                "version": "1.0",
                "status": "update_available",
                "lineage": [1, 2],
                "replacement": {"file_id": 2, "name": "Selected", "version": "2.0"},
            }],
        }],
    }

    json_path, report_path = audit.write_outputs(result, tmp_path)

    assert json.loads(json_path.read_text())["summary"] == {"update_available": 1}
    report = report_path.read_text()
    assert "Curio" in report
    assert "Selected.zip" in report
    assert "1 -> 2" in report


def test_inspect_manifest_archives_resolves_download_directory(tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    archive = downloads / "fixture.zip"
    archive.write_bytes(b"data")
    manifest_path = tmp_path / "download-manifest.json"
    manifest_path.write_text("[]")
    manifest = [{**selected(1), "path": "fixture.zip", "size": 4}]

    result = audit.inspect_manifest_archives(manifest_path, manifest)

    assert result == {
        "summary": {"present": 1},
        "present_bytes": 4,
        "problems": [],
        "artifact_count": 1,
        "archive_copy_count": 1,
    }


def test_inspect_manifest_archives_validates_all_preserved_copies(tmp_path):
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    first.write_bytes(b"data")
    second.write_bytes(b"data")
    manifest_path = tmp_path / "download-manifest.json"
    manifest_path.write_text("[]")
    manifest = [{
        **selected(1),
        "path": str(first),
        "paths": [str(first), str(second)],
        "size": 4,
    }]

    result = audit.inspect_manifest_archives(manifest_path, manifest)

    assert result == {
        "summary": {"present": 2},
        "present_bytes": 8,
        "problems": [],
        "artifact_count": 1,
        "archive_copy_count": 2,
    }


def test_parse_tool_responses_rejects_non_json_tool_content_safely():
    stdout = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": "init", "result": {}}),
        json.dumps({
            "jsonrpc": "2.0",
            "id": "69-mod",
            "result": {
                "isError": True,
                "content": [{"type": "text", "text": "remote API returned a non-JSON error"}],
            },
        }),
    ])

    with pytest.raises(RuntimeError, match="69-mod.*non-JSON"):
        audit.parse_tool_responses(stdout, {"69-mod"})


def test_parse_tool_responses_can_preserve_provider_errors_for_batch_audits():
    stdout = json.dumps({
        "jsonrpc": "2.0",
        "id": "988-files",
        "result": {
            "isError": True,
            "content": [{"type": "text", "text": "provider rejected the request"}],
        },
    })

    result = audit.parse_tool_responses(stdout, {"988-files"}, allow_tool_errors=True)

    assert result == {"988-files": {"_tool_error": True}}


def test_run_lineage_audit_uses_builder_previous_metadata_directory_by_default(tmp_path):
    inventory = tmp_path / "inventory"
    previous = inventory / "previous-metadata"
    current = tmp_path / "current-metadata"
    output = tmp_path / "output"
    previous.mkdir(parents=True)
    current.mkdir()
    archive = inventory / "fixture.zip"
    archive.write_bytes(b"fixture")
    write_json(inventory / "download-manifest.json", [{
        **selected(1),
        "path": str(archive),
        "size": archive.stat().st_size,
    }])
    for directory in (previous, current):
        write_json(directory / "mod-10.json", {"name": "Fixture Mod", "updated_timestamp": 10})
        write_json(directory / "files-10.json", {
            "files": [file_record(1, "Selected")],
            "file_updates": [],
        })

    audit.run_lineage_audit(argparse.Namespace(
        manifest=inventory / "download-manifest.json",
        previous_metadata=None,
        current_metadata=current,
        output=output,
        game="fixture",
    ))

    result = json.loads((output / "update-audit.json").read_text())
    assert result["previous_metadata"] == str(previous.resolve())
    assert result["summary"] == {"current": 1}


def test_audit_mod_marks_artifacts_unavailable_when_file_metadata_fetch_fails():
    result = audit.audit_mod(
        mod_id=10,
        selected_records=[selected(1)],
        previous_mod={"name": "Fixture Mod", "author": "Curio", "updated_timestamp": 10},
        previous_files={"files": [file_record(1, "Selected")], "file_updates": []},
        current_mod={"name": "Fixture Mod", "author": "Curio", "updated_timestamp": 20},
        current_files={"_tool_error": True},
    )

    assert result["summary"] == {"metadata_unavailable": 1}
    assert result["artifacts"][0]["status"] == "metadata_unavailable"
    assert result["metadata_errors"] == ["file_metadata_unavailable"]
    assert result["new_sibling_comparison_available"] is False
