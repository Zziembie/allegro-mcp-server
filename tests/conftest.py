import pytest

_ENV_VARS = (
    "ALLEGRO_CLIENT_ID",
    "ALLEGRO_CLIENT_SECRET",
    "ALLEGRO_APP_NAME",
    "ALLEGRO_APP_VERSION",
    "ALLEGRO_APP_URL",
    "ALLEGRO_TOKEN_FILE",
    "ALLEGRO_ENABLE_WRITE_TOOLS",
    "ALLEGRO_SANDBOX",
    "ALLEGRO_ENV_FILE",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Isolate every test from the developer's real environment."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
