# Mod Downloader MCP Handoff

Last verified: 2026-11-11 on `main`; this document is part of the verified
revision.

## Purpose

`mod-downloader-mcp` is a dependency-free Python MCP server for official Nexus
Mods, Thunderstore, CurseForge, and Steam Workshop APIs. It provides catalog
search, metadata lookup, official download resolution, staged downloads, durable
download tracking, and tracked update checks.

The repository also carries deterministic TCG archive-inventory and Nexus
lineage-audit utilities under `scripts/`. Their generated inventories, metadata
snapshots, reports, and downloaded archives belong outside Git.

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

Nexus update checks are artifact-specific. They follow official `file_updates`
replacement chains for every tracked or supplied `{mod_id, file_id}` identity,
so unrelated files on the same Nexus page are not treated as replacements.

## TCG audit workflow

Build a deterministic inventory from an archive tree:

<!-- markdownlint-disable-next-line MD013 -->
`scripts/build-tcg-audit-manifest.py discover DOWNLOADS_ROOT AUDIT_INVENTORY`

The builder writes `download-manifest.json`, `previous-metadata/`, and
`inventory-summary.json`. Run an offline-reproducible audit with a saved current
metadata snapshot:

<!-- markdownlint-disable-next-line MD013 -->
`scripts/tcg-update-audit.py AUDIT_OUTPUT --manifest AUDIT_INVENTORY/download-manifest.json --current-metadata CURRENT_METADATA`

The audit defaults to `previous-metadata/` beside the manifest. If
`--current-metadata` is omitted, it fetches a fresh snapshot through the local
MCP launcher. The audit does not install mods or launch the game.

## Verification

Run tests serially:

`PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q`

Compile Python files with one worker:

<!-- markdownlint-disable-next-line MD013 -->
`PYTHONDONTWRITEBYTECODE=1 python3 -m compileall -q -j 1 nexusmods_mcp tests scripts`

Type-check the changed Python entry points:

<!-- markdownlint-disable-next-line MD013 -->
`MYPYPATH=. python3 -m mypy --show-error-codes --no-error-summary nexusmods_mcp/server.py scripts/tcg-update-audit.py scripts/build-tcg-audit-manifest.py`

Validate both utility entry points:

`scripts/build-tcg-audit-manifest.py --help`

`scripts/tcg-update-audit.py --help`

The protocol regression tests cover newline-delimited and Content-Length MCP
framing, malformed requests, tool validation, error redaction, catalog search,
provider downloads, persistent tracking, and update checks.

## Safety boundaries

- Do not scrape provider web pages; use official APIs and returned URLs.
- Never print API keys or raw credential-bearing errors.
- Preserve protocol-only stdout; diagnostics belong on stderr or in files.
- Update checks report candidates but do not install, replace, or delete mods.
