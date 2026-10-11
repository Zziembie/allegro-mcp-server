"""User-Agent header required by the Allegro REST API.

Allegro requires every request to carry a User-Agent in the form
``AppName/Version (+URL)``. The name should match the application registered
at developer.allegro.pl. All parts are read from the environment so that no
application identity is baked into the code.
"""

from __future__ import annotations

import os

from . import __version__

DEFAULT_APP_NAME = "allegro-mcp-server"
DEFAULT_APP_VERSION = __version__
DEFAULT_APP_URL = "https://github.com/"


def _env(key: str, fallback: str) -> str:
    return (os.getenv(key) or "").strip() or fallback


def get_user_agent() -> str:
    """Build the User-Agent value from ALLEGRO_APP_NAME/VERSION/URL."""
    name = _env("ALLEGRO_APP_NAME", DEFAULT_APP_NAME)
    version = _env("ALLEGRO_APP_VERSION", DEFAULT_APP_VERSION)
    url = _env("ALLEGRO_APP_URL", DEFAULT_APP_URL)
    return f"{name}/{version} (+{url})"


def allegro_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return request headers with the User-Agent set, merged with ``extra``."""
    headers = {"User-Agent": get_user_agent()}
    if extra:
        headers.update(extra)
    return headers
