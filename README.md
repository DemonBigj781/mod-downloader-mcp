# Mod Downloader MCP

Local MCP server for official mod-host APIs. It intentionally does not scrape
Nexus, Thunderstore, CurseForge, or Steam pages, so it stays out of Cloudflare
and anti-bot flows.

## MCP transport and errors

The server supports newline-delimited JSON and the existing Content-Length
framing. Replies use the request's framing. Standard `ping` requests return an
empty result. Invalid tool names/arguments return protocol errors; API/download
failures return readable tool results with `isError: true`. Configured API keys
are redacted from error messages.

Malformed JSON returns a parse error without killing the connection; subsequent
requests still work when the message boundary is known. Invalid or truncated
Content-Length framing returns a parse error and ends the stream because its
remaining message boundaries cannot be trusted.

If Code reports `Transport closed`, a server patch cannot resurrect that dead
session connection. Use `/mcp reload nexusmods` in Code, then retry a metadata
tool. A successful fresh launcher connection does not prove that the older
session connection has been replaced. The October 16 failure's original trigger
was not captured; malformed-input crashes were reproduced separately and fixed.

Run regression tests from this project directory: `python3 -m pytest -q`.

## Download staging and update tracking

All provider download tools use one shared staging root by default. Files are
separated into provider/source subdirectories, for example:

- `staging/nexus/<game-domain>/<mod-id>/`
- `staging/thunderstore/<namespace>/<package>/`
- `staging/curseforge/<mod-id>/`
- `staging/steam/<published-file-id>/`

Every successful download is recorded in a SQLite database with its provider
identity, remote artifact/version, remote update timestamp, local path, byte
size, SHA-256 digest, and provider metadata. Explicit `output_dir` arguments
remain supported and those downloads are tracked too.

Environment variables:

- `MOD_DOWNLOADER_STAGING_DIR` sets the shared staging root.
- `MOD_DOWNLOADER_TRACKING_DB` sets the SQLite database path.
- `NEXUS_STAGING_DIR` is a Nexus-oriented staging alias.
- `NEXUS_DOWNLOAD_DIR` remains accepted as the legacy staging alias.

When no variable is set, the source checkout uses `staging/` and stores the
database at `staging/downloads.sqlite3`.

Tracking tools:

- `get_download_staging` reports the resolved staging/database paths and counts.
- `list_tracked_downloads` lists recorded artifacts and whether each local file
  still exists.
- `check_tracked_updates` checks every tracked artifact against current official
  provider metadata. It can filter by provider and optionally return only
  updates/errors. Nexus checks follow explicit `file_updates` replacement chains
  independently for every downloaded file, so an unrelated newer file on the
  same multi-file page is not reported as its replacement.
- `check_nexus_artifact_updates` performs the same exact Nexus lineage check for
  a supplied inventory of `{mod_id, file_id}` records. Use it for Vortex exports,
  historical manifests, or existing installs that predate the tracking database.
  If the replacement file is also present in the supplied inventory, the older
  record is marked `superseded_in_inventory` instead of requesting another
  download.

## TCG artifact audit utilities

The repository includes two metadata-oriented utilities used to audit an
existing TCG Card Shop Simulator Nexus archive collection without installing or
launching the game.

- `scripts/build-tcg-audit-manifest.py` discovers `.zip`, `.rar`, and `.7z`
  archives, maps them to Nexus mod/file identities, merges duplicate local
  copies, and writes `download-manifest.json`, `previous-metadata/`, and an
  inventory summary. Numeric `MOD_ID-FILE_ID` names are recognized directly;
  reviewed `STAGED.md` mappings and the tracked legacy override file cover
  historical names.
- `scripts/tcg-update-audit.py` compares every selected artifact with saved or
  freshly fetched official Nexus metadata, follows explicit replacement chains,
  distinguishes unrelated sibling files, validates that every recorded archive
  copy exists with the expected size, and writes JSON, Markdown, and actionable
  replacement outputs.

<!-- markdownlint-disable-next-line MD013 -->
Discover an archive tree: `scripts/build-tcg-audit-manifest.py discover DOWNLOADS_ROOT AUDIT_INVENTORY`.

<!-- markdownlint-disable-next-line MD013 -->
Merge existing manifests: `scripts/build-tcg-audit-manifest.py merge AUDIT_INVENTORY MANIFEST...`.

