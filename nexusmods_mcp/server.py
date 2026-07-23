from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from nexusmods_mcp import __version__


API_ROOT = "https://api.nexusmods.com/v1"
THUNDERSTORE_ROOT = "https://thunderstore.io"
CURSEFORGE_ROOT = "https://api.curseforge.com"
STEAM_API_ROOT = "https://api.steampowered.com"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOWNLOAD_DIR = PROJECT_ROOT / "downloads"
MIRROR_COUNTRIES = {
    "amsterdam": "NL",
    "prague": "CZ",
    "chicago": "US",
    "los angeles": "US",
    "la": "US",
    "miami": "US",
    "dallas": "US",
    "nexus cdn": "GLOBAL",
    "nexus global content delivery network": "GLOBAL",
}
DEFAULT_US_LOCATIONS = ("Chicago", "Dallas", "Los Angeles", "Miami")


@dataclass(frozen=True)
class Config:
    api_key: str
    thunderstore_api_key: str
    curseforge_api_key: str
    steam_api_key: str
    country: str
    preferred_locations: tuple[str, ...]
    download_dir: Path
    max_result_chars: int

    @staticmethod
    def load() -> "Config":
        api_key = first_env("NEXUS_API_KEY", "NEXUSMODS_API_KEY", "nexux_api_key")
        if not api_key:
            raise RuntimeError(
                "Missing Nexus Mods API key. Set NEXUS_API_KEY, NEXUSMODS_API_KEY, "
                "or source /var/home/jack/.bashrc.d/.secrets for nexux_api_key."
            )
        country = os.environ.get("NEXUS_COUNTRY", "US").strip().upper() or "US"
        preferred = split_csv(os.environ.get("NEXUS_PREFERRED_LOCATIONS", ""))
        if country == "US" and not preferred:
            preferred = DEFAULT_US_LOCATIONS
        download_dir = Path(os.environ.get("NEXUS_DOWNLOAD_DIR", str(DEFAULT_DOWNLOAD_DIR))).expanduser()
        return Config(
            api_key=api_key,
            thunderstore_api_key=os.environ.get("THUNDERSTORE_API_KEY", "").strip(),
            curseforge_api_key=os.environ.get("CURSEFORGE_API_KEY", "").strip(),
            steam_api_key=os.environ.get("STEAM_API_KEY", "").strip(),
            country=country,
            preferred_locations=tuple(preferred),
            download_dir=download_dir,
            max_result_chars=int(os.environ.get("NEXUS_MCP_MAX_RESULT_CHARS", "16000")),
        )


class NexusClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._account: dict[str, Any] | None = None

    def validate_api_key(self) -> dict[str, Any]:
        account = self.request("/users/validate.json")
        if not isinstance(account, dict):
            raise RuntimeError("Unexpected validation response from Nexus Mods API")
        self._account = account
        return sanitize_account(account)

    def find_games(self, query: str = "", limit: int = 20, include_unapproved: bool = False) -> list[dict[str, Any]]:
        params = {"include_unapproved": str(bool(include_unapproved)).lower()}
        games = self.request("/games.json", params)
        if not isinstance(games, list):
            raise RuntimeError("Unexpected games response from Nexus Mods API")
        terms = normalize_terms(query)
        matches: list[dict[str, Any]] = []
        for game in games:
            haystack = " ".join(str(game.get(key, "")) for key in ("name", "domain_name", "genre")).lower()
            if terms and not all(term in haystack for term in terms):
                continue
            matches.append(slim_game(game))
            if len(matches) >= clamp(limit, 1, 100):
                break
        return matches

    def search_mods(
        self,
        game_domain_name: str,
        query: str = "",
        limit: int = 20,
        include_updated_periods: bool = True,
    ) -> dict[str, Any]:
        feeds: list[tuple[str, str, dict[str, str] | None]] = [
            ("trending", f"/games/{quote(game_domain_name)}/mods/trending.json", None),
            ("latest_added", f"/games/{quote(game_domain_name)}/mods/latest_added.json", None),
            ("latest_updated", f"/games/{quote(game_domain_name)}/mods/latest_updated.json", None),
        ]
        if include_updated_periods:
            for period in ("1d", "1w", "1m"):
                feeds.append((f"updated_{period}", f"/games/{quote(game_domain_name)}/mods/updated.json", {"period": period}))

        terms = normalize_terms(query)
        seen: set[int] = set()
        matches: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        max_matches = clamp(limit, 1, 100)
        for feed_name, path, params in feeds:
            try:
                mods = self.request(path, params)
            except RuntimeError as exc:
                errors.append({"feed": feed_name, "error": str(exc)})
                continue
            if not isinstance(mods, list):
                continue
            for mod in mods:
                mod_id = int(mod.get("mod_id") or mod.get("id") or 0)
                if mod_id in seen:
                    continue
                haystack = mod_text(mod)
                if terms and not all(term in haystack for term in terms):
                    continue
                slim = slim_mod(mod)
                slim["matched_feed"] = feed_name
                matches.append(slim)
                seen.add(mod_id)
                if len(matches) >= max_matches:
                    return search_payload(game_domain_name, query, matches, errors)
        return search_payload(game_domain_name, query, matches, errors)

    def get_mod(self, game_domain_name: str, mod_id: int) -> dict[str, Any]:
        mod = self.request(f"/games/{quote(game_domain_name)}/mods/{int(mod_id)}.json")
        if not isinstance(mod, dict):
            raise RuntimeError("Unexpected mod response from Nexus Mods API")
        return mod

    def list_mod_files(self, game_domain_name: str, mod_id: int, category: str = "") -> dict[str, Any]:
        params = {"category": category} if category else None
        files = self.request(f"/games/{quote(game_domain_name)}/mods/{int(mod_id)}/files.json", params)
        if not isinstance(files, dict):
            raise RuntimeError("Unexpected files response from Nexus Mods API")
        return files

    def get_download_links(self, game_domain_name: str, mod_id: int, file_id: int) -> dict[str, Any]:
        links = self._download_links(game_domain_name, mod_id, file_id)
        account = self.account()
        ranked = rank_links(links, account, self.config.country, self.config.preferred_locations)
        return {
            "game_domain_name": game_domain_name,
            "mod_id": int(mod_id),
            "file_id": int(file_id),
            "account": sanitize_account(account),
            "country": self.config.country,
            "preferred_locations": list(self.config.preferred_locations),
            "links": ranked,
        }

    def download_mod_file(
        self,
        game_domain_name: str,
        mod_id: int,
        file_id: int,
        output_dir: str = "",
        filename: str = "",
    ) -> dict[str, Any]:
        link_payload = self.get_download_links(game_domain_name, mod_id, file_id)
        links = link_payload["links"]
        if not links:
            raise RuntimeError("No official Nexus download links were returned for this file")
        selected = links[0]
        target_dir = Path(output_dir).expanduser() if output_dir else self.config.download_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target_name = filename or filename_from_uri(selected["URI"]) or f"{game_domain_name}-{mod_id}-{file_id}.download"
        target_path = unique_path(target_dir / safe_filename(target_name))
        started = time.time()
        size = download_uri(selected["URI"], target_path)
        return {
            "downloaded": str(target_path),
            "bytes": size,
            "duration_seconds": round(time.time() - started, 2),
            "selected_mirror": {key: selected[key] for key in selected if key != "URI"},
            "selection_reason": selected.get("selection_reason", ""),
        }

    def account(self) -> dict[str, Any]:
        if self._account is None:
            account = self.request("/users/validate.json")
            if not isinstance(account, dict):
                raise RuntimeError("Unexpected validation response from Nexus Mods API")
            self._account = account
        return self._account

    def _download_links(self, game_domain_name: str, mod_id: int, file_id: int) -> list[dict[str, Any]]:
        links = self.request(
            f"/games/{quote(game_domain_name)}/mods/{int(mod_id)}/files/{int(file_id)}/download_link.json"
        )
        if not isinstance(links, list):
            raise RuntimeError("Unexpected download link response from Nexus Mods API")
        return [dict(link) for link in links if isinstance(link, dict) and link.get("URI")]

    def request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = f"?{urlencode(params)}" if params else ""
        url = f"{API_ROOT}{path}{query}"
        req = Request(
            url,
            headers={
                "apikey": self.config.api_key,
                "Accept": "application/json",
                "User-Agent": "nexusmods-mcp/0.1.0",
            },
        )
        try:
            with urlopen(req, timeout=60) as resp:
                body = resp.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"Nexus API HTTP {exc.code} for {path}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Nexus API request failed for {path}: {exc}") from exc
        return json.loads(body.decode("utf-8"))


