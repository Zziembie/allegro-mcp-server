"""OAuth2 for Allegro: device code flow, token storage and refresh."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from .user_agent import allegro_headers

# Refresh the access token this many seconds before it expires.
REFRESH_MARGIN_SECONDS = 300


class AuthError(RuntimeError):
    """Raised when no usable token is available."""


def is_token_valid(token: dict[str, Any], now: float | None = None) -> bool:
    """True if the stored access token is still valid, with a safety margin."""
    now = time.time() if now is None else now
    saved_at = float(token.get("saved_at", 0) or 0)
    expires_in = float(token.get("expires_in", 0) or 0)
    return bool(token.get("access_token")) and now < saved_at + expires_in - REFRESH_MARGIN_SECONDS


def write_token_file(path: Path, token: dict[str, Any]) -> None:
    """Write the token as JSON, readable only by the current user (0600)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(token, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


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
        try:
            return json.loads(self.token_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

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
        """Exchange a refresh token for a new token pair and store it."""
        try:
            resp = self._token_request(
                {"grant_type": "refresh_token", "refresh_token": refresh_token}
            )
        except httpx.HTTPError as exc:
            raise AuthError(f"Could not refresh the Allegro token: {exc}") from exc
        if resp.status_code != 200:
            raise AuthError(
                f"Allegro rejected the refresh token (HTTP {resp.status_code}). "
                "Log in again with: allegro-mcp-server --auth"
            )
        token = resp.json()
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
        refresh_token = token.get("refresh_token")
        if not refresh_token:
            raise AuthError(
                "The Allegro token has expired and has no refresh token. "
                "Log in again with: allegro-mcp-server --auth"
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
