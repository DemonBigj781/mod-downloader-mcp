from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexusmods_mcp import server as server_module
from nexusmods_mcp.server import Config, DownloadTracker, McpServer


def config(tmp_path: Path) -> Config:
    return Config(
        "fixture-nexus-secret",
        "",
        "fixture-curseforge-secret",
        "fixture-steam-secret",
        "US",
        (),
        tmp_path / "staging",
        16000,
        tmp_path / "tracking.sqlite3",
    )


def fake_download(_uri: str, target_path: Path) -> int:
    payload = b"fixture archive"
    target_path.write_bytes(payload)
    return len(payload)


def test_nexus_download_uses_staging_tree_and_records_artifact(monkeypatch, tmp_path):
    server = McpServer(config(tmp_path))
    monkeypatch.setattr(server.client, "get_download_links", lambda *_args: {
        "links": [{"URI": "https://example.invalid/mod.zip", "name": "fixture"}],
    })
    monkeypatch.setattr(server.client, "list_mod_files", lambda *_args: {
        "files": [{
            "file_id": 22,
            "file_name": "fixture.zip",
            "version": "2.0",
            "category_name": "MAIN",
            "uploaded_timestamp": 200,
        }],
    })
    monkeypatch.setattr(server.client, "get_mod", lambda *_args: {
        "name": "Fixture Mod",
        "version": "2.0",
        "updated_timestamp": 500,
    })
    monkeypatch.setattr(server_module, "download_uri", fake_download)

    result = server.client.download_mod_file("fixturegame", 11, 22)

    downloaded = Path(result["downloaded"])
    assert downloaded.parent == tmp_path / "staging" / "nexus" / "fixturegame" / "11"
    tracked = server.tracker.list_downloads()
    assert len(tracked) == 1
    assert tracked[0]["provider"] == "nexus"
    assert tracked[0]["source_key"] == "fixturegame:11"
    assert tracked[0]["artifact_id"] == "22"
    assert tracked[0]["version"] == "2.0"
    assert tracked[0]["remote_updated_at"] == 500
    assert tracked[0]["sha256"]
    assert result["tracking"]["record_id"] == tracked[0]["id"]


@pytest.mark.parametrize("provider", ["thunderstore", "curseforge", "steam"])
def test_other_provider_downloads_use_staging_and_record_artifacts(monkeypatch, tmp_path, provider):
    server = McpServer(config(tmp_path))
    monkeypatch.setattr(server_module, "download_uri", fake_download)

    if provider == "thunderstore":
        monkeypatch.setattr(server.thunderstore, "latest_version", lambda *_args: {
            "version_number": "1.2.0",
            "date_created": "2026-11-11T10:00:00Z",
            "download_url": "https://example.invalid/thunderstore.zip",
        })
        result = server.thunderstore.download_package("owner", "package")
        expected_parent = tmp_path / "staging" / "thunderstore" / "owner" / "package"
        expected_source = "owner:package"
        expected_artifact = "1.2.0"
    elif provider == "curseforge":
        monkeypatch.setattr(server.curseforge, "get_download_url", lambda *_args: {
            "download_url": "https://example.invalid/curseforge.zip",
        })
        monkeypatch.setattr(server.curseforge, "get_mod_files", lambda *_args, **_kwargs: {
            "files": [{"id": 44, "fileDate": "2026-11-11T10:00:00Z", "displayName": "Release 4"}],
        })
        monkeypatch.setattr(server.curseforge, "get_mod", lambda *_args: {
            "name": "Fixture CurseForge Mod",
            "dateModified": "2026-11-12T10:00:00Z",
        })
        result = server.curseforge.download_file(33, 44)
        expected_parent = tmp_path / "staging" / "curseforge" / "33"
        expected_source = "33"
        expected_artifact = "44"
    else:
        monkeypatch.setattr(server.steam, "get_workshop_download_info", lambda *_args: {
            "file_url": "https://example.invalid/workshop.zip",
            "item": {"time_updated": 200, "title": "Fixture Workshop Mod"},
        })
        result = server.steam.download_workshop_file(55)
        expected_parent = tmp_path / "staging" / "steam" / "55"
        expected_source = "55"
        expected_artifact = "55"

    assert Path(result["downloaded"]).parent == expected_parent
    tracked = server.tracker.list_downloads(provider=provider)
    assert len(tracked) == 1
    assert tracked[0]["source_key"] == expected_source
    assert tracked[0]["artifact_id"] == expected_artifact
    if provider == "curseforge":
        assert tracked[0]["remote_updated_at"] == 1794477600


