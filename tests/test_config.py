from nexusmods_mcp.server import Config


def test_lowercase_nexus_key_used_by_the_secrets_file(monkeypatch, tmp_path):
    for name in ("NEXUS_API_KEY", "NEXUSMODS_API_KEY", "nexux_api_key", "nexus_api_key"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("nexus_api_key", "fixture-not-a-real-key")
    monkeypatch.setenv("NEXUS_DOWNLOAD_DIR", str(tmp_path))
    assert Config.load().api_key == "fixture-not-a-real-key"
