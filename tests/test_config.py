from pathlib import Path

import pytest

from allegro_mcp.config import (
    PRODUCTION_API_BASE,
    SANDBOX_API_BASE,
    ConfigError,
    load_settings,
    resolve_token_file,
)


def test_missing_credentials_names_both_variables():
    with pytest.raises(ConfigError) as exc:
        load_settings()
    assert "ALLEGRO_CLIENT_ID" in str(exc.value)
    assert "ALLEGRO_CLIENT_SECRET" in str(exc.value)


def test_missing_secret_only(monkeypatch):
    monkeypatch.setenv("ALLEGRO_CLIENT_ID", "id")
    with pytest.raises(ConfigError) as exc:
        load_settings()
    assert "ALLEGRO_CLIENT_SECRET" in str(exc.value)
    assert "ALLEGRO_CLIENT_ID," not in str(exc.value)


def test_token_file_default(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_token_file() == tmp_path / ".config" / "allegro-mcp" / "token.json"
    assert resolve_token_file(sandbox=True).name == "token-sandbox.json"


def test_token_file_from_env(monkeypatch, tmp_path):
    target = tmp_path / "custom.json"
    monkeypatch.setenv("ALLEGRO_TOKEN_FILE", str(target))
    assert resolve_token_file() == target


def test_token_file_expands_user(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALLEGRO_TOKEN_FILE", "~/tok.json")
    assert resolve_token_file() == Path(tmp_path) / "tok.json"


@pytest.mark.parametrize("value,expected", [("1", True), ("true", True), ("YES", True), ("0", False), ("", False)])
def test_write_tools_flag(monkeypatch, value, expected):
    monkeypatch.setenv("ALLEGRO_CLIENT_ID", "id")
    monkeypatch.setenv("ALLEGRO_CLIENT_SECRET", "secret")
    monkeypatch.setenv("ALLEGRO_ENABLE_WRITE_TOOLS", value)
    assert load_settings().enable_write_tools is expected


def test_sandbox_switches_api_base(monkeypatch):
    monkeypatch.setenv("ALLEGRO_CLIENT_ID", "id")
    monkeypatch.setenv("ALLEGRO_CLIENT_SECRET", "secret")
    assert load_settings().api_base == PRODUCTION_API_BASE
    monkeypatch.setenv("ALLEGRO_SANDBOX", "1")
    assert load_settings().api_base == SANDBOX_API_BASE