def test_check_tracked_updates_reports_newer_nexus_file(monkeypatch, tmp_path):
    server = McpServer(config(tmp_path))
    archive = tmp_path / "installed.zip"
    archive.write_bytes(b"old")
    server.tracker.record_download(
        provider="nexus",
        source_key="fixturegame:11",
        artifact_id="22",
        version="2.0",
        remote_updated_at=200,
        local_path=archive,
        size_bytes=archive.stat().st_size,
        metadata={"game_domain_name": "fixturegame", "mod_id": 11, "file_id": 22},
    )
    monkeypatch.setattr(server.client, "list_mod_files", lambda *_args: {
        "files": [
            {"file_id": 22, "version": "2.0", "category_name": "OLD_VERSION", "uploaded_timestamp": 200},
            {"file_id": 23, "version": "2.1", "category_name": "MAIN", "uploaded_timestamp": 300},
        ],
        "file_updates": [{"old_file_id": 22, "new_file_id": 23, "uploaded_timestamp": 300}],
    })
    monkeypatch.setattr(server.client, "get_mod", lambda *_args: {"updated_timestamp": 300, "version": "2.1"})

    result = server.check_tracked_updates(provider="nexus")

    assert result["tracked_sources"] == 1
    assert result["updates_available"] == 1
    assert result["results"][0]["update_available"] is True
    assert result["results"][0]["latest"]["file_id"] == 23
    assert result["results"][0]["lineage"] == [22, 23]


def test_nexus_update_check_tracks_every_file_on_multi_file_pages(monkeypatch, tmp_path):
    server = McpServer(config(tmp_path))
    for file_id in (22, 40):
        archive = tmp_path / f"{file_id}.zip"
        archive.write_bytes(str(file_id).encode())
        server.tracker.record_download(
            provider="nexus",
            source_key="fixturegame:11",
            artifact_id=str(file_id),
            version="1.0",
            remote_updated_at=file_id * 10,
            local_path=archive,
            size_bytes=archive.stat().st_size,
            metadata={"game_domain_name": "fixturegame", "mod_id": 11, "file_id": file_id},
        )
    monkeypatch.setattr(server.client, "get_mod", lambda *_args: {
        "name": "Multi-file Fixture",
        "updated_timestamp": 900,
    })
    monkeypatch.setattr(server.client, "list_mod_files", lambda *_args: {
        "files": [
            {"file_id": 22, "version": "1.0", "category_name": "OLD_VERSION", "uploaded_timestamp": 220},
            {"file_id": 23, "version": "2.0", "category_name": "MAIN", "uploaded_timestamp": 230},
            {"file_id": 40, "version": "1.0", "category_name": "MAIN", "uploaded_timestamp": 400},
            {"file_id": 99, "version": "9.0", "category_name": "MAIN", "uploaded_timestamp": 900},
        ],
        "file_updates": [{"old_file_id": 22, "new_file_id": 23, "uploaded_timestamp": 230}],
    })

    result = server.check_tracked_updates(provider="nexus")

    assert result["tracked_sources"] == 2
    assert result["updates_available"] == 1
    by_file = {int(item["tracked"]["artifact_id"]): item for item in result["results"]}
    assert by_file[22]["status"] == "update_available"
    assert by_file[22]["latest"]["file_id"] == 23
    assert by_file[40]["status"] == "current"
    assert by_file[40]["update_available"] is False


