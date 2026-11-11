"""Stage requested TCG mod metadata/packages through the local MCP server.

This script never launches a game or writes into its installation.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
MOD_IDS = (1592, 1754, 1609, 1492, 1474, 1729, 1632, 1777, 875, 1116, 1431)
GAME = "tcgcardshopsimulator"


def call_tools(calls):
    requests = [{"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "tcg-update-audit", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"}]
    for identifier, tool, arguments in calls:
        requests.append({"jsonrpc": "2.0", "id": identifier, "method": "tools/call",
                         "params": {"name": tool, "arguments": arguments}})
    env = {key: value for key, value in os.environ.items()
           if key not in ("NEXUS_API_KEY", "NEXUSMODS_API_KEY", "nexus_api_key", "nexux_api_key")}
    env["NEXUS_MCP_MAX_RESULT_CHARS"] = "2000000"
    result = subprocess.run([str(ROOT / "scripts/nexusmods-mcp")],
                            input="".join(json.dumps(request) + "\n" for request in requests),
                            capture_output=True, text=True, timeout=600, env=env, check=False)
    if result.returncode:
        raise RuntimeError("MCP server exited; no credentials or raw stderr displayed")
    responses = {}
    for line in result.stdout.splitlines():
        message = json.loads(line)
        if message.get("id") == "init":
            continue
        if "error" in message:
            raise RuntimeError(f"MCP request {message.get('id')} failed with code {message['error'].get('code')}")
        responses[message["id"]] = json.loads(message["result"]["content"][0]["text"])
    if set(responses) != {call[0] for call in calls}:
        raise RuntimeError("Incomplete MCP response set")
    return responses


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--download", nargs="+", metavar="MOD:FILE")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.download:
        calls = []
        for pair in args.download:
            mod_id, file_id = map(int, pair.split(":"))
            if mod_id not in MOD_IDS or file_id <= 0:
                raise ValueError("Download is outside the requested mod set")
            calls.append((pair, "download_mod_file", {
                "game_domain_name": GAME, "mod_id": mod_id, "file_id": file_id,
                "output_dir": str(output / "archives"), "filename": f"{mod_id}-{file_id}.zip"}))
        results = call_tools(calls)
        with (output / "downloads.jsonl").open("a", encoding="utf-8") as stream:
            for pair, result in results.items():
                stream.write(json.dumps({"id": pair, **result}) + "\n")
                print(pair, result["bytes"], result["downloaded"])
        return

    calls = []
    for mod_id in MOD_IDS:
        arguments = {"game_domain_name": GAME, "mod_id": mod_id}
        calls.extend([(f"{mod_id}-mod", "get_mod", arguments),
                      (f"{mod_id}-files", "list_mod_files", arguments)])
    results = call_tools(calls)
    for identifier, data in results.items():
        with (output / f"{identifier}.json").open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
    for mod_id in MOD_IDS:
        mod = results[f"{mod_id}-mod"]
        print(f"{mod_id}: {mod.get('name')} | listed version {mod.get('version')}")
        files = results[f"{mod_id}-files"]["files"]
        for file in files:
            if file.get("category_name") in ("MAIN", "UPDATE", "OPTIONAL"):
                print(" ", json.dumps({key: file.get(key) for key in (
                    "file_id", "name", "version", "category_name", "file_name", "size_in_bytes")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
