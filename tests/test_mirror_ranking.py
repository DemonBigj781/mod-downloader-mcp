from nexusmods_mcp.server import escape_uri_path, rank_links


def link(name: str, short_name: str, uri: str = "https://example.invalid/file.zip"):
    return {"name": name, "short_name": short_name, "URI": uri}


def test_premium_account_prefers_in_country_premium_before_free_and_outside():
    links = [
        link("Amsterdam (Premium)", "Amsterdam", "https://amsterdam-premium.nexus-cdn.com/file.zip"),
        link("Nexus Global Content Delivery Network", "Nexus CDN"),
        link("Dallas (Premium)", "Dallas", "https://dallas-premium.nexus-cdn.com/file.zip"),
        link("Chicago (Premium)", "Chicago", "https://chicago-premium.nexus-cdn.com/file.zip"),
    ]

    ranked = rank_links(links, {"is_premium": True}, "US", ("Chicago", "Dallas"))

    assert [item["short_name"] for item in ranked] == ["Chicago", "Dallas", "Amsterdam", "Nexus CDN"]


def test_non_premium_account_prefers_free_global_before_premium():
    links = [
        link("Chicago (Premium)", "Chicago", "https://chicago-premium.nexus-cdn.com/file.zip"),
        link("Nexus Global Content Delivery Network", "Nexus CDN"),
        link("Amsterdam (Premium)", "Amsterdam", "https://amsterdam-premium.nexus-cdn.com/file.zip"),
    ]

    ranked = rank_links(links, {"is_premium": False}, "US", ("Chicago", "Dallas"))

    assert [item["short_name"] for item in ranked] == ["Nexus CDN", "Chicago", "Amsterdam"]


def test_escape_uri_path_preserves_query_and_escapes_spaces():
    uri = "https://example.invalid/a dir/mod file.zip?expires=1&md5=abc"

    assert escape_uri_path(uri) == "https://example.invalid/a%20dir/mod%20file.zip?expires=1&md5=abc"