<!-- markdownlint-disable-next-line MD013 -->
Run a reproducible audit from saved current metadata: `scripts/tcg-update-audit.py AUDIT_OUTPUT --manifest AUDIT_INVENTORY/download-manifest.json --current-metadata CURRENT_METADATA`.

The audit automatically reads historical snapshots from
`AUDIT_INVENTORY/previous-metadata/`. Omit `--current-metadata` to fetch a new
official metadata snapshot through the local MCP launcher instead.

## Secrets

The launcher sources `/var/home/jack/.bashrc.d/.secrets` and accepts any of
these variables:

- `NEXUS_API_KEY`
- `NEXUSMODS_API_KEY`
- `nexus_api_key`
- `nexux_api_key`
- `THUNDERSTORE_API_KEY`
- `CURSEFORGE_API_KEY`
- `STEAM_API_KEY`

`nexus_api_key` is the lowercase spelling used by the local secrets file.
The older misspelled `nexux_api_key` remains accepted as a legacy alias.

`THUNDERSTORE_API_KEY` is optional for public Thunderstore search/downloads,
but is sent as a bearer token when configured.

`CURSEFORGE_API_KEY` is required for CurseForge tools and is sent as the
official `x-api-key` header.

`STEAM_API_KEY` is required for Steam Workshop search. Published-file details
can often be read without it, but the MCP uses it when available.

## Mirror Preference

The server validates the account before choosing a download location. Premium
status is not assumed to be permanent.

- Premium account: prefer nearby premium mirrors in-country, then free mirrors
  in-country, then premium outside-country, then free outside-country.
- Non-premium account: prefer free mirrors first, with in-country mirrors before
  outside-country mirrors.

Environment variables:

- `NEXUS_COUNTRY` defaults to `US`.
- `NEXUS_PREFERRED_LOCATIONS` can be a comma-separated list such as
  `Chicago,Dallas,Los Angeles,Miami`.

## Tools

### Shared staging and tracking

- `get_download_staging` - reports shared staging and tracking state.
- `list_tracked_downloads` - lists downloads recorded across every provider.
- `check_tracked_updates` - checks tracked artifacts against official metadata.

### Nexus Mods

- `validate_api_key` - validates the Nexus API key and reports account
  capability fields.
- `find_games` - searches Nexus game names/domain names from `/v1/games.json`.
- `search_mods` - searches the official experimental Nexus API v3 mod catalog.
  Nexus currently exposes game/time filters and sorting but no server-side text
  query, so the MCP pages through catalog results and matches the requested
  terms locally against name, summary, version, author, and category. Searches
  are bounded by `max_pages` (default 20, maximum 100) and report whether the
  catalog was exhausted; use `sort=updated` for recent updates or increase the
  bound for older/less popular results.
- `get_mod` - fetches metadata for a known mod id.
- `list_mod_files` - lists files for a known mod id.
- `check_nexus_artifact_updates` - checks explicit Nexus replacement chains for
  a supplied artifact inventory without requiring tracking-database records.
- `get_download_links` - returns official temporary CDN links for a file id.
- `download_mod_file` - downloads a file using an official API-provided CDN
  link.

### Thunderstore

- `thunderstore_list_communities` - searches Thunderstore communities.
- `thunderstore_search_packages` - searches Thunderstore package listings,
  optionally scoped to a community.
- `thunderstore_get_package` - fetches package metadata by namespace and name.
- `thunderstore_get_package_version` - fetches package version metadata.
- `thunderstore_download_package` - downloads a package ZIP from its official
  `download_url`.

### CurseForge

- `curseforge_list_games` - searches CurseForge games visible to the API key.
- `curseforge_search_mods` - searches CurseForge mods by game id and filters.
- `curseforge_get_mod` - fetches CurseForge mod metadata by mod id.
- `curseforge_get_mod_files` - lists files for a CurseForge mod.
- `curseforge_get_download_url` - returns the official download URL for a file.
- `curseforge_download_file` - downloads a file from its official URL.

### Steam

- `steam_search_workshop` - searches Steam Workshop items with Steam Web API.
- `steam_get_workshop_details` - fetches published-file metadata.
- `steam_get_workshop_download_info` - returns `file_url` when Steam exposes it,
  plus a Steam subscribe URI.
- `steam_download_workshop_file` - downloads only when Steam exposes a direct
  `file_url`; otherwise it returns the official Steam subscribe URI.
