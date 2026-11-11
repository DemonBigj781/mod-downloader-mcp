from __future__ import annotations

from pathlib import Path

from nexusmods_mcp.server import Config, NexusClient


def client(tmp_path: Path) -> NexusClient:
    return NexusClient(Config("fixture-key", "", "", "", "US", (), tmp_path, 16000))


def test_catalog_search_pages_v3_and_matches_name_summary_author_and_category(tmp_path, monkeypatch):
    nexus = client(tmp_path)
    calls = []
    pages = [
        {
            "data": {
                "mods": [
                    {
                        "id": "1001",
                        "game_scoped_id": "2001",
                        "game": {"id": "1704", "domain_name": "tcgcardshopsimulator", "name": "TCG Card Shop Simulator"},
                        "name": "Unrelated Updated Mod",
                        "summary": "Nothing about progression",
                        "version": "1.0",
                        "author": {"id": "1", "name": "ExampleAuthor"},
                        "category": {"id": "7", "name": "Gameplay"},
                        "updated_at": "2026-11-05T10:00:00Z",
                    }
                ]
            },
            "meta": {"type": "cursor", "page_size": 1, "total_count": 2, "next_cursor": "page-two"},
        },
        {
            "data": {
                "mods": [
                    {
                        "id": "1002",
                        "game_scoped_id": "2017",
                        "game": {"id": "1704", "domain_name": "tcgcardshopsimulator", "name": "TCG Card Shop Simulator"},
                        "name": "Skill Tree",
                        "summary": "Worker progression and scheduling upgrades",
                        "version": "3.0",
                        "author": {"id": "2", "name": "TreeAuthor"},
                        "category": {"id": "8", "name": "Skills"},
                        "updated_at": "2026-11-05T12:00:00Z",
                    }
                ]
            },
            "meta": {"type": "cursor", "page_size": 1, "total_count": 2, "next_cursor": None},
        },
    ]

    def request_v3(path, body):
        calls.append((path, body))
        return pages[len(calls) - 1]

    monkeypatch.setattr(nexus, "request_v3", request_v3)

    result = nexus.search_mod_catalog(
        game_domain_name="tcgcardshopsimulator",
        query="skill tree",
        limit=10,
        max_pages=5,
        sort="updated",
        direction="desc",
    )

    assert [match["game_scoped_id"] for match in result["matches"]] == ["2017"]
    assert result["scanned_pages"] == 2
    assert result["scanned_mods"] == 2
    assert result["total_count"] == 2
    assert result["catalog_exhausted"] is True
    assert calls[0] == (
        "/mods/search",
        {
            "filters": {"games": ["tcgcardshopsimulator"]},
            "sort": {"field": "updated", "direction": "desc"},
            "pagination": {"type": "cursor", "page_size": 100},
        },
    )
    assert calls[1][1]["pagination"]["cursor"] == "page-two"


def test_catalog_search_is_bounded_and_reports_partial_scan(tmp_path, monkeypatch):
    nexus = client(tmp_path)

    def request_v3(_path, _body):
        return {
            "data": {"mods": []},
            "meta": {"type": "cursor", "page_size": 100, "total_count": 5000, "next_cursor": "more"},
        }

    monkeypatch.setattr(nexus, "request_v3", request_v3)

    result = nexus.search_mod_catalog("tcgcardshopsimulator", "missing", limit=20, max_pages=1)

    assert result["matches"] == []
    assert result["scanned_pages"] == 1
    assert result["catalog_exhausted"] is False
    assert result["next_cursor"] == "more"
