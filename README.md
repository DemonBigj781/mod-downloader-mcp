# Mod Downloader MCP

Local MCP server for official mod-host APIs. It intentionally does not scrape
Nexus, Thunderstore, CurseForge, or Steam pages, so it stays out of Cloudflare
and anti-bot flows.

## Secrets

The launcher sources `/var/home/jack/.bashrc.d/.secrets` and accepts any of
these variables:

- `NEXUS_API_KEY`
- `NEXUSMODS_API_KEY`
- `nexux_api_key`
- `THUNDERSTORE_API_KEY`
- `CURSEFORGE_API_KEY`
- `STEAM_API_KEY`

`nexux_api_key` is supported because that is the current variable name in the
local secrets file.

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

### Nexus Mods

- `validate_api_key` - validates the Nexus API key and reports account
  capability fields.
- `find_games` - searches Nexus game names/domain names from `/v1/games.json`.
- `search_mods` - searches official API mod feeds for a game. Nexus API v1 has
  no full text mod search endpoint, so this checks `trending`, `latest_added`,
  `latest_updated`, and optionally updated periods.
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