class ThunderstoreClient:
    def __init__(self, config: Config) -> None:
        self.config = config

    def list_communities(self, query: str = "", limit: int = 20) -> dict[str, Any]:
        terms = normalize_terms(query)
        matches: list[dict[str, Any]] = []
        cursor = ""
        pages_scanned = 0
        next_url = None
        max_matches = clamp(limit, 1, 100)
        while pages_scanned < 20 and len(matches) < max_matches:
            payload = self.request("/api/experimental/community/", {"cursor": cursor} if cursor else None)
            next_url = payload.get("next") if isinstance(payload, dict) else None
            for community in page_results(payload):
                haystack = json.dumps(community, ensure_ascii=False).lower()
                if terms and not all(term in haystack for term in terms):
                    continue
                matches.append(community)
                if len(matches) >= max_matches:
                    break
            pages_scanned += 1
            cursor = cursor_from_next(next_url)
            if not cursor:
                break
        return {
            "query": query,
            "matches": matches,
            "pages_scanned": pages_scanned,
            "api_key_configured": bool(self.config.thunderstore_api_key),
        }

    def search_packages(
        self, query: str = "", community: str = "", limit: int = 20, cursor: str = ""
    ) -> dict[str, Any]:
        path = "/api/experimental/package/"
        params = {"cursor": cursor} if cursor else None
        terms = normalize_terms(query)
        matches: list[dict[str, Any]] = []
        next_url = None
        pages_scanned = 0
        max_matches = clamp(limit, 1, 100)
        while pages_scanned < 20 and len(matches) < max_matches:
            params = {"cursor": cursor} if cursor else None
            payload = self.request(path, params)
            next_url = payload.get("next") if isinstance(payload, dict) else None
            for package in page_results(payload):
                haystack = thunderstore_package_text(package)
                if community and community.lower() not in haystack:
                    continue
                if terms and not all(term in haystack for term in terms):
                    continue
                matches.append(slim_thunderstore_package(package))
                if len(matches) >= max_matches:
                    break
            pages_scanned += 1
            cursor = cursor_from_next(next_url)
            if not cursor:
                break
        return {
            "query": query,
            "community": community,
            "next": next_url,
            "pages_scanned": pages_scanned,
            "matches": matches,
        }

    def get_package(self, namespace: str, name: str) -> dict[str, Any]:
        payload = self.request(f"/api/experimental/package/{quote(namespace)}/{quote(name)}/")
        if not isinstance(payload, dict):
            raise RuntimeError("Unexpected Thunderstore package response")
        return payload

    def get_package_version(self, namespace: str, name: str, version: str) -> dict[str, Any]:
        payload = self.request(f"/api/experimental/package/{quote(namespace)}/{quote(name)}/{quote(version)}/")
        if not isinstance(payload, dict):
            raise RuntimeError("Unexpected Thunderstore package version response")
        return payload

    def download_package(
        self,
        namespace: str,
        name: str,
        version: str = "",
        output_dir: str = "",
        filename: str = "",
    ) -> dict[str, Any]:
        version_payload = self.get_package_version(namespace, name, version) if version else self.latest_version(namespace, name)
        download_url = str(version_payload.get("download_url", ""))
        if not download_url:
            raise RuntimeError("Thunderstore package version did not include download_url")
        target_dir = Path(output_dir).expanduser() if output_dir else self.config.download_dir / "thunderstore"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_name = filename or filename_from_uri(download_url) or f"{namespace}-{name}-{version_payload.get('version_number', 'latest')}.zip"
        target_path = unique_path(target_dir / safe_filename(target_name))
        started = time.time()
        size = download_uri(download_url, target_path)
        return {
            "downloaded": str(target_path),
            "bytes": size,
            "duration_seconds": round(time.time() - started, 2),
            "namespace": namespace,
            "name": name,
            "version": version_payload.get("version_number", version),
            "download_url": download_url,
        }

    def latest_version(self, namespace: str, name: str) -> dict[str, Any]:
        package = self.get_package(namespace, name)
        latest = package.get("latest")
        if not isinstance(latest, dict):
            raise RuntimeError("Thunderstore package did not include latest version metadata")
        return latest

    def request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = f"?{urlencode(params)}" if params else ""
        url = f"{THUNDERSTORE_ROOT}{path}{query}"
        headers = {"Accept": "application/json", "User-Agent": "nexusmods-mcp/0.1.0"}
        if self.config.thunderstore_api_key:
            headers["Authorization"] = f"Bearer {self.config.thunderstore_api_key}"
        req = Request(url, headers=headers)
        try:
            with urlopen(req, timeout=60) as resp:
                body = resp.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"Thunderstore API HTTP {exc.code} for {path}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Thunderstore API request failed for {path}: {exc}") from exc
        return json.loads(body.decode("utf-8"))


