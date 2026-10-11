from allegro_mcp import __version__
from allegro_mcp.user_agent import allegro_headers, get_user_agent


def test_default_user_agent_is_generic():
    assert get_user_agent() == f"allegro-mcp-server/{__version__} (+https://github.com/)"


def test_user_agent_from_env(monkeypatch):
    monkeypatch.setenv("ALLEGRO_APP_NAME", "my-shop-tools")
    monkeypatch.setenv("ALLEGRO_APP_VERSION", "2.1.0")
    monkeypatch.setenv("ALLEGRO_APP_URL", "https://example.com/app")
    assert get_user_agent() == "my-shop-tools/2.1.0 (+https://example.com/app)"


def test_blank_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("ALLEGRO_APP_NAME", "   ")
    assert get_user_agent().startswith("allegro-mcp-server/")


def test_headers_merge_keeps_user_agent():
    headers = allegro_headers({"Accept": "application/json"})
    assert headers["Accept"] == "application/json"
    assert headers["User-Agent"] == get_user_agent()
