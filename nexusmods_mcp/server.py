from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from nexusmods_mcp import __version__

API_ROOT = "https://api.nexusmods.com/v1"
API_ROOT_V3 = "https://api.nexusmods.com/v3"
THUNDERSTORE_ROOT = "https://thunderstore.io"
CURSEFORGE_ROOT = "https://api.curseforge.com"
STEAM_API_ROOT = "https://api.steampowered.com"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STAGING_DIR = PROJECT_ROOT / "staging"
DEFAULT_DOWNLOAD_DIR = DEFAULT_STAGING_DIR
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
    tracking_db: Path | None = None

    @property
    def tracking_path(self) -> Path:
        return self.tracking_db or self.download_dir / "downloads.sqlite3"

    @staticmethod
    def load() -> Config:
        api_key = first_env("NEXUS_API_KEY", "NEXUSMODS_API_KEY", "nexus_api_key", "nexux_api_key")
        if not api_key:
            raise RuntimeError(
                "Missing Nexus Mods API key. Set NEXUS_API_KEY, NEXUSMODS_API_KEY, "
                "nexus_api_key, or the legacy nexux_api_key alias."
            )
        country = os.environ.get("NEXUS_COUNTRY", "US").strip().upper() or "US"
        preferred = split_csv(os.environ.get("NEXUS_PREFERRED_LOCATIONS", ""))
        if country == "US" and not preferred:
            preferred = DEFAULT_US_LOCATIONS
        staging_value = first_env(
            "MOD_DOWNLOADER_STAGING_DIR",
            "NEXUS_STAGING_DIR",
            "NEXUS_DOWNLOAD_DIR",
        )
        download_dir = Path(staging_value or DEFAULT_STAGING_DIR).expanduser()
        tracking_value = first_env("MOD_DOWNLOADER_TRACKING_DB")
        return Config(
            api_key=api_key,
            thunderstore_api_key=os.environ.get("THUNDERSTORE_API_KEY", "").strip(),
            curseforge_api_key=os.environ.get("CURSEFORGE_API_KEY", "").strip(),
            steam_api_key=os.environ.get("STEAM_API_KEY", "").strip(),
            country=country,
            preferred_locations=tuple(preferred),
            download_dir=download_dir,
            max_result_chars=int(os.environ.get("NEXUS_MCP_MAX_RESULT_CHARS", "16000")),
            tracking_db=Path(tracking_value).expanduser() if tracking_value else None,
        )