class CurseForgeClient:
    def __init__(self, config: Config) -> None:
        self.config = config

    def list_games(self, query: str = "", limit: int = 20, index: int = 0) -> dict[str, Any]:
        terms = normalize_terms(query)
        matches: list[dict[str, Any]] = []
        page_size = 50 if terms else clamp(limit, 1, 50)
        current_index = int(index)
        pages_scanned = 0
        pagination = None
        max_matches = clamp(limit, 1, 100)
        while pages_scanned < 20 and len(matches) < max_matches:
            payload = self.request("/v1/games", {"index": current_index, "pageSize": page_size})
            pagination = payload.get("pagination")
            for game in curseforge_data_list(payload):
                haystack = json.dumps(game, ensure_ascii=False).lower()
                if terms and not all(term in haystack for term in terms):
                    continue
                matches.append(slim_curseforge_game(game))
                if len(matches) >= max_matches:
                    break
            pages_scanned += 1
            page = dict_or_empty(pagination)
            result_count = int(page.get("resultCount") or 0)
            total_count = int(page.get("totalCount") or 0)
            current_index += page_size
            if result_count <= 0 or current_index >= total_count:
                break
        return {"query": query, "matches": matches, "pagination": pagination, "pages_scanned": pages_scanned}

    def search_mods(
        self,
        game_id: int,
        query: str = "",
        class_id: int = 0,
        category_id: int = 0,
        sort_field: int = 0,
        sort_order: str = "desc",
        index: int = 0,
        page_size: int = 20,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "gameId": int(game_id),
            "index": int(index),
            "pageSize": clamp(page_size, 1, 50),
        }
        if query:
            params["searchFilter"] = query
        if class_id:
            params["classId"] = int(class_id)
        if category_id:
            params["categoryId"] = int(category_id)
        if sort_field:
            params["sortField"] = int(sort_field)
            params["sortOrder"] = sort_order if sort_order in {"asc", "desc"} else "desc"
        payload = self.request("/v1/mods/search", params)
        return {
            "query": query,
            "game_id": int(game_id),
            "matches": [slim_curseforge_mod(mod) for mod in curseforge_data_list(payload)],
            "pagination": payload.get("pagination"),
        }

    def get_mod(self, mod_id: int) -> dict[str, Any]:
        payload = self.request(f"/v1/mods/{int(mod_id)}")
        return curseforge_data_dict(payload)

    def get_mod_files(self, mod_id: int, index: int = 0, page_size: int = 20) -> dict[str, Any]:
        payload = self.request(
            f"/v1/mods/{int(mod_id)}/files",
            {"index": int(index), "pageSize": clamp(page_size, 1, 50)},
        )
        return {
            "mod_id": int(mod_id),
            "files": [slim_curseforge_file(file) for file in curseforge_data_list(payload)],
            "pagination": payload.get("pagination"),
        }

    def get_download_url(self, mod_id: int, file_id: int) -> dict[str, Any]:
        payload = self.request(f"/v1/mods/{int(mod_id)}/files/{int(file_id)}/download-url")
        url = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(url, str) or not url:
            raise RuntimeError("CurseForge did not return a download URL for this file")
        return {"mod_id": int(mod_id), "file_id": int(file_id), "download_url": url}

    def download_file(self, mod_id: int, file_id: int, output_dir: str = "", filename: str = "") -> dict[str, Any]:
        payload = self.get_download_url(mod_id, file_id)
        download_url = payload["download_url"]
        target_dir = Path(output_dir).expanduser() if output_dir else self.config.download_dir / "curseforge"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_name = filename or filename_from_uri(download_url) or f"curseforge-{mod_id}-{file_id}.download"
        target_path = unique_path(target_dir / safe_filename(target_name))
        started = time.time()
        size = download_uri(download_url, target_path)
        return {
            "downloaded": str(target_path),
            "bytes": size,
            "duration_seconds": round(time.time() - started, 2),
            "mod_id": int(mod_id),
            "file_id": int(file_id),
            "download_url": download_url,
        }

    def request(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.config.curseforge_api_key:
            raise RuntimeError("Missing CURSEFORGE_API_KEY in environment or sourced .secrets")
        query = f"?{urlencode(params)}" if params else ""
        url = f"{CURSEFORGE_ROOT}{path}{query}"
        req = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "nexusmods-mcp/0.1.0",
                "x-api-key": self.config.curseforge_api_key,
            },
        )
        try:
            with urlopen(req, timeout=60) as resp:
                body = resp.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"CurseForge API HTTP {exc.code} for {path}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"CurseForge API request failed for {path}: {exc}") from exc
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("Unexpected CurseForge API response")
        return payload


