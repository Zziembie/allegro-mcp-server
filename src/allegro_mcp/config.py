"""Runtime configuration read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PRODUCTION_AUTH_BASE = "https://allegro.pl/auth/oauth"
PRODUCTION_API_BASE = "https://api.allegro.pl"
SANDBOX_AUTH_BASE = "https://allegro.pl.allegrosandbox.pl/auth/oauth"
SANDBOX_API_BASE = "https://api.allegro.pl.allegrosandbox.pl"

_TRUE_VALUES = {"1", "true", "yes", "on"}


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def env_flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in _TRUE_VALUES


def resolve_token_file(sandbox: bool = False) -> Path:
    """Token location: ALLEGRO_TOKEN_FILE, else ~/.config/allegro-mcp/token.json."""
    if env := (os.getenv("ALLEGRO_TOKEN_FILE") or "").strip():
        return Path(env).expanduser()
    filename = "token-sandbox.json" if sandbox else "token.json"
    return Path.home() / ".config" / "allegro-mcp" / filename


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    token_file: Path
    enable_write_tools: bool = False
    sandbox: bool = False

    @property
    def auth_base(self) -> str:
        return SANDBOX_AUTH_BASE if self.sandbox else PRODUCTION_AUTH_BASE

    @property
    def api_base(self) -> str:
        return SANDBOX_API_BASE if self.sandbox else PRODUCTION_API_BASE


def load_settings() -> Settings:
    """Read settings from the environment. Raises ConfigError if credentials are missing."""
    client_id = (os.getenv("ALLEGRO_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("ALLEGRO_CLIENT_SECRET") or "").strip()
    missing = [
        name
        for name, value in (
            ("ALLEGRO_CLIENT_ID", client_id),
            ("ALLEGRO_CLIENT_SECRET", client_secret),
        )
        if not value
    ]
    if missing:
        raise ConfigError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "Register an application at https://developer.allegro.pl and set them "
            "in the MCP client config or in a .env file (see .env.example)."
        )
    sandbox = env_flag("ALLEGRO_SANDBOX")
    return Settings(
        client_id=client_id,
        client_secret=client_secret,
        token_file=resolve_token_file(sandbox),
        enable_write_tools=env_flag("ALLEGRO_ENABLE_WRITE_TOOLS"),
        sandbox=sandbox,
    )