def test_nexus_inventory_check_does_not_redownload_a_replacement_already_present(monkeypatch, tmp_path):
    server = McpServer(config(tmp_path))
    monkeypatch.setattr(server.client, "get_mod", lambda *_args: {
        "name": "Fixture Mod",
        "author": "Fixture Author",
    })
    monkeypatch.setattr(server.client, "list_mod_files", lambda *_args: {
        "files": [
            {"file_id": 22, "version": "1.0", "category_name": "OLD_VERSION", "uploaded_timestamp": 220},
            {"file_id": 23, "version": "2.0", "category_name": "MAIN", "uploaded_timestamp": 230},
        ],
        "file_updates": [{"old_file_id": 22, "new_file_id": 23, "uploaded_timestamp": 230}],
    })

    result = server.check_nexus_artifact_updates(
        "fixturegame",
        [{"mod_id": 11, "file_id": 22}, {"mod_id": 11, "file_id": 23}],
    )

    assert result["artifact_count"] == 2
    assert result["updates_available"] == 0
    by_file = {item["file_id"]: item for item in result["results"]}
    assert by_file[22]["status"] == "superseded_in_inventory"
    assert by_file[22]["lineage"] == [22, 23]
    assert by_file[23]["status"] == "current"


def test_nexus_update_check_does_not_treat_an_unrelated_file_as_an_update(monkeypatch, tmp_path):
    server = McpServer(config(tmp_path))
    archive = tmp_path / "card-set.zip"
    archive.write_bytes(b"card set")
    server.tracker.record_download(
        provider="nexus",
        source_key="fixturegame:11",
        artifact_id="22",
        version="2.0",
        remote_updated_at=500,
        local_path=archive,
        size_bytes=archive.stat().st_size,
        metadata={"game_domain_name": "fixturegame", "mod_id": 11, "file_id": 22},
    )
    monkeypatch.setattr(server.client, "get_mod", lambda *_args: {"updated_timestamp": 500, "version": "9.0"})
    monkeypatch.setattr(server.client, "list_mod_files", lambda *_args: {
        "files": [
            {"file_id": 22, "version": "2.0", "category_name": "MAIN", "uploaded_timestamp": 100},
            {"file_id": 99, "version": "9.0", "category_name": "MAIN", "uploaded_timestamp": 500},
        ],
    })

    result = server.check_tracked_updates(provider="nexus")

    assert result["updates_available"] == 0
    assert result["results"][0]["update_available"] is False


def test_tool_catalog_exposes_staging_tracking_and_update_checks(tmp_path):
    server = McpServer(config(tmp_path))
    tools = {tool["name"]: tool for tool in server._tool_descriptions()}

    assert {
        "get_download_staging",
        "list_tracked_downloads",
        "check_tracked_updates",
        "check_nexus_artifact_updates",
    } <= tools.keys()
    assert "provider" in tools["check_tracked_updates"]["inputSchema"]["properties"]
    assert tools["check_nexus_artifact_updates"]["inputSchema"]["required"] == [
        "game_domain_name",
        "artifacts",
    ]


def test_tracking_tools_are_callable_through_mcp_protocol(tmp_path):
    server = McpServer(config(tmp_path))

    staging_response = server.handle({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_download_staging", "arguments": {}},
    })
    updates_response = server.handle({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "check_tracked_updates", "arguments": {}},
    })

    staging = json.loads(staging_response["result"]["content"][0]["text"])
    updates = json.loads(updates_response["result"]["content"][0]["text"])
    assert staging["staging_dir"] == str((tmp_path / "staging").resolve())
    assert updates == {
        "provider": "all",
        "tracked_sources": 0,
        "updates_available": 0,
        "errors": 0,
        "only_updates": False,
        "results": [],
    }