class SteamClient:
    def __init__(self, config: Config) -> None:
        self.config = config

    def search_workshop(self, app_id: int, query: str = "", page: int = 1, page_size: int = 20) -> dict[str, Any]:
        if not self.config.steam_api_key:
            raise RuntimeError("Missing STEAM_API_KEY in environment or sourced .secrets")
        params: dict[str, Any] = {
            "key": self.config.steam_api_key,
            "appid": int(app_id),
            "page": max(1, int(page)),
            "numperpage": clamp(page_size, 1, 50),
            "return_metadata": "true",
            "return_children": "true",
            "return_tags": "true",
        }
        if query:
            params["search_text"] = query
        payload = self.request_get("/IPublishedFileService/QueryFiles/v1/", params)
        response = dict_or_empty(payload.get("response"))
        details = list_or_empty(response.get("publishedfiledetails"))
        return {
            "app_id": int(app_id),
            "query": query,
            "total": response.get("total"),
            "details": [slim_steam_workshop_item(item) for item in details if isinstance(item, dict)],
        }

    def get_workshop_details(self, published_file_ids: list[int]) -> dict[str, Any]:
        ids = [int(value) for value in published_file_ids[:50]]
        if not ids:
            raise RuntimeError("At least one published_file_id is required")
        data: dict[str, Any] = {"itemcount": len(ids)}
        for index, published_file_id in enumerate(ids):
            data[f"publishedfileids[{index}]"] = published_file_id
        payload = self.request_post("/ISteamRemoteStorage/GetPublishedFileDetails/v1/", data)
        response = dict_or_empty(payload.get("response"))
        details = list_or_empty(response.get("publishedfiledetails"))
        return {"details": [slim_steam_workshop_item(item) for item in details if isinstance(item, dict)]}

    def get_workshop_download_info(self, published_file_id: int) -> dict[str, Any]:
        payload = self.get_workshop_details([int(published_file_id)])
        details = payload["details"]
        if not details:
            raise RuntimeError("Steam did not return details for that published file id")
        item = details[0]
        file_url = item.get("file_url")
        return {
            "published_file_id": int(published_file_id),
            "file_url": file_url,
            "steam_url": item.get("url"),
            "steam_subscribe_uri": f"steam://url/CommunityFilePage/{int(published_file_id)}",
            "note": "Steam Workshop often omits direct file_url; use the Steam URI or Steam client subscription when absent.",
            "item": item,
        }

    def download_workshop_file(self, published_file_id: int, output_dir: str = "", filename: str = "") -> dict[str, Any]:
        info = self.get_workshop_download_info(published_file_id)
        file_url = info.get("file_url")
        if not isinstance(file_url, str) or not file_url:
            return {"downloaded": None, **info}
        target_dir = Path(output_dir).expanduser() if output_dir else self.config.download_dir / "steam"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_name = filename or filename_from_uri(file_url) or f"steam-workshop-{published_file_id}.download"
        target_path = unique_path(target_dir / safe_filename(target_name))
        started = time.time()
        size = download_uri(file_url, target_path)
        return {
            "downloaded": str(target_path),
            "bytes": size,
            "duration_seconds": round(time.time() - started, 2),
            **info,
        }

    def request_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{STEAM_API_ROOT}{path}?{urlencode(params)}"
        return request_json(url, {"Accept": "application/json", "User-Agent": "nexusmods-mcp/0.1.0"})

    def request_post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        url = f"{STEAM_API_ROOT}{path}"
        encoded = urlencode(data).encode("utf-8")
        return request_json(url, {"Accept": "application/json", "User-Agent": "nexusmods-mcp/0.1.0"}, encoded)


