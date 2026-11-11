# Mod Downloader MCP Handoff

Last verified: 2026-11-11, from the working tree based on revision `3364f9a`.

## Purpose

`mod-downloader-mcp` is a dependency-free Python MCP server for official Nexus
Mods, Thunderstore, CurseForge, and Steam Workshop APIs. It provides catalog
search, metadata lookup, official download resolution, staged downloads, durable
download tracking, and tracked update checks.

## Entry points

- Python module: `python3 -m nexusmods_mcp.server`
- Local launcher: `scripts/nexusmods-mcp`
- Installed commands: `mod-downloader-mcp` and `nexusmods-mcp`

The launcher resolves the checkout dynamically, sources the local secrets file,
and keeps stdout reserved for MCP JSON-RPC messages.

## Download and tracking layout

The default staging root is `staging/`, ignored by Git. Provider downloads are
stored under provider/source subdirectories. Successful downloads are recorded
in `staging/downloads.sqlite3` with remote identity/version metadata, local path,
size, and SHA-256 digest.

Configuration and public tracking tools are documented in `README.md`. The
generic environment variables are `MOD_DOWNLOADER_STAGING_DIR` and
`MOD_DOWNLOADER_TRACKING_DB`; legacy Nexus download-directory configuration is
still accepted.

## Verification

Run tests serially:

`PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q`

Compile Python files with one worker:

`PYTHONDONTWRITEBYTECODE=1 python3 -m compileall -q -j 1 nexusmods_mcp tests`

The protocol regression tests cover newline-delimited and Content-Length MCP
framing, malformed requests, tool validation, error redaction, catalog search,
provider downloads, persistent tracking, and update checks.

## Safety boundaries

- Do not scrape provider web pages; use official APIs and returned URLs.
- Never print API keys or raw credential-bearing errors.
- Preserve protocol-only stdout; diagnostics belong on stderr or in files.
- Update checks report candidates but do not install, replace, or delete mods.
