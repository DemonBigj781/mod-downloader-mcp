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
- `check_tracked_updates` checks the latest recorded download for every tracked
  mod/package/workshop item against current official provider metadata. It can
  filter by provider and optionally return only updates/errors. Nexus and
  CurseForge compare the mod page update timestamp captured at download time,
  avoiding false updates when a page hosts multiple independent files.

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