class McpServer:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.client = NexusClient(config)
        self.thunderstore = ThunderstoreClient(config)
        self.curseforge = CurseForgeClient(config)
        self.steam = SteamClient(config)
        self.tools: dict[str, tuple[str, dict[str, Any], Callable[[dict[str, Any]], Any]]] = {
            "validate_api_key": (
                "Validate the configured Nexus Mods API key and show account capability fields.",
                {"type": "object", "properties": {}},
                lambda _args: self.client.validate_api_key(),
            ),
            "find_games": (
                "Find Nexus game domain names by game title, domain, or genre.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                        "include_unapproved": {"type": "boolean"},
                    },
                },
                lambda args: self.client.find_games(
                    str(args.get("query", "")),
                    int(args.get("limit", 20)),
                    bool(args.get("include_unapproved", False)),
                ),
            ),
            "search_mods": (
                "Search official Nexus API mod feeds for a game; Nexus API v1 has no broad text search endpoint.",
                {
                    "type": "object",
                    "required": ["game_domain_name"],
                    "properties": {
                        "game_domain_name": {"type": "string"},
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                        "include_updated_periods": {"type": "boolean"},
                    },
                },
                lambda args: self.client.search_mods(
                    str(args["game_domain_name"]),
                    str(args.get("query", "")),
                    int(args.get("limit", 20)),
                    bool(args.get("include_updated_periods", True)),
                ),
            ),
            "get_mod": (
                "Fetch metadata for a known Nexus mod id.",
                {
                    "type": "object",
                    "required": ["game_domain_name", "mod_id"],
                    "properties": {"game_domain_name": {"type": "string"}, "mod_id": {"type": "integer"}},
                },
                lambda args: self.client.get_mod(str(args["game_domain_name"]), int(args["mod_id"])),
            ),
            "list_mod_files": (
                "List files for a known Nexus mod id.",
                {
                    "type": "object",
                    "required": ["game_domain_name", "mod_id"],
                    "properties": {
                        "game_domain_name": {"type": "string"},
                        "mod_id": {"type": "integer"},
                        "category": {"type": "string"},
                    },
                },
                lambda args: self.client.list_mod_files(
                    str(args["game_domain_name"]), int(args["mod_id"]), str(args.get("category", ""))
                ),
            ),
            "get_download_links": (
                "Get official temporary Nexus CDN links, ranked by premium/free and location preference.",
                download_schema(),
                lambda args: self.client.get_download_links(
                    str(args["game_domain_name"]), int(args["mod_id"]), int(args["file_id"])
                ),
            ),
            "download_mod_file": (
                "Download a Nexus mod file through the best official API-provided CDN link.",
                {
                    **download_schema(),
                    "properties": {
                        **download_schema()["properties"],
                        "output_dir": {"type": "string"},
                        "filename": {"type": "string"},
                    },
                },
                lambda args: self.client.download_mod_file(
                    str(args["game_domain_name"]),
                    int(args["mod_id"]),
                    int(args["file_id"]),
                    str(args.get("output_dir", "")),
                    str(args.get("filename", "")),
                ),
            ),
            "thunderstore_list_communities": (
                "List/search Thunderstore communities using the official API.",
                {
                    "type": "object",
                    "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                },
                lambda args: self.thunderstore.list_communities(str(args.get("query", "")), int(args.get("limit", 20))),
            ),
            "thunderstore_search_packages": (
                "Search Thunderstore package listings, optionally scoped to a community identifier.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "community": {"type": "string"},
                        "limit": {"type": "integer"},
                        "cursor": {"type": "string"},
                    },
                },
                lambda args: self.thunderstore.search_packages(
                    str(args.get("query", "")),
                    str(args.get("community", "")),
                    int(args.get("limit", 20)),
                    str(args.get("cursor", "")),
                ),
            ),
            "thunderstore_get_package": (
                "Fetch Thunderstore package metadata by namespace and package name.",
                thunderstore_package_schema(required_version=False),
                lambda args: self.thunderstore.get_package(str(args["namespace"]), str(args["name"])),
            ),
            "thunderstore_get_package_version": (
                "Fetch Thunderstore package version metadata by namespace, package name, and version.",
                thunderstore_package_schema(required_version=True),
                lambda args: self.thunderstore.get_package_version(
                    str(args["namespace"]), str(args["name"]), str(args["version"])
                ),
            ),
            "thunderstore_download_package": (
                "Download a Thunderstore package ZIP from its official download_url.",
                {
                    **thunderstore_package_schema(required_version=False),
                    "properties": {
                        **thunderstore_package_schema(required_version=False)["properties"],
                        "version": {"type": "string"},
                        "output_dir": {"type": "string"},
                        "filename": {"type": "string"},
                    },
                },
                lambda args: self.thunderstore.download_package(
                    str(args["namespace"]),
                    str(args["name"]),
                    str(args.get("version", "")),
                    str(args.get("output_dir", "")),
                    str(args.get("filename", "")),
                ),
            ),
            "curseforge_list_games": (
                "List/search CurseForge games available to the configured API key.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                        "index": {"type": "integer"},
                    },
                },
                lambda args: self.curseforge.list_games(
                    str(args.get("query", "")), int(args.get("limit", 20)), int(args.get("index", 0))
                ),
            ),
            "curseforge_search_mods": (
                "Search CurseForge mods by game id and optional filters.",
                {
                    "type": "object",
                    "required": ["game_id"],
                    "properties": {
                        "game_id": {"type": "integer"},
                        "query": {"type": "string"},
                        "class_id": {"type": "integer"},
                        "category_id": {"type": "integer"},
                        "sort_field": {"type": "integer"},
                        "sort_order": {"type": "string"},
                        "index": {"type": "integer"},
                        "page_size": {"type": "integer"},
                    },
                },
                lambda args: self.curseforge.search_mods(
                    int(args["game_id"]),
                    str(args.get("query", "")),
                    int(args.get("class_id", 0)),
                    int(args.get("category_id", 0)),
                    int(args.get("sort_field", 0)),
                    str(args.get("sort_order", "desc")),
                    int(args.get("index", 0)),
                    int(args.get("page_size", 20)),
                ),
            ),
            "curseforge_get_mod": (
                "Fetch CurseForge mod metadata by mod id.",
                {"type": "object", "required": ["mod_id"], "properties": {"mod_id": {"type": "integer"}}},
                lambda args: self.curseforge.get_mod(int(args["mod_id"])),
            ),
            "curseforge_get_mod_files": (
                "List CurseForge files for a mod id.",
                {
                    "type": "object",
                    "required": ["mod_id"],
                    "properties": {
                        "mod_id": {"type": "integer"},
                        "index": {"type": "integer"},
                        "page_size": {"type": "integer"},
                    },
                },
                lambda args: self.curseforge.get_mod_files(
                    int(args["mod_id"]), int(args.get("index", 0)), int(args.get("page_size", 20))
                ),
            ),
            "curseforge_get_download_url": (
                "Get an official CurseForge download URL for a mod file.",
                curseforge_file_schema(),
                lambda args: self.curseforge.get_download_url(int(args["mod_id"]), int(args["file_id"])),
            ),
            "curseforge_download_file": (
                "Download a CurseForge mod file from its official download URL.",
                {
                    **curseforge_file_schema(),
                    "properties": {
                        **curseforge_file_schema()["properties"],
                        "output_dir": {"type": "string"},
                        "filename": {"type": "string"},
                    },
                },
                lambda args: self.curseforge.download_file(
                    int(args["mod_id"]),
                    int(args["file_id"]),
                    str(args.get("output_dir", "")),
                    str(args.get("filename", "")),
                ),
            ),
            "steam_search_workshop": (
                "Search Steam Workshop items with the Steam Web API.",
                {
                    "type": "object",
                    "required": ["app_id"],
                    "properties": {
                        "app_id": {"type": "integer"},
                        "query": {"type": "string"},
                        "page": {"type": "integer"},
                        "page_size": {"type": "integer"},
                    },
                },
                lambda args: self.steam.search_workshop(
                    int(args["app_id"]), str(args.get("query", "")), int(args.get("page", 1)), int(args.get("page_size", 20))
                ),
            ),
            "steam_get_workshop_details": (
                "Fetch Steam Workshop published-file metadata for one or more ids.",
                {
                    "type": "object",
                    "required": ["published_file_ids"],
                    "properties": {"published_file_ids": {"type": "array", "items": {"type": "integer"}}},
                },
                lambda args: self.steam.get_workshop_details([int(value) for value in args["published_file_ids"]]),
            ),
            "steam_get_workshop_download_info": (
                "Return direct Steam Workshop file_url when exposed, plus official Steam subscribe URI.",
                steam_file_schema(),
                lambda args: self.steam.get_workshop_download_info(int(args["published_file_id"])),
            ),
            "steam_download_workshop_file": (
                "Best-effort Steam Workshop download: downloads direct file_url when Steam exposes one.",
                {
                    **steam_file_schema(),
                    "properties": {
                        **steam_file_schema()["properties"],
                        "output_dir": {"type": "string"},
                        "filename": {"type": "string"},
                    },
                },
                lambda args: self.steam.download_workshop_file(
                    int(args["published_file_id"]), str(args.get("output_dir", "")), str(args.get("filename", ""))
                ),
            ),
        }

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if "id" not in message:
            return None
        method = message.get("method")
        try:
            if method == "initialize":
                requested_version = message.get("params", {}).get("protocolVersion", "2024-11-05")
                return self.result(
                    message,
                    {
                        "protocolVersion": requested_version,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "nexusmods-mcp", "version": __version__},
                    },
                )
            if method == "tools/list":
                return self.result(message, {"tools": self._tool_descriptions()})
            if method == "tools/call":
                params = message.get("params", {})
                name = params.get("name")
                args = params.get("arguments") or {}
                if name not in self.tools:
                    raise ValueError(f"Unknown tool: {name}")
                payload = self.tools[name][2](args)
                return self.result(
                    message,
                    {"content": [{"type": "text", "text": truncate_payload(payload, self.config.max_result_chars)}]},
                )
            return self.error(message, -32601, f"Method not found: {method}")
        except Exception as exc:
            return self.error(message, -32000, str(exc))

    def _tool_descriptions(self) -> list[dict[str, Any]]:
        return [
            {"name": name, "description": desc, "inputSchema": schema}
            for name, (desc, schema, _handler) in self.tools.items()
        ]

    @staticmethod
    def result(message: dict[str, Any], result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": message["id"], "result": result}

    @staticmethod
    def error(message: dict[str, Any], code: int, text: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": code, "message": text}}


def download_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["game_domain_name", "mod_id", "file_id"],
        "properties": {
            "game_domain_name": {"type": "string"},
            "mod_id": {"type": "integer"},
            "file_id": {"type": "integer"},
        },
    }


def thunderstore_package_schema(required_version: bool) -> dict[str, Any]:
    required = ["namespace", "name"]
    if required_version:
        required.append("version")
    return {
        "type": "object",
        "required": required,
        "properties": {
            "namespace": {"type": "string"},
            "name": {"type": "string"},
            "version": {"type": "string"},
        },
    }


def curseforge_file_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["mod_id", "file_id"],
        "properties": {"mod_id": {"type": "integer"}, "file_id": {"type": "integer"}},
    }


