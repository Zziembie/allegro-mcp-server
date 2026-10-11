import json
import stat
import time

import httpx
import pytest

from allegro_mcp.auth import AllegroAuth, AuthError, is_token_valid, write_token_file


def make_auth(tmp_path, handler=None):
    transport = httpx.MockTransport(handler or (lambda request: httpx.Response(500)))
    return AllegroAuth(
        "client-id",
        "client-secret",
        token_file=tmp_path / "cfg" / "token.json",
        auth_base="https://auth.example/oauth",
        http=httpx.Client(transport=transport),
    )


def test_is_token_valid_respects_margin():
    now = 1_000_000.0
    assert is_token_valid(dict(access_token="a", saved_at=now, expires_in=3600), now)
    assert not is_token_valid(dict(access_token="a", saved_at=now, expires_in=200), now)
    assert not is_token_valid({"saved_at": now, "expires_in": 3600}, now)
    assert not is_token_valid({}, now)


def test_write_token_file_is_private(tmp_path):
    path = tmp_path / "nested" / "token.json"
    write_token_file(path, dict(access_token="x"))
    assert json.loads(path.read_text()) == dict(access_token="x")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not path.with_name("token.json.tmp").exists()


def test_missing_token_points_to_auth_flag(tmp_path):
    with pytest.raises(AuthError, match="--auth"):
        make_auth(tmp_path).get_access_token()


def test_valid_token_is_returned_without_network(tmp_path):
    auth = make_auth(tmp_path)
    auth.save_token(dict(access_token="cached", expires_in=3600, refresh_token="r"))
    assert auth.get_access_token() == "cached"


def test_expired_token_is_refreshed_and_saved(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        seen["ua"] = request.headers["User-Agent"]
        return httpx.Response(200, json=dict(access_token="new", refresh_token="r2", expires_in=3600))

    auth = make_auth(tmp_path, handler)
    write_token_file(
        auth.token_file,
        dict(access_token="old", refresh_token="r1", expires_in=3600, saved_at=time.time() - 7200),
    )
    assert auth.get_access_token() == "new"
    assert seen["url"] == "https://auth.example/oauth/token"
    assert "grant_type=refresh_token" in seen["body"] and "refresh_token=r1" in seen["body"]
    assert seen["ua"].startswith("allegro-mcp-server/")
    assert auth.load_token()["refresh_token"] == "r2"


def test_rejected_refresh_raises_auth_error(tmp_path):
    auth = make_auth(tmp_path, lambda request: httpx.Response(400, json={"error": "invalid_grant"}))
    write_token_file(
        auth.token_file,
        dict(access_token="old", refresh_token="r1", expires_in=1, saved_at=0),
    )
    with pytest.raises(AuthError, match="--auth"):
        auth.get_access_token()
