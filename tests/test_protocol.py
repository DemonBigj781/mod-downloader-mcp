from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from nexusmods_mcp.server import Config, McpServer, read_messages


@pytest.fixture
def server(tmp_path):
    return McpServer(Config("fixture-nexus-secret", "", "", "", "US", (), tmp_path, 16000))


def request(method, params=None, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def test_ping_preserves_request_id(server):
    assert server.handle(request("ping", request_id="alive")) == {
        "jsonrpc": "2.0", "id": "alive", "result": {}
    }


def test_tool_failure_is_readable_tool_error_and_redacts_credentials(server):
    def fail(_args):
        raise RuntimeError("Nexus API HTTP 429 fixture-nexus-secret")

    server.tools["fixture"] = ("test", {"type": "object"}, fail)
    response = server.handle(request("tools/call", {"name": "fixture"}))
    assert "error" not in response
    assert response["result"]["isError"] is True
    text = response["result"]["content"][0]["text"]
    assert "HTTP 429" in text
    assert "fixture-nexus-secret" not in text
    assert server.handle(request("ping"))["result"] == {}


@pytest.mark.parametrize("params", [
    {"name": "missing-tool"},
    {"name": "get_mod", "arguments": []},
    {"name": "get_mod", "arguments": {"game_domain_name": "fixture"}},
    {"name": "get_mod", "arguments": {"game_domain_name": "fixture", "mod_id": True}},
    {"name": "get_mod", "arguments": {"game_domain_name": "fixture", "mod_id": "wrong"}},
])
def test_invalid_call_is_protocol_error_without_calling_network(server, monkeypatch, params):
    def unexpected(*_args, **_kwargs):
        pytest.fail("invalid arguments reached the network")

    monkeypatch.setattr(server.client, "request", unexpected)
    response = server.handle(request("tools/call", params))
    assert response["error"]["code"] == -32602
    assert response["id"] == 1


@pytest.mark.parametrize("payload", [None, [], 42, "not a request", {"jsonrpc": "2.0", "id": 4}])
def test_invalid_jsonrpc_request_does_not_crash_server(server, payload):
    assert server.handle(payload)["error"]["code"] == -32600


@pytest.mark.parametrize("framed", [False, True])
def test_bad_json_returns_parse_error_and_next_request_still_works(framed, tmp_path):
    launcher = Path(__file__).resolve().parents[1] / "scripts" / "nexusmods-mcp"
    bodies = [b'{"broken":', json.dumps(request("ping", request_id=2)).encode(),
              json.dumps(request("tools/list", request_id=3)).encode()]
    if framed:
        wire = b"".join(f"Content-Length: {len(body)}\r\n\r\n".encode() + body for body in bodies)
    else:
        wire = b"\n".join(bodies) + b"\n"
    result = subprocess.run([str(launcher)], cwd=tmp_path,
        env={**os.environ, "NEXUS_API_KEY": "fixture-nexus-secret"},
        input=wire, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    messages = [message for message, _mode in read_messages(io.BytesIO(result.stdout))]
    assert messages[0]["error"]["code"] == -32700
    assert messages[0]["id"] is None
    assert messages[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}
    assert any(t["name"] == "get_mod" for t in messages[2]["result"]["tools"])
    assert b"Traceback" not in result.stderr


def test_notifications_do_not_get_responses(server):
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert server.handle(request("unknown"))["error"]["code"] == -32601


def test_tool_catalog_exposes_bounded_mod_search(server):
    search = next(tool for tool in server._tool_descriptions() if tool["name"] == "search_mods")
    properties = search["inputSchema"]["properties"]

    assert "query" in properties
    assert "max_pages" in properties
    assert "sort" in properties
    assert "direction" in properties
    assert "official Nexus API v3" in search["description"]


@pytest.mark.parametrize("request_id", [[], {}, True])
def test_invalid_request_ids_cannot_produce_invalid_response_ids(server, request_id):
    response = server.handle(request("ping", request_id=request_id))
    assert response["id"] is None
    assert response["error"]["code"] == -32600


@pytest.mark.parametrize("wire", [
    b"Content-Length: wrong\r\n\r\n",
    b"Content-Length: -1\r\n\r\n",
    b"Content-Type: application/json\r\n\r\n",
    b'Content-Length: 100\r\n\r\n{"jsonrpc":"2.0","id":1,"method":"ping"}',
])
def test_invalid_framing_reports_error_and_exits_cleanly(wire, tmp_path):
    launcher = Path(__file__).resolve().parents[1] / "scripts" / "nexusmods-mcp"
    result = subprocess.run([str(launcher)], cwd=tmp_path,
        env={**os.environ, "NEXUS_API_KEY": "fixture-nexus-secret"},
        input=wire, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    messages = [message for message, _mode in read_messages(io.BytesIO(result.stdout))]
    assert len(messages) == 1
    assert messages[0]["error"]["code"] == -32700
    assert b"Traceback" not in result.stderr