def steam_file_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["published_file_id"],
        "properties": {"published_file_id": {"type": "integer"}},
    }


def rank_links(
    links: list[dict[str, Any]], account: dict[str, Any], country: str, preferred_locations: tuple[str, ...]
) -> list[dict[str, Any]]:
    premium_account = is_premium(account)
    enriched = [enrich_link(link, index, country, preferred_locations) for index, link in enumerate(links)]

    def order(link: dict[str, Any]) -> tuple[int, int, int, str]:
        in_country = bool(link["in_country"])
        global_link = bool(link["global"])
        premium = bool(link["premium"])
        if premium_account:
            if premium and in_country:
                group = 0
            elif (not premium) and in_country:
                group = 1
            elif premium:
                group = 2
            elif global_link:
                group = 3
            else:
                group = 4
        else:
            if (not premium) and in_country:
                group = 0
            elif (not premium) and global_link:
                group = 1
            elif not premium:
                group = 2
            elif premium and in_country:
                group = 3
            else:
                group = 4
        return (group, int(link["location_rank"]), int(link["original_index"]), str(link.get("name", "")))

    ranked = sorted(enriched, key=order)
    for idx, link in enumerate(ranked, start=1):
        link["rank"] = idx
        link["selection_reason"] = mirror_reason(link, premium_account)
    return ranked


