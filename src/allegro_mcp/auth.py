"""OAuth2 for Allegro: device code flow, token storage and refresh."""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:  # POSIX only; on other systems the token is refreshed without a lock.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

import httpx

from .user_agent import allegro_headers

# Refresh the access token this many seconds before it expires.
REFRESH_MARGIN_SECONDS = 300

# A token file that does not parse is read again a few times before giving up:
# on synced or network folders a reader can briefly see a half-written file.
TOKEN_READ_ATTEMPTS = 3
TOKEN_READ_RETRY_SECONDS = 0.5

LOGIN_HINT = "Log in again with: allegro-mcp-server --auth"


class AuthError(RuntimeError):
    """Raised when no usable token is available."""


def is_token_valid(token: dict[str, Any], now: float | None = None) -> bool:
    """True if the stored access token is still valid, with a safety margin."""
    now = time.time() if now is None else now
    saved_at = float(token.get("saved_at", 0) or 0)
    expires_in = float(token.get("expires_in", 0) or 0)
    return bool(token.get("access_token")) and now < saved_at + expires_in - REFRESH_MARGIN_SECONDS


def write_token_file(path: Path, token: dict[str, Any]) -> None:
    """Write the token as JSON, readable only by the current user (0600).

    The write is atomic. If ``path`` is a symlink, the file it points to is
    replaced and the link stays in place.
    """
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(token, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _oauth_error_code(resp: httpx.Response) -> str:
    """The OAuth ``error`` field of a failed token response, or an empty string."""
    try:
        body = resp.json()
    except ValueError:
        return ""
    return str(body.get("error") or "") if isinstance(body, dict) else ""


class AllegroAuth:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_file: Path,
        auth_base: str,
        http: httpx.Client | None = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_file = token_file
        self.auth_base = auth_base.rstrip("/")
        self.http = http or httpx.Client(timeout=30)

    @property
    def token_url(self) -> str:
        return f"{self.auth_base}/token"

    @property
    def device_url(self) -> str:
        return f"{self.auth_base}/device"

    def load_token(self) -> dict[str, Any] | None:
        """Read the stored token. Returns None only when there is no token file.

        A file that exists but cannot be read or parsed raises AuthError with the
        reason, so that a permission or sync problem is not reported as "log in".
        """
        path = self.token_file
        if path.is_symlink() and not path.exists():
            raise AuthError(
                f"The token file {path} is a symlink to a missing file "
                f"({os.readlink(path)}). Fix the link or remove it. {LOGIN_HINT}"
            )
        problem = "empty file"
        for attempt in range(TOKEN_READ_ATTEMPTS):
            if attempt:
                time.sleep(TOKEN_READ_RETRY_SECONDS)
            try:
                text = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise AuthError(
                    f"Could not read the token file {path}: "
                    f"{exc.strerror or exc} (errno {exc.errno}). "
                    "The token was not changed; fix access to the file and try again."
                ) from exc
            try:
                token = json.loads(text)
            except ValueError as exc:
                problem = str(exc)
                continue
            if isinstance(token, dict):
                return token
            problem = "not a JSON object"
        raise AuthError(
            f"The token file {path} is not valid JSON ({problem}). "
            f"Delete it. {LOGIN_HINT}"
        )

    @property
    def lock_file(self) -> Path:
        token_path = self.token_file.resolve()
        return token_path.with_name(token_path.name + ".lock")

    @contextmanager
    def _refresh_lock(self) -> Iterator[None]:
        """Exclusive lock around read-refresh-write.

        Allegro refresh tokens are single use. Without the lock two processes
        (for example the MCP server and a cron job) can both refresh with the
        same token, and the one that loses the race is left with a revoked token.
        """
        if fcntl is None:
            yield
            return
        lock_path = self.lock_file
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def save_token(self, token: dict[str, Any]) -> None:
        token = {**token, "saved_at": time.time()}
        write_token_file(self.token_file, token)

    def _token_request(self, data: dict[str, str]) -> httpx.Response:
        return self.http.post(
            self.token_url,
            headers=allegro_headers(),
            auth=(self.client_id, self.client_secret),
            data=data,
        )

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        """Exchange a refresh token for a new token pair and store it.

        Only a rejected refresh token asks the user to log in again. Network
        errors and temporary server errors leave the stored token untouched.
        """
        unchanged = "The stored token is unchanged; try again later."
        try:
            resp = self._token_request(
                {"grant_type": "refresh_token", "refresh_token": refresh_token}
            )
        except httpx.TimeoutException as exc:
            raise AuthError(
                f"Allegro did not answer the token refresh in time. {unchanged}"
            ) from exc
        except httpx.HTTPError as exc:
            raise AuthError(
                "Could not reach Allegro to refresh the token "
                f"({type(exc).__name__}: {exc}). Check the network connection. {unchanged}"
            ) from exc
        status = resp.status_code
        if status == 429 or status >= 500:
            raise AuthError(
                f"Allegro could not refresh the token right now (HTTP {status}). {unchanged}"
            )
        if status != 200:
            code = _oauth_error_code(resp)
            detail = f", {code}" if code else ""
            raise AuthError(
                f"Allegro rejected the refresh token (HTTP {status}{detail}). {LOGIN_HINT}"
            )
        try:
            token = resp.json()
        except ValueError:
            token = None
        if not isinstance(token, dict) or not token.get("access_token"):
            raise AuthError(
                f"Allegro answered the token refresh without an access token. {unchanged}"
            )
        self.save_token(token)
        return token

    def get_access_token(self) -> str:
        """Return a valid access token, refreshing it when needed."""
        token = self.load_token()
        if not token:
            raise AuthError(
                f"No Allegro token found at {self.token_file}. "
                "Log in first with: allegro-mcp-server --auth"
            )
        if is_token_valid(token):
            return token["access_token"]
        with self._refresh_lock():
            # Another process may have refreshed while we waited for the lock.
            token = self.load_token() or {}
            if is_token_valid(token):
                return token["access_token"]
            refresh_token = token.get("refresh_token")
            if not refresh_token:
                raise AuthError(
                    f"The Allegro token has expired and has no refresh token. {LOGIN_HINT}"
                )
            return self.refresh(refresh_token)["access_token"]

    def device_code_flow(self) -> None:
        """Interactive device code login. Prints instructions to stderr and stores the token."""
        resp = self.http.post(
            self.device_url,
            headers=allegro_headers(),
            auth=(self.client_id, self.client_secret),
            data={"client_id": self.client_id},
        )
        resp.raise_for_status()
        device = resp.json()

        interval = int(device.get("interval", 5))
        expires_in = int(device.get("expires_in", 600))
        url = device.get("verification_uri_complete") or device["verification_uri"]
        print(
            "\nAllegro authorization required.\n"
            f"  1. Open: {url}\n"
            f"  2. Confirm the code: {device['user_code']}\n"
            f"Waiting for confirmation (expires in {expires_in}s)...",
            file=sys.stderr,
        )

        deadline = time.time() + expires_in
        while time.time() < deadline:
            time.sleep(interval)
            try:
                token_resp = self._token_request(
                    {
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "device_code": device["device_code"],
                    }
                )
            except httpx.HTTPError:
                continue
            if token_resp.status_code == 200:
                self.save_token(token_resp.json())
                print(f"Authorized. Token saved to {self.token_file}", file=sys.stderr)
                return
            error = token_resp.json().get("error", "")
            if error == "slow_down":
                interval += 5
            elif error != "authorization_pending":
                raise AuthError(f"Authorization failed: {error or token_resp.status_code}")
        raise AuthError("Authorization timed out.")