def test_nexus_inventory_update_check_is_callable_through_mcp_protocol(monkeypatch, tmp_path):
    server = McpServer(config(tmp_path))
    monkeypatch.setattr(server.client, "get_mod", lambda *_args: {"name": "Fixture Mod"})
    monkeypatch.setattr(server.client, "list_mod_files", lambda *_args: {
        "files": [{"file_id": 22, "version": "1.0", "category_name": "MAIN"}],
        "file_updates": [],
    })

    response = server.handle({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "check_nexus_artifact_updates",
            "arguments": {
                "game_domain_name": "fixturegame",
                "artifacts": [{"mod_id": 11, "file_id": 22}],
            },
        },
    })

    result = json.loads(response["result"]["content"][0]["text"])
    assert result["updates_available"] == 0
    assert result["results"][0]["status"] == "current"


@pytest.mark.parametrize("provider", ["thunderstore", "curseforge", "steam"])
def test_check_tracked_updates_for_other_providers(monkeypatch, tmp_path, provider):
    server = McpServer(config(tmp_path))
    archive = tmp_path / f"{provider}.zip"
    archive.write_bytes(b"old")
    if provider == "thunderstore":
        source_key = "owner:package"
        artifact_id = "1.0.0"
        metadata = {"namespace": "owner", "name": "package", "version": "1.0.0"}
        monkeypatch.setattr(server.thunderstore, "latest_version", lambda *_args: {
            "version_number": "2.0.0",
            "date_created": "1970-01-01T00:03:20Z",
            "download_url": "https://example.invalid/package.zip",
        })
    elif provider == "curseforge":
        source_key = "33"
        artifact_id = "44"
        metadata = {"mod_id": 33, "file_id": 44}
        monkeypatch.setattr(server.curseforge, "get_mod", lambda *_args: {
            "dateModified": "1970-01-01T00:03:20Z",
        })
        monkeypatch.setattr(server.curseforge, "get_mod_files", lambda *_args, **_kwargs: {
            "files": [{"id": 45, "fileDate": "1970-01-01T00:03:20Z"}],
        })
    else:
        source_key = "55"
        artifact_id = "55"
        metadata = {"published_file_id": 55}
        monkeypatch.setattr(server.steam, "get_workshop_details", lambda *_args: {
            "details": [{"published_file_id": 55, "time_updated": 200}],
        })
    server.tracker.record_download(
        provider=provider,
        source_key=source_key,
        artifact_id=artifact_id,
        version=artifact_id,
        remote_updated_at=100,
        local_path=archive,
        size_bytes=archive.stat().st_size,
        metadata=metadata,
    )

    result = server.check_tracked_updates(provider=provider)

    assert result["updates_available"] == 1
    assert result["results"][0]["update_available"] is True


def test_config_prefers_generic_staging_and_tracking_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_API_KEY", "fixture-not-a-real-key")
    monkeypatch.setenv("MOD_DOWNLOADER_STAGING_DIR", str(tmp_path / "all-mods"))
    monkeypatch.setenv("MOD_DOWNLOADER_TRACKING_DB", str(tmp_path / "state" / "downloads.sqlite3"))
    monkeypatch.setenv("NEXUS_DOWNLOAD_DIR", str(tmp_path / "legacy"))

    loaded = Config.load()

    assert loaded.download_dir == tmp_path / "all-mods"
    assert loaded.tracking_db == tmp_path / "state" / "downloads.sqlite3"


def test_tracker_survives_new_server_instance(tmp_path):
    cfg = config(tmp_path)
    archive = tmp_path / "persisted.zip"
    archive.write_bytes(b"persisted")
    first = DownloadTracker(cfg.tracking_path)
    first.record_download(
        provider="nexus",
        source_key="fixturegame:99",
        artifact_id="100",
        version="1.0",
        remote_updated_at=100,
        local_path=archive,
        size_bytes=archive.stat().st_size,
        metadata={"game_domain_name": "fixturegame", "mod_id": 99, "file_id": 100},
    )

    second = McpServer(cfg)

    assert second.tracker.list_downloads()[0]["source_key"] == "fixturegame:99"