def enrich_link(
    link: dict[str, Any], original_index: int, country: str, preferred_locations: tuple[str, ...]
) -> dict[str, Any]:
    result = dict(link)
    text = f"{link.get('name', '')} {link.get('short_name', '')} {link.get('URI', '')}".lower()
    result["premium"] = "premium" in text or "premium" in urlparse(str(link.get("URI", ""))).netloc.lower()
    result["country"] = mirror_country(text)
    result["global"] = result["country"] == "GLOBAL"
    result["in_country"] = result["country"] == country
    result["location_rank"] = location_rank(text, preferred_locations)
    result["original_index"] = original_index
    return result


def mirror_country(text: str) -> str:
    for needle, country in MIRROR_COUNTRIES.items():
        if needle in text:
            return country
    return "UNKNOWN"


def location_rank(text: str, preferred_locations: tuple[str, ...]) -> int:
    for index, location in enumerate(preferred_locations):
        if location.lower() in text:
            return index
    return len(preferred_locations) + 100


def mirror_reason(link: dict[str, Any], premium_account: bool) -> str:
    account = "premium account" if premium_account else "non-premium account"
    tier = "premium mirror" if link.get("premium") else "free mirror"
    if link.get("in_country"):
        place = "in configured country"
    elif link.get("global"):
        place = "global CDN"
    else:
        place = "outside configured country"
    return f"Selected order for {account}: {tier}, {place}."


def is_premium(account: dict[str, Any]) -> bool:
    for key in ("is_premium", "is_premium?"):
        value = account.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "1", "yes"}:
            return True
    return False


def sanitize_account(account: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in account.items() if key.lower() not in {"key", "email"}}


def slim_game(game: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": game.get("id"),
        "name": game.get("name"),
        "domain_name": game.get("domain_name"),
        "genre": game.get("genre"),
        "mods": game.get("mods"),
        "downloads": game.get("downloads"),
        "nexusmods_url": game.get("nexusmods_url"),
    }


def slim_mod(mod: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "mod_id",
        "name",
        "summary",
        "version",
        "author",
        "category_id",
        "endorsement_count",
        "download_count",
        "updated_time",
        "mod_downloads",
        "domain_name",
        "nexusmods_url",
    )
    return {key: mod.get(key) for key in keys if key in mod}


def slim_thunderstore_package(package: dict[str, Any]) -> dict[str, Any]:
    latest = dict_or_empty(package.get("latest"))
    listings = list_or_empty(package.get("community_listings"))
    return {
        "namespace": package.get("namespace"),
        "name": package.get("name"),
        "full_name": package.get("full_name"),
        "owner": package.get("owner"),
        "package_url": package.get("package_url"),
        "is_deprecated": package.get("is_deprecated"),
        "total_downloads": package.get("total_downloads"),
        "rating_score": package.get("rating_score"),
        "latest": {
            "version_number": latest.get("version_number"),
            "description": latest.get("description"),
            "download_url": latest.get("download_url"),
            "downloads": latest.get("downloads"),
            "dependencies": latest.get("dependencies", []),
            "date_created": latest.get("date_created"),
            "website_url": latest.get("website_url"),
            "is_active": latest.get("is_active"),
        },
        "communities": [listing.get("community") for listing in listings if isinstance(listing, dict)],
        "categories": sorted(
            {
                category
                for listing in listings
                if isinstance(listing, dict)
                for category in listing.get("categories", [])
                if isinstance(category, str)
            }
        ),
    }


def curseforge_data_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, list):
        raise RuntimeError("Unexpected CurseForge list response")
    return [item for item in data if isinstance(item, dict)]


