import json
import os
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


def expired_token(auth, **extra):
    write_token_file(
        auth.token_file,
        dict(access_token="old", refresh_token="r1", expires_in=1, saved_at=0, **extra),
    )


def test_rejected_refresh_shows_allegro_error_code(tmp_path):
    auth = make_auth(tmp_path, lambda request: httpx.Response(400, json={"error": "invalid_grant"}))
    expired_token(auth)
    with pytest.raises(AuthError, match="HTTP 400, invalid_grant"):
        auth.get_access_token()


@pytest.mark.parametrize("status", [429, 500, 503])
def test_temporary_refresh_failure_does_not_ask_for_login(tmp_path, status):
    auth = make_auth(tmp_path, lambda request: httpx.Response(status))
    expired_token(auth)
    with pytest.raises(AuthError) as excinfo:
        auth.get_access_token()
    assert f"HTTP {status}" in str(excinfo.value)
    assert "--auth" not in str(excinfo.value)
    assert auth.load_token()["refresh_token"] == "r1"


def test_network_error_during_refresh_keeps_token(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    auth = make_auth(tmp_path, handler)
    expired_token(auth)
    with pytest.raises(AuthError, match="Could not reach Allegro") as excinfo:
        auth.get_access_token()
    assert "--auth" not in str(excinfo.value)
    assert auth.load_token()["refresh_token"] == "r1"


def test_refresh_timeout_has_its_own_message(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    auth = make_auth(tmp_path, handler)
    expired_token(auth)
    with pytest.raises(AuthError, match="did not answer"):
        auth.get_access_token()


def test_refresh_without_access_token_is_an_error(tmp_path):
    auth = make_auth(tmp_path, lambda request: httpx.Response(200, json={"scope": "x"}))
    expired_token(auth)
    with pytest.raises(AuthError, match="without an access token"):
        auth.get_access_token()
    assert auth.load_token()["access_token"] == "old"


def test_unreadable_token_file_is_not_reported_as_missing(tmp_path):
    auth = make_auth(tmp_path)
    auth.token_file.parent.mkdir(parents=True)
    auth.token_file.mkdir()  # reading a directory raises OSError, as a locked file would
    with pytest.raises(AuthError, match="Could not read the token file") as excinfo:
        auth.get_access_token()
    assert "errno" in str(excinfo.value)
    assert "--auth" not in str(excinfo.value)


def test_broken_symlink_is_named(tmp_path):
    auth = make_auth(tmp_path)
    auth.token_file.parent.mkdir(parents=True)
    os.symlink(tmp_path / "gone.json", auth.token_file)
    with pytest.raises(AuthError, match="symlink to a missing file"):
        auth.get_access_token()


def test_corrupt_token_file_is_retried_then_reported(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr("allegro_mcp.auth.time.sleep", sleeps.append)
    auth = make_auth(tmp_path)
    auth.token_file.parent.mkdir(parents=True)
    auth.token_file.write_text("{not json")
    with pytest.raises(AuthError, match="not valid JSON"):
        auth.get_access_token()
    assert len(sleeps) == 2


def test_token_file_that_recovers_is_read(tmp_path, monkeypatch):
    auth = make_auth(tmp_path)
    auth.token_file.parent.mkdir(parents=True)
    auth.token_file.write_text("")

    def finish_write(_seconds):
        auth.save_token(dict(access_token="ready", expires_in=3600))

    monkeypatch.setattr("allegro_mcp.auth.time.sleep", finish_write)
    assert auth.get_access_token() == "ready"


def test_save_through_symlink_keeps_the_link(tmp_path):
    real = tmp_path / "secure" / "token.json"
    real.parent.mkdir()
    write_token_file(real, dict(access_token="a"))
    auth = make_auth(tmp_path)
    auth.token_file.parent.mkdir(parents=True)
    os.symlink(real, auth.token_file)
    auth.save_token(dict(access_token="b", expires_in=3600))
    assert auth.token_file.is_symlink()
    assert json.loads(real.read_text())["access_token"] == "b"
    assert stat.S_IMODE(real.stat().st_mode) == 0o600


def test_token_refreshed_by_another_process_is_reused(tmp_path, monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=dict(access_token="mine", refresh_token="r3", expires_in=3600))

    auth = make_auth(tmp_path, handler)
    expired_token(auth)
    real_lock = auth._refresh_lock

    def lock_after_other_process_refreshed():
        # Simulates a second process that refreshed while we waited for the lock.
        write_token_file(
            auth.token_file,
            dict(access_token="theirs", refresh_token="r2", expires_in=3600, saved_at=time.time()),
        )
        return real_lock()

    monkeypatch.setattr(auth, "_refresh_lock", lock_after_other_process_refreshed)
    assert auth.get_access_token() == "theirs"
    assert calls == []


def test_refresh_creates_private_lock_file(tmp_path):
    auth = make_auth(
        tmp_path,
        lambda request: httpx.Response(200, json=dict(access_token="n", refresh_token="r2", expires_in=3600)),
    )
    expired_token(auth)
    auth.get_access_token()
    assert auth.lock_file.exists()
    assert stat.S_IMODE(auth.lock_file.stat().st_mode) == 0o600