class DownloadTracker:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path).expanduser()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.database_path), timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    remote_updated_at INTEGER,
                    downloaded_at INTEGER NOT NULL,
                    local_path TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS downloads_source_idx "
                "ON downloads(provider, source_key, downloaded_at DESC, id DESC)"
            )

    def record_download(
        self,
        *,
        provider: str,
        source_key: str,
        artifact_id: str,
        version: str,
        remote_updated_at: int | None,
        local_path: Path,
        size_bytes: int,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        path = Path(local_path).expanduser().resolve()
        if not path.is_file():
            raise RuntimeError(f"Downloaded artifact does not exist: {path}")
        downloaded_at = int(time.time())
        digest = sha256_file(path)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO downloads (
                    provider, source_key, artifact_id, version,
                    remote_updated_at, downloaded_at, local_path,
                    size_bytes, sha256, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    provider,
                    source_key,
                    artifact_id,
                    version,
                    remote_updated_at,
                    downloaded_at,
                    str(path),
                    int(size_bytes),
                    digest,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str),
                ),
            )
            record_id = int(cursor.lastrowid or 0)
        return {
            "record_id": record_id,
            "tracking_db": str(self.database_path),
            "provider": provider,
            "source_key": source_key,
            "artifact_id": artifact_id,
            "sha256": digest,
        }

    def list_downloads(
        self,
        provider: str = "",
        limit: int = 100,
        existing_only: bool = False,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM downloads"
        params: list[Any] = []
        if provider:
            query += " WHERE provider = ?"
            params.append(provider)
        query += " ORDER BY downloaded_at DESC, id DESC LIMIT ?"
        params.append(clamp(limit, 1, 1000))
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        downloads = [tracked_download_dict(row) for row in rows]
        if existing_only:
            downloads = [download for download in downloads if download["local_exists"]]
        return downloads

    def latest_downloads(self, provider: str = "", limit: int = 100) -> list[dict[str, Any]]:
        query = (
            "SELECT downloads.* FROM downloads "
            "JOIN (SELECT provider, source_key, MAX(id) AS latest_id FROM downloads "
        )
        params: list[Any] = []
        if provider:
            query += "WHERE provider = ? "
            params.append(provider)
        query += (
            "GROUP BY provider, source_key) latest "
            "ON downloads.id = latest.latest_id "
            "ORDER BY downloads.downloaded_at DESC, downloads.id DESC LIMIT ?"
        )
        params.append(clamp(limit, 1, 1000))
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [tracked_download_dict(row) for row in rows]

    def latest_artifact_downloads(self, provider: str, limit: int = 100) -> list[dict[str, Any]]:
        """Return the newest record for every artifact on multi-file provider pages."""

        query = (
            "SELECT downloads.* FROM downloads "
            "JOIN (SELECT provider, source_key, artifact_id, MAX(id) AS latest_id FROM downloads "
            "WHERE provider = ? GROUP BY provider, source_key, artifact_id) latest "
            "ON downloads.id = latest.latest_id "
            "ORDER BY downloads.downloaded_at DESC, downloads.id DESC LIMIT ?"
        )
        with self._connect() as connection:
            rows = connection.execute(query, (provider, clamp(limit, 1, 1000))).fetchall()
        return [tracked_download_dict(row) for row in rows]

    def summary(self) -> dict[str, Any]:
        with self._connect() as connection:
            total = int(connection.execute("SELECT COUNT(*) FROM downloads").fetchone()[0])
            rows = connection.execute(
                "SELECT provider, COUNT(*) AS count FROM downloads GROUP BY provider ORDER BY provider"
            ).fetchall()
        return {"total_downloads": total, "providers": {str(row["provider"]): int(row["count"]) for row in rows}}


class NexusClient:
    def __init__(self, config: Config, tracker: DownloadTracker | None = None) -> None:
        self.config = config
        self.tracker = tracker or DownloadTracker(config.tracking_path)
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

    def search_mod_catalog(
        self,
        game_domain_name: str,
        query: str = "",
        limit: int = 20,
        max_pages: int = 20,
        sort: str = "updated",
        direction: str = "desc",
    ) -> dict[str, Any]:
        game_domain_name = game_domain_name.strip()
        if not game_domain_name:
            raise ValueError("game_domain_name must not be empty")
        if sort not in {"new", "updated", "endorsements", "downloads"}:
            raise ValueError("sort must be new, updated, endorsements, or downloads")
        if direction not in {"asc", "desc"}:
            raise ValueError("direction must be asc or desc")
        terms = normalize_terms(query)
        matches: list[dict[str, Any]] = []
        max_matches = clamp(limit, 1, 100)
        page_limit = clamp(max_pages, 1, 100)
        cursor = ""
        next_cursor: str | None = None
        total_count: int | None = None
        scanned_mods = 0
        scanned_pages = 0

        while scanned_pages < page_limit:
            pagination: dict[str, Any] = {"type": "cursor", "page_size": 100}
            if cursor:
                pagination["cursor"] = cursor
            payload = self.request_v3(
                "/mods/search",
                {
                    "filters": {"games": [game_domain_name]},
                    "sort": {"field": sort, "direction": direction},
                    "pagination": pagination,
                },
            )
            data = dict_or_empty(payload.get("data"))
            mods = list_or_empty(data.get("mods"))
            meta = dict_or_empty(payload.get("meta"))
            scanned_pages += 1
            scanned_mods += len(mods)
            if isinstance(meta.get("total_count"), int):
                total_count = meta["total_count"]
            next_value = meta.get("next_cursor")
            next_cursor = next_value if isinstance(next_value, str) and next_value else None

            for mod in mods:
                if not isinstance(mod, dict):
                    continue
                haystack = catalog_mod_text(mod)
                if terms and not all(term in haystack for term in terms):
                    continue
                matches.append(slim_catalog_mod(mod))
                if len(matches) >= max_matches:
                    break
            if len(matches) >= max_matches or next_cursor is None:
                break
            cursor = next_cursor

        return {
            "game_domain_name": game_domain_name,
            "query": query,
            "source": "Nexus Mods API v3 experimental /mods/search with local text filtering",
            "matches": matches,
            "scanned_pages": scanned_pages,
            "scanned_mods": scanned_mods,
            "total_count": total_count,
            "catalog_exhausted": next_cursor is None,
            "next_cursor": next_cursor,
        }

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
        target_dir = (
            Path(output_dir).expanduser()
            if output_dir
            else self.config.download_dir / "nexus" / safe_filename(game_domain_name) / str(int(mod_id))
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        target_name = filename or filename_from_uri(selected["URI"]) or f"{game_domain_name}-{mod_id}-{file_id}.download"
        target_path = unique_path(target_dir / safe_filename(target_name))
        file_metadata: dict[str, Any] = {}
        mod_metadata: dict[str, Any] = {}
        try:
            files = list_or_empty(self.list_mod_files(game_domain_name, mod_id).get("files"))
            file_metadata = next(
                (dict(item) for item in files if isinstance(item, dict) and int(item.get("file_id") or 0) == int(file_id)),
                {},
            )
        except (RuntimeError, ValueError, TypeError):
            file_metadata = {}
        try:
            mod_metadata = self.get_mod(game_domain_name, mod_id)
        except (RuntimeError, ValueError, TypeError):
            mod_metadata = {}
        started = time.time()
        size = download_uri(selected["URI"], target_path)
        tracking = self.tracker.record_download(
            provider="nexus",
            source_key=f"{game_domain_name}:{int(mod_id)}",
            artifact_id=str(int(file_id)),
            version=str(file_metadata.get("version") or mod_metadata.get("version") or ""),
            remote_updated_at=(
                integer_or_none(mod_metadata.get("updated_timestamp"))
                or integer_or_none(mod_metadata.get("updated_time"))
                or integer_or_none(file_metadata.get("uploaded_timestamp"))
            ),
            local_path=target_path,
            size_bytes=size,
            metadata={
                "game_domain_name": game_domain_name,
                "mod_id": int(mod_id),
                "file_id": int(file_id),
                "mod": mod_metadata,
                "file": file_metadata,
                "selected_mirror": {key: selected[key] for key in selected if key != "URI"},
            },
        )
        return {
            "downloaded": str(target_path),
            "bytes": size,
            "duration_seconds": round(time.time() - started, 2),
            "selected_mirror": {key: selected[key] for key in selected if key != "URI"},
            "selection_reason": selected.get("selection_reason", ""),
            "tracking": tracking,
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

    def request_v3(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        req = Request(
            f"{API_ROOT_V3}{path}",
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            headers={
                "apikey": self.config.api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "nexusmods-mcp/0.2.0",
            },
            method="POST",
        )
        try:
            with urlopen(req, timeout=60) as resp:
                response_body = resp.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"Nexus API v3 HTTP {exc.code} for {path}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Nexus API v3 request failed for {path}: {exc}") from exc
        payload = json.loads(response_body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("Unexpected Nexus API v3 response")
        return payload


class ThunderstoreClient:
    def __init__(self, config: Config, tracker: DownloadTracker | None = None) -> None:
        self.config = config
        self.tracker = tracker or DownloadTracker(config.tracking_path)

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
        target_dir = (
            Path(output_dir).expanduser()
            if output_dir
            else self.config.download_dir / "thunderstore" / safe_filename(namespace) / safe_filename(name)
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        target_name = filename or filename_from_uri(download_url) or f"{namespace}-{name}-{version_payload.get('version_number', 'latest')}.zip"
        target_path = unique_path(target_dir / safe_filename(target_name))
        started = time.time()
        size = download_uri(download_url, target_path)
        resolved_version = str(version_payload.get("version_number") or version)
        tracking = self.tracker.record_download(
            provider="thunderstore",
            source_key=f"{namespace}:{name}",
            artifact_id=resolved_version,
            version=resolved_version,
            remote_updated_at=parse_remote_timestamp(version_payload.get("date_created")),
            local_path=target_path,
            size_bytes=size,
            metadata={
                "namespace": namespace,
                "name": name,
                "version": resolved_version,
                "package_version": version_payload,
            },
        )
        return {
            "downloaded": str(target_path),
            "bytes": size,
            "duration_seconds": round(time.time() - started, 2),
            "namespace": namespace,
            "name": name,
            "version": resolved_version,
            "download_url": download_url,
            "tracking": tracking,
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
    def __init__(self, config: Config, tracker: DownloadTracker | None = None) -> None:
        self.config = config
        self.tracker = tracker or DownloadTracker(config.tracking_path)

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
        target_dir = (
            Path(output_dir).expanduser()
            if output_dir
            else self.config.download_dir / "curseforge" / str(int(mod_id))
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        target_name = filename or filename_from_uri(download_url) or f"curseforge-{mod_id}-{file_id}.download"
        target_path = unique_path(target_dir / safe_filename(target_name))
        file_metadata: dict[str, Any] = {}
        mod_metadata: dict[str, Any] = {}
        try:
            files = list_or_empty(self.get_mod_files(mod_id, page_size=50).get("files"))
            file_metadata = next(
                (dict(item) for item in files if isinstance(item, dict) and int(item.get("id") or 0) == int(file_id)),
                {},
            )
        except (RuntimeError, ValueError, TypeError):
            file_metadata = {}
        try:
            mod_metadata = self.get_mod(mod_id)
        except (RuntimeError, ValueError, TypeError):
            mod_metadata = {}
        started = time.time()
        size = download_uri(download_url, target_path)
        tracking = self.tracker.record_download(
            provider="curseforge",
            source_key=str(int(mod_id)),
            artifact_id=str(int(file_id)),
            version=str(file_metadata.get("displayName") or file_metadata.get("fileName") or ""),
            remote_updated_at=(
                parse_remote_timestamp(mod_metadata.get("dateModified"))
                or parse_remote_timestamp(file_metadata.get("fileDate"))
            ),
            local_path=target_path,
            size_bytes=size,
            metadata={
                "mod_id": int(mod_id),
                "file_id": int(file_id),
                "mod": mod_metadata,
                "file": file_metadata,
            },
        )
        return {
            "downloaded": str(target_path),
            "bytes": size,
            "duration_seconds": round(time.time() - started, 2),
            "mod_id": int(mod_id),
            "file_id": int(file_id),
            "download_url": download_url,
            "tracking": tracking,
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
    def __init__(self, config: Config, tracker: DownloadTracker | None = None) -> None:
        self.config = config
        self.tracker = tracker or DownloadTracker(config.tracking_path)

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
        target_dir = (
            Path(output_dir).expanduser()
            if output_dir
            else self.config.download_dir / "steam" / str(int(published_file_id))
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        target_name = filename or filename_from_uri(file_url) or f"steam-workshop-{published_file_id}.download"
        target_path = unique_path(target_dir / safe_filename(target_name))
        started = time.time()
        size = download_uri(file_url, target_path)
        item = dict_or_empty(info.get("item"))
        tracking = self.tracker.record_download(
            provider="steam",
            source_key=str(int(published_file_id)),
            artifact_id=str(int(published_file_id)),
            version=str(item.get("time_updated") or ""),
            remote_updated_at=integer_or_none(item.get("time_updated")),
            local_path=target_path,
            size_bytes=size,
            metadata={"published_file_id": int(published_file_id), "item": item},
        )
        return {
            "downloaded": str(target_path),
            "bytes": size,
            "duration_seconds": round(time.time() - started, 2),
            "tracking": tracking,
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
        self.tracker = DownloadTracker(config.tracking_path)
        self.client = NexusClient(config, self.tracker)
        self.thunderstore = ThunderstoreClient(config, self.tracker)
        self.curseforge = CurseForgeClient(config, self.tracker)
        self.steam = SteamClient(config, self.tracker)
        self.tools: dict[str, tuple[str, dict[str, Any], Callable[[dict[str, Any]], Any]]] = {
            "get_download_staging": (
                "Show the shared staging directory, tracking database, and recorded download counts.",
                {"type": "object", "properties": {}},
                lambda _args: self.download_staging(),
            ),
            "list_tracked_downloads": (
                "List mod archives recorded by this MCP across Nexus, Thunderstore, CurseForge, and Steam.",
                {
                    "type": "object",
                    "properties": {
                        "provider": {"type": "string"},
                        "limit": {"type": "integer"},
                        "existing_only": {"type": "boolean"},
                    },
                },
                lambda args: {
                    "downloads": self.tracker.list_downloads(
                        str(args.get("provider", "")),
                        int(args.get("limit", 100)),
                        bool(args.get("existing_only", False)),
                    )
                },
            ),
            "check_tracked_updates": (
                "Check official provider metadata for updates to the latest recorded download of each tracked mod or package.",
                {
                    "type": "object",
                    "properties": {
                        "provider": {"type": "string"},
                        "only_updates": {"type": "boolean"},
                        "limit": {"type": "integer"},
                    },
                },
                lambda args: self.check_tracked_updates(
                    str(args.get("provider", "")),
                    bool(args.get("only_updates", False)),
                    int(args.get("limit", 100)),
                ),
            ),
            "check_nexus_artifact_updates": (
                "Check explicit Nexus file replacement chains for a supplied artifact inventory.",
                {
                    "type": "object",
                    "required": ["game_domain_name", "artifacts"],
                    "properties": {
                        "game_domain_name": {"type": "string"},
                        "artifacts": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["mod_id", "file_id"],
                                "properties": {
                                    "mod_id": {"type": "integer"},
                                    "file_id": {"type": "integer"},
                                    "file_name": {"type": "string"},
                                    "version": {"type": "string"},
                                    "path": {"type": "string"},
                                },
                            },
                        },
                        "only_updates": {"type": "boolean"},
                    },
                },
                lambda args: self.check_nexus_artifact_updates(
                    str(args["game_domain_name"]),
                    list(args["artifacts"]),
                    bool(args.get("only_updates", False)),
                ),
            ),
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
                "Search the official Nexus API v3 mod catalog for a game, with bounded local text matching over mod metadata.",
                {
                    "type": "object",
                    "required": ["game_domain_name"],
                    "properties": {
                        "game_domain_name": {"type": "string"},
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                        "max_pages": {"type": "integer"},
                        "sort": {"type": "string"},
                        "direction": {"type": "string"},
                    },
                },
                lambda args: self.client.search_mod_catalog(
                    str(args["game_domain_name"]),
                    str(args.get("query", "")),
                    int(args.get("limit", 20)),
                    int(args.get("max_pages", 20)),
                    str(args.get("sort", "updated")),
                    str(args.get("direction", "desc")),
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

    def download_staging(self) -> dict[str, Any]:
        self.config.download_dir.mkdir(parents=True, exist_ok=True)
        return {
            "staging_dir": str(self.config.download_dir.resolve()),
            "tracking_db": str(self.config.tracking_path.resolve()),
            **self.tracker.summary(),
        }

    def check_tracked_updates(
        self,
        provider: str = "",
        only_updates: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        supported = {"nexus", "thunderstore", "curseforge", "steam"}
        if provider and provider not in supported:
            raise ValueError(f"provider must be one of: {', '.join(sorted(supported))}")
        if provider == "nexus":
            tracked = self.tracker.latest_artifact_downloads("nexus", limit)
        elif provider:
            tracked = self.tracker.latest_downloads(provider=provider, limit=limit)
        else:
            nexus = self.tracker.latest_artifact_downloads("nexus", 1000)
            other = [
                record
                for record in self.tracker.latest_downloads(limit=1000)
                if record["provider"] != "nexus"
            ]
            tracked = sorted(
                [*nexus, *other],
                key=lambda record: (int(record["downloaded_at"]), int(record["id"])),
                reverse=True,
            )[:clamp(limit, 1, 1000)]
        results: list[dict[str, Any]] = []
        nexus_by_game: dict[str, list[dict[str, Any]]] = {}
        non_nexus: list[dict[str, Any]] = []
        for record in tracked:
            if record["provider"] != "nexus":
                non_nexus.append(record)
                continue
            metadata = dict_or_empty(record.get("metadata"))
            game_domain_name = str(metadata.get("game_domain_name") or "")
            nexus_by_game.setdefault(game_domain_name, []).append(record)

        for game_domain_name, records in nexus_by_game.items():
            artifacts = []
            records_by_identity: dict[tuple[int, int], dict[str, Any]] = {}
            for record in records:
                metadata = dict_or_empty(record.get("metadata"))
                mod_id = int(metadata["mod_id"])
                file_id = int(metadata.get("file_id") or record["artifact_id"])
                identity = (mod_id, file_id)
                records_by_identity[identity] = record
                artifacts.append({"mod_id": mod_id, "file_id": file_id})
            try:
                checked = self.check_nexus_artifact_updates(game_domain_name, artifacts)
                checked_by_identity = {
                    (int(item["mod_id"]), int(item["file_id"])): item
                    for item in checked["results"]
                }
                for identity, record in records_by_identity.items():
                    item = checked_by_identity[identity]
                    result = {
                        "provider": "nexus",
                        "source_key": record["source_key"],
                        "tracked": record,
                        **item,
                        "latest": item.get("replacement") or item.get("current_file") or {},
                    }
                    if only_updates and result.get("update_available") is False:
                        continue
                    results.append(result)
            except Exception as exc:
                for record in records:
                    result = {
                        "provider": "nexus",
                        "source_key": record["source_key"],
                        "tracked": record,
                        "update_available": None,
                        "status": "metadata_unavailable",
                        "error": self._error_text(exc),
                    }
                    results.append(result)

        for record in non_nexus:
            try:
                result = self._check_tracked_download(record)
            except Exception as exc:
                result = {
                    "provider": record["provider"],
                    "source_key": record["source_key"],
                    "tracked": record,
                    "update_available": None,
                    "error": self._error_text(exc),
                }
            if only_updates and result.get("update_available") is False:
                continue
            results.append(result)
        return {
            "provider": provider or "all",
            "tracked_sources": len(tracked),
            "updates_available": sum(result.get("update_available") is True for result in results),
            "errors": sum(bool(result.get("error")) for result in results),
            "only_updates": only_updates,
            "results": results,
        }

    def check_nexus_artifact_updates(
        self,
        game_domain_name: str,
        artifacts: list[dict[str, Any]],
        only_updates: bool = False,
    ) -> dict[str, Any]:
        """Check exact Nexus artifacts without requiring tracking-database records."""

        if not game_domain_name.strip():
            raise ValueError("game_domain_name is required")
        if not isinstance(artifacts, list) or not artifacts:
            raise ValueError("artifacts must be a non-empty list")
        if len(artifacts) > 1000:
            raise ValueError("artifacts cannot contain more than 1000 records")

        by_mod: dict[int, list[dict[str, Any]]] = {}
        identities: set[tuple[int, int]] = set()
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise TypeError("every artifact must be an object")
            mod_id = int(artifact["mod_id"])
            file_id = int(artifact["file_id"])
            if mod_id <= 0 or file_id <= 0:
                raise ValueError("mod_id and file_id must be positive integers")
            identity = (mod_id, file_id)
            if identity in identities:
                raise ValueError(f"duplicate Nexus artifact {mod_id}:{file_id}")
            identities.add(identity)
            by_mod.setdefault(mod_id, []).append(dict(artifact))

        results: list[dict[str, Any]] = []
        for mod_id, selected in sorted(by_mod.items()):
            try:
                mod = self.client.get_mod(game_domain_name, mod_id)
                files_payload = self.client.list_mod_files(game_domain_name, mod_id)
                selected_ids = {int(item["file_id"]) for item in selected}
                for artifact in selected:
                    result = nexus_artifact_update_result(
                        mod_id=mod_id,
                        file_id=int(artifact["file_id"]),
                        files_payload=files_payload,
                        inventory_file_ids=selected_ids,
                    )
                    result.update(
                        {
                            "game_domain_name": game_domain_name,
                            "mod_name": mod.get("name"),
                            "author": mod.get("author") or mod.get("uploaded_by"),
                            "selected": artifact,
                        }
                    )
                    if only_updates and result.get("update_available") is False:
                        continue
                    results.append(result)
            except Exception as exc:
                for artifact in selected:
                    results.append(
                        {
                            "game_domain_name": game_domain_name,
                            "mod_id": mod_id,
                            "file_id": int(artifact["file_id"]),
                            "selected": artifact,
                            "status": "metadata_unavailable",
                            "update_available": None,
                            "error": self._error_text(exc),
                        }
                    )

        return {
            "game_domain_name": game_domain_name,
            "artifact_count": len(artifacts),
            "mod_count": len(by_mod),
            "updates_available": sum(item.get("update_available") is True for item in results),
            "superseded_in_inventory": sum(
                item.get("status") == "superseded_in_inventory" for item in results
            ),
            "errors": sum(bool(item.get("error")) for item in results),
            "only_updates": only_updates,
            "results": results,
        }

    def _check_tracked_download(self, record: dict[str, Any]) -> dict[str, Any]:
        provider = record["provider"]
        metadata = dict_or_empty(record.get("metadata"))
        if provider == "nexus":
            raise RuntimeError("Nexus tracked updates must be checked as an artifact inventory")
        if provider == "thunderstore":
            namespace = str(metadata["namespace"])
            name = str(metadata["name"])
            latest = self.thunderstore.latest_version(namespace, name)
            latest_version = str(latest.get("version_number") or "")
            latest_timestamp = parse_remote_timestamp(latest.get("date_created"))
            update_available = remote_artifact_is_newer(record, latest_version, latest_timestamp)
            return update_check_result(
                record,
                update_available,
                {
                    "version": latest_version,
                    "date_created": latest.get("date_created"),
                    "download_url": latest.get("download_url"),
                },
            )
        if provider == "curseforge":
            mod_id = int(metadata["mod_id"])
            curseforge_mod: dict[str, Any] = {}
            try:
                curseforge_mod = self.curseforge.get_mod(mod_id)
            except (RuntimeError, ValueError, TypeError):
                curseforge_mod = {}
            files = [item for item in list_or_empty(self.curseforge.get_mod_files(mod_id, page_size=50).get("files")) if isinstance(item, dict)]
            latest = max(files, key=curseforge_file_sort_key) if files else {}
            latest_id = str(latest.get("id") or "")
            latest_timestamp = (
                parse_remote_timestamp(curseforge_mod.get("dateModified"))
                or parse_remote_timestamp(latest.get("fileDate"))
            )
            if record.get("remote_updated_at") is not None and latest_timestamp is not None:
                update_available = latest_timestamp > int(record["remote_updated_at"])
            else:
                update_available = remote_artifact_is_newer(record, latest_id, latest_timestamp)
            latest_result = dict(latest)
            latest_result["mod_date_modified"] = curseforge_mod.get("dateModified")
            return update_check_result(record, update_available, latest_result)
        if provider == "steam":
            published_file_id = int(metadata["published_file_id"])
            details = self.steam.get_workshop_details([published_file_id]).get("details", [])
            latest = dict(details[0]) if details else {}
            latest_timestamp = integer_or_none(latest.get("time_updated"))
            update_available = bool(
                latest_timestamp is not None
                and latest_timestamp > int(record.get("remote_updated_at") or 0)
            )
            return update_check_result(record, update_available, latest)
        raise ValueError(f"Unsupported tracked provider: {provider}")

    def handle(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            return self.error({"id": None}, -32600, "Invalid Request: expected an object")
        if "id" in message and message["id"] is not None and type(message["id"]) not in (str, int):
            return self.error({"id": None}, -32600, "Invalid Request: invalid request id")
        if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return self.error(message, -32600, "Invalid Request")
        if "id" not in message:
            return None
        method = message.get("method")
        params = message.get("params", {})
        if not isinstance(params, dict):
            return self.error(message, -32602, "Invalid params: expected an object")
        try:
            if method == "ping":
                return self.result(message, {})
            if method == "initialize":
                requested_version = params.get("protocolVersion", "2024-11-05")
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
                name = params.get("name")
                args = params.get("arguments", {})
                if not isinstance(name, str) or name not in self.tools:
                    return self.error(message, -32602, "Unknown tool")
                try:
                    validate_tool_arguments(args, self.tools[name][1])
                except ValueError as exc:
                    return self.error(message, -32602, str(exc))
                try:
                    payload = self.tools[name][2](args)
                    text = truncate_payload(payload, self.config.max_result_chars)
                except Exception as exc:
                    return self.result(message, {
                        "isError": True,
                        "content": [{"type": "text", "text": f"{name}: {self._error_text(exc)}"}],
                    })
                return self.result(message, {"content": [{"type": "text", "text": text}]})
            return self.error(message, -32601, f"Method not found: {method}")
        except Exception as exc:
            return self.error(message, -32603, self._error_text(exc))

    def _error_text(self, error: Exception) -> str:
        text = str(error) or type(error).__name__
        for secret in (self.config.api_key, self.config.thunderstore_api_key,
                       self.config.curseforge_api_key, self.config.steam_api_key):
            if secret:
                text = text.replace(secret, "[redacted]")
        return text[:2000]

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
        return {"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": code, "message": text}}


def validate_tool_arguments(args: Any, schema: dict[str, Any]) -> None:
    if not isinstance(args, dict):
        raise ValueError("Tool arguments must be an object")
    for name in schema.get("required", []):
        if name not in args:
            raise ValueError(f"Missing required argument: {name}")
    types = {"string": str, "integer": int, "boolean": bool, "array": list, "object": dict}
    for name, spec in schema.get("properties", {}).items():
        if name not in args:
            continue
        expected = types.get(spec.get("type"))
        if expected is not None and type(args[name]) is not expected:
            raise ValueError(f"Argument {name} must be {spec['type']}")
        if spec.get("type") == "array" and spec.get("items", {}).get("type") in types:
            item_type = types[spec["items"]["type"]]
            if any(type(value) is not item_type for value in args[name]):
                raise ValueError(f"Argument {name} contains an invalid item type")


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


def slim_catalog_mod(mod: dict[str, Any]) -> dict[str, Any]:
    game = dict_or_empty(mod.get("game"))
    author = dict_or_empty(mod.get("author"))
    category = dict_or_empty(mod.get("category"))
    return {
        "id": mod.get("id"),
        "game_scoped_id": mod.get("game_scoped_id"),
        "game_domain_name": game.get("domain_name"),
        "game_name": game.get("name"),
        "name": mod.get("name"),
        "summary": mod.get("summary"),
        "version": mod.get("version"),
        "author": author.get("name"),
        "category": category.get("name"),
        "endorsements": mod.get("endorsements"),
        "downloads": mod.get("downloads"),
        "unique_downloads": mod.get("unique_downloads"),
        "updated_at": mod.get("updated_at"),
        "created_at": mod.get("created_at"),
        "adult": mod.get("adult"),
        "supports_vortex": mod.get("supports_vortex"),
        "nexusmods_url": (
            f"https://www.nexusmods.com/{game.get('domain_name')}/mods/{mod.get('game_scoped_id')}"
            if game.get("domain_name") and mod.get("game_scoped_id")
            else None
        ),
    }


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


def catalog_mod_text(mod: dict[str, Any]) -> str:
    author = dict_or_empty(mod.get("author"))
    category = dict_or_empty(mod.get("category"))
    fields = (
        mod.get("name", ""),
        mod.get("summary", ""),
        mod.get("version", ""),
        author.get("name", ""),
        category.get("name", ""),
    )
    return " ".join(str(field) for field in fields).lower()


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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tracked_download_dict(row: sqlite3.Row) -> dict[str, Any]:
    metadata_text = str(row["metadata_json"] or "{}")
    try:
        metadata = json.loads(metadata_text)
    except json.JSONDecodeError:
        metadata = {"tracking_error": "invalid metadata_json"}
    local_path = str(row["local_path"])
    return {
        "id": int(row["id"]),
        "provider": str(row["provider"]),
        "source_key": str(row["source_key"]),
        "artifact_id": str(row["artifact_id"]),
        "version": str(row["version"]),
        "remote_updated_at": integer_or_none(row["remote_updated_at"]),
        "downloaded_at": int(row["downloaded_at"]),
        "downloaded_at_iso": datetime.fromtimestamp(int(row["downloaded_at"]), timezone.utc).isoformat(),
        "local_path": local_path,
        "local_exists": Path(local_path).is_file(),
        "size_bytes": int(row["size_bytes"]),
        "sha256": str(row["sha256"]),
        "metadata": metadata if isinstance(metadata, dict) else {},
    }


def integer_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_remote_timestamp(value: Any) -> int | None:
    numeric = integer_or_none(value)
    if numeric is not None:
        return numeric
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def nexus_file_summary(file: dict[str, Any] | None, file_id: int | None = None) -> dict[str, Any] | None:
    if file is None and file_id is None:
        return None
    source = file or {}
    resolved_id = integer_or_none(source.get("file_id")) or file_id
    return {
        "file_id": resolved_id,
        "name": source.get("name"),
        "file_name": source.get("file_name"),
        "version": source.get("version"),
        "category_name": source.get("category_name"),
        "uploaded_timestamp": integer_or_none(source.get("uploaded_timestamp")),
        "uploaded_time": source.get("uploaded_time"),
        "size_in_bytes": integer_or_none(source.get("size_in_bytes")),
    }


def nexus_artifact_update_result(
    *,
    mod_id: int,
    file_id: int,
    files_payload: dict[str, Any],
    inventory_file_ids: set[int],
) -> dict[str, Any]:
    """Classify one Nexus file using explicit, chronologically valid replacement edges."""

    file_index = {
        int(item["file_id"]): item
        for item in list_or_empty(files_payload.get("files"))
        if isinstance(item, dict) and integer_or_none(item.get("file_id")) is not None
    }
    candidates: dict[int, list[tuple[int, int]]] = {}
    anomalies: list[dict[str, Any]] = []
    for update in list_or_empty(files_payload.get("file_updates")):
        if not isinstance(update, dict):
            continue
        old_id = integer_or_none(update.get("old_file_id"))
        new_id = integer_or_none(update.get("new_file_id"))
        if old_id is None or new_id is None:
            continue
        old_timestamp = integer_or_none(file_index.get(old_id, {}).get("uploaded_timestamp")) or 0
        replacement_timestamp = max(
            integer_or_none(update.get("uploaded_timestamp")) or 0,
            integer_or_none(file_index.get(new_id, {}).get("uploaded_timestamp")) or 0,
        )
        if old_timestamp and replacement_timestamp <= old_timestamp:
            anomalies.append(
                {
                    "old_file_id": old_id,
                    "new_file_id": new_id,
                    "reason": "replacement_not_newer",
                }
            )
            continue
        candidates.setdefault(old_id, []).append((replacement_timestamp, new_id))

    updates: dict[int, int] = {}
    ambiguous: dict[int, list[int]] = {}
    for old_id, replacements in candidates.items():
        replacements.sort()
        replacement_ids = sorted({replacement_id for _, replacement_id in replacements})
        if len(replacement_ids) > 1:
            ambiguous[old_id] = replacement_ids
        updates[old_id] = replacements[-1][1]

    lineage = [file_id]
    seen = {file_id}
    lineage_error = None
    while lineage[-1] in updates:
        replacement_id = updates[lineage[-1]]
        lineage.append(replacement_id)
        if replacement_id in seen:
            lineage_error = "cycle"
            break
        seen.add(replacement_id)

    current = file_index.get(file_id)
    terminal_id = lineage[-1]
    replacement = file_index.get(terminal_id)
    ambiguous_steps = {str(step): ambiguous[step] for step in lineage if step in ambiguous}
    if lineage_error:
        status = "lineage_error"
        update_available = None
    elif ambiguous_steps:
        status = "lineage_ambiguous"
        update_available = None
    elif len(lineage) > 1 and replacement is None:
        status = "replacement_missing"
        update_available = None
    elif len(lineage) > 1 and terminal_id in inventory_file_ids:
        status = "superseded_in_inventory"
        update_available = False
    elif len(lineage) > 1:
        status = "update_available"
        update_available = True
    elif current is None:
        status = "missing_unmapped"
        update_available = None
    elif str(current.get("category_name") or "").upper() in {"OLD_VERSION", "ARCHIVED", "DELETED"}:
        status = "removed_or_unmapped"
        update_available = None
    else:
        status = "current"
        update_available = False

    result = {
        "mod_id": mod_id,
        "file_id": file_id,
        "status": status,
        "update_available": update_available,
        "lineage": lineage,
        "current_file": nexus_file_summary(current, file_id),
        "replacement": nexus_file_summary(replacement, terminal_id) if len(lineage) > 1 else None,
        "metadata_anomalies": anomalies,
    }
    if lineage_error:
        result["lineage_error"] = lineage_error
    if ambiguous_steps:
        result["ambiguous_replacements"] = ambiguous_steps
    return result


def nexus_file_sort_key(file: dict[str, Any]) -> tuple[int, int]:
    return (
        integer_or_none(file.get("uploaded_timestamp")) or 0,
        integer_or_none(file.get("file_id")) or 0,
    )


def curseforge_file_sort_key(file: dict[str, Any]) -> tuple[int, int]:
    return (
        parse_remote_timestamp(file.get("fileDate")) or 0,
        integer_or_none(file.get("id")) or 0,
    )


def remote_artifact_is_newer(
    record: dict[str, Any],
    latest_artifact_id: str,
    latest_updated_at: int | None,
) -> bool:
    current_artifact_id = str(record.get("artifact_id") or "")
    current_updated_at = integer_or_none(record.get("remote_updated_at"))
    if not latest_artifact_id or latest_artifact_id == current_artifact_id:
        return False
    if current_updated_at is not None and latest_updated_at is not None:
        return latest_updated_at > current_updated_at
    current_numeric = integer_or_none(current_artifact_id)
    latest_numeric = integer_or_none(latest_artifact_id)
    if current_numeric is not None and latest_numeric is not None:
        return latest_numeric > current_numeric
    return True


def update_check_result(
    record: dict[str, Any],
    update_available: bool,
    latest: dict[str, Any],
) -> dict[str, Any]:
    return {
        "provider": record["provider"],
        "source_key": record["source_key"],
        "update_available": update_available,
        "tracked": record,
        "latest": latest,
    }


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


class McpParseError(ValueError):
    pass


def decode_message(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return McpParseError("Parse error")


def read_messages(stream: Any) -> Iterable[tuple[Any, str]]:
    while True:
        first_line = stream.readline()
        if not first_line:
            return
        if first_line in (b"\n", b"\r\n"):
            continue
        stripped = first_line.lstrip()
        if not stripped.lower().startswith((b"content-length:", b"content-type:")):
            yield decode_message(first_line), "json-line"
            continue

        try:
            headers = parse_headers(first_line, stream)
            length = int(headers["content-length"])
            if length < 0:
                raise ValueError("Negative content length")
        except (KeyError, ValueError):
            yield McpParseError("Invalid framing"), "framed"
            return
        body = stream.read(length)
        if len(body) != length:
            yield McpParseError("Truncated frame"), "framed"
            return
        yield decode_message(body), "framed"


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
        response = server.error({"id": None}, -32700, "Parse error") if isinstance(message, McpParseError) else server.handle(message)
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