def curseforge_data_dict(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected CurseForge object response")
    return data


def slim_curseforge_game(game: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": game.get("id"),
        "name": game.get("name"),
        "slug": game.get("slug"),
        "dateModified": game.get("dateModified"),
        "status": game.get("status"),
        "apiStatus": game.get("apiStatus"),
    }


def slim_curseforge_mod(mod: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": mod.get("id"),
        "gameId": mod.get("gameId"),
        "name": mod.get("name"),
        "slug": mod.get("slug"),
        "summary": mod.get("summary"),
        "downloadCount": mod.get("downloadCount"),
        "isAvailable": mod.get("isAvailable"),
        "mainFileId": mod.get("mainFileId"),
        "dateModified": mod.get("dateModified"),
        "dateReleased": mod.get("dateReleased"),
        "websiteUrl": dict_or_empty(mod.get("links")).get("websiteUrl"),
        "authors": [author.get("name") for author in list_or_empty(mod.get("authors")) if isinstance(author, dict)],
        "latestFiles": [slim_curseforge_file(file) for file in list_or_empty(mod.get("latestFiles")) if isinstance(file, dict)][:5],
    }


def slim_curseforge_file(file: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": file.get("id"),
        "gameId": file.get("gameId"),
        "modId": file.get("modId"),
        "displayName": file.get("displayName"),
        "fileName": file.get("fileName"),
        "fileDate": file.get("fileDate"),
        "fileLength": file.get("fileLength"),
        "downloadCount": file.get("downloadCount"),
        "downloadUrl": file.get("downloadUrl"),
        "gameVersions": file.get("gameVersions", []),
        "isAvailable": file.get("isAvailable"),
    }


def slim_steam_workshop_item(item: dict[str, Any]) -> dict[str, Any]:
    published_file_id = item.get("publishedfileid") or item.get("published_file_id")
    return {
        "published_file_id": published_file_id,
        "result": item.get("result"),
        "title": item.get("title"),
        "description": item.get("description"),
        "creator": item.get("creator"),
        "consumer_app_id": item.get("consumer_app_id") or item.get("consumer_appid"),
        "filename": item.get("filename"),
        "file_size": item.get("file_size"),
        "file_url": item.get("file_url"),
        "preview_url": item.get("preview_url"),
        "time_created": item.get("time_created"),
        "time_updated": item.get("time_updated"),
        "subscriptions": item.get("subscriptions"),
        "favorited": item.get("favorited"),
        "url": f"https://steamcommunity.com/sharedfiles/filedetails/?id={published_file_id}" if published_file_id else None,
    }


def page_results(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return [item for item in payload["results"] if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    raise RuntimeError("Unexpected paginated Thunderstore response")


def thunderstore_package_text(package: dict[str, Any]) -> str:
    latest = dict_or_empty(package.get("latest"))
    listings = list_or_empty(package.get("community_listings"))
    fields = [
        package.get("namespace", ""),
        package.get("name", ""),
        package.get("full_name", ""),
        package.get("owner", ""),
        latest.get("description", ""),
        latest.get("version_number", ""),
        " ".join(str(dep) for dep in latest.get("dependencies", [])),
    ]
    for listing in listings:
        if not isinstance(listing, dict):
            continue
        fields.append(str(listing.get("community", "")))
        fields.extend(str(category) for category in listing.get("categories", []))
    return " ".join(str(field) for field in fields).lower()


def dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def list_or_empty(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def cursor_from_next(next_url: Any) -> str:
    if not isinstance(next_url, str) or not next_url:
        return ""
    values = parse_qs(urlparse(next_url).query).get("cursor", [])
    return values[0] if values else ""


def search_payload(game_domain_name: str, query: str, matches: list[dict[str, Any]], errors: list[dict[str, str]]) -> dict[str, Any]:
    note = "Nexus Mods API v1 does not expose broad full-text mod search; searched official mod feeds only."
    return {"game_domain_name": game_domain_name, "query": query, "note": note, "matches": matches, "errors": errors}


def mod_text(mod: dict[str, Any]) -> str:
    fields = ("name", "summary", "description", "author", "version")
    return " ".join(str(mod.get(field, "")) for field in fields).lower()


def normalize_terms(query: str) -> list[str]:
    return [term.lower() for term in re.findall(r"[A-Za-z0-9_.+-]+", query) if term]


def first_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value.strip().strip('"').strip("'")
    return ""


def split_csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def truncate_payload(value: Any, max_chars: int) -> str:
    text = json.dumps(value, indent=2, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... truncated ..."


def filename_from_uri(uri: str) -> str:
    parsed = urlparse(uri)
    return Path(parsed.path).name


def safe_filename(filename: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", filename).strip(" .")
    return cleaned or "download"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 10000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find unique download path for {path}")


def download_uri(uri: str, target_path: Path) -> int:
    req = Request(escape_uri_path(uri), headers={"User-Agent": "nexusmods-mcp/0.1.0"})
    try:
        with urlopen(req, timeout=300) as resp, target_path.open("wb") as fh:
            total = 0
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    return total
                fh.write(chunk)
                total += len(chunk)
    except Exception:
        try:
            target_path.unlink()
        except FileNotFoundError:
            pass
        raise


def request_json(url: str, headers: dict[str, str], data: bytes | None = None) -> dict[str, Any]:
    req = Request(url, data=data, headers=headers)
    try:
        with urlopen(req, timeout=60) as resp:
            body = resp.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"HTTP {exc.code} for {urlparse(url).path}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Request failed for {urlparse(url).path}: {exc}") from exc
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected JSON response")
    return payload


def escape_uri_path(uri: str) -> str:
    parsed = urlparse(uri)
    escaped_path = quote(parsed.path, safe="/%")
    return urlunparse((parsed.scheme, parsed.netloc, escaped_path, parsed.params, parsed.query, parsed.fragment))


def read_messages(stream: Any) -> Iterable[tuple[dict[str, Any], str]]:
    while True:
        first_line = stream.readline()
        if not first_line:
            return
        if first_line in (b"\n", b"\r\n"):
            continue
        stripped = first_line.lstrip()
        if stripped.startswith((b"{", b"[")) or b":" not in first_line:
            yield json.loads(first_line.decode("utf-8")), "json-line"
            continue

        headers = parse_headers(first_line, stream)
        length_value = headers.get("content-length")
        if length_value is None:
            raise ValueError(f"Missing Content-Length header: {headers}")
        body = stream.read(int(length_value))
        yield json.loads(body.decode("utf-8")), "framed"


def parse_headers(first_line: bytes, stream: Any) -> dict[str, str]:
    headers: dict[str, str] = {}
    line = first_line
    while line not in (b"\n", b"\r\n", b""):
        key, value = line.decode("ascii", errors="replace").split(":", 1)
        headers[key.strip().lower()] = value.strip()
        line = stream.readline()
    return headers


def write_message(stream: Any, message: dict[str, Any], mode: str = "framed") -> None:
    body = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if mode == "json-line":
        stream.write(body + b"\n")
    else:
        stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    stream.flush()


def run_stdio(config: Config) -> None:
    server = McpServer(config)
    for message, mode in read_messages(sys.stdin.buffer):
        response = server.handle(message)
        if response is not None:
            write_message(sys.stdout.buffer, response, mode)


def run_tool(config: Config, tool: str, args: dict[str, Any]) -> None:
    server = McpServer(config)
    if tool not in server.tools:
        raise SystemExit(f"Unknown tool: {tool}")
    print(truncate_payload(server.tools[tool][2](args), config.max_result_chars))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Nexus Mods API MCP server")
    parser.add_argument("--tool", help="Run a tool directly for smoke tests instead of MCP stdio")
    parser.add_argument("--args", default="{}", help="JSON arguments for --tool")
    args = parser.parse_args(argv)
    config = Config.load()
    if args.tool:
        run_tool(config, args.tool, json.loads(args.args))
        return
    run_stdio(config)


if __name__ == "__main__":
    main()
