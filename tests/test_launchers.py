from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("launcher_name", ("mod-downloader-mcp", "nexusmods-mcp"))
def test_launcher_imports_package_outside_repository(tmp_path: Path, launcher_name: str) -> None:
    launcher = PROJECT_ROOT / "scripts" / launcher_name

    result = subprocess.run(
        [str(launcher), "--help"],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Nexus Mods API MCP server" in result.stdout


@pytest.mark.parametrize("launcher_name", ("mod-downloader-mcp", "nexusmods-mcp"))
def test_launcher_completes_framed_mcp_handshake(tmp_path: Path, launcher_name: str) -> None:
    launcher = PROJECT_ROOT / "scripts" / launcher_name
    request = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "launcher-test", "version": "1"},
            },
        },
        separators=(",", ":"),
    ).encode()
    frame = f"Content-Length: {len(request)}\r\n\r\n".encode() + request

    result = subprocess.run(
        [str(launcher)],
        cwd=tmp_path,
        env={**os.environ, "NEXUS_API_KEY": "test-key"},
        input=frame,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode()
    headers, body = result.stdout.split(b"\r\n\r\n", 1)
    content_length = int(
        next(line for line in headers.splitlines() if line.lower().startswith(b"content-length:")).split(b":", 1)[1]
    )
    response = json.loads(body[:content_length])
    assert response["result"]["protocolVersion"] == "2025-06-18"
    assert response["result"]["serverInfo"]["name"] == "nexusmods-mcp"


@pytest.mark.parametrize("launcher_name", ("mod-downloader-mcp", "nexusmods-mcp"))
def test_launcher_completes_json_lines_handshake_and_discovery(tmp_path: Path, launcher_name: str) -> None:
    requests = [
        {"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "stdio-test", "version": "1"},
        }},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    result = subprocess.run(
        [str(PROJECT_ROOT / "scripts" / launcher_name)], cwd=tmp_path,
        env={**os.environ, "NEXUS_API_KEY": "test-key"},
        input="".join(json.dumps(request) + "\n" for request in requests),
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    messages = [json.loads(line) for line in result.stdout.splitlines()]
    assert [message["id"] for message in messages] == ["init", 2]
    assert messages[0]["result"]["protocolVersion"] == "2025-06-18"
    assert any(tool["name"] == "get_mod" for tool in messages[1]["result"]["tools"])
