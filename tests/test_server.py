import asyncio
import importlib
import json

import pytest

from allegro_mcp import server
from allegro_mcp.server import READ_TOOLS, WRITE_TOOLS, build_server, dispatch, enabled_tools


def test_import_has_no_side_effects():
    # Importing must not require credentials or touch the network.
    importlib.reload(server)


def test_write_tools_hidden_by_default():
    names = {spec.name for spec in enabled_tools(False)}
    assert names == {spec.name for spec in READ_TOOLS}
    assert not names & {spec.name for spec in WRITE_TOOLS}


def test_write_tools_listed_when_enabled():
    names = {spec.name for spec in enabled_tools(True)}
    assert {spec.name for spec in WRITE_TOOLS} <= names


def test_tool_names_are_unique():
    names = [spec.name for spec in READ_TOOLS + WRITE_TOOLS]
    assert len(names) == len(set(names))


def _tools(enable_write_tools):
    return {spec.name: spec for spec in enabled_tools(enable_write_tools)}


def test_disabled_write_tool_is_rejected():
    result = asyncio.run(
        dispatch(_tools(False), None, "allegro_set_offer_price", {"offer_id": "1", "amount": "1"})
    )
    assert result.is_error
    assert "ALLEGRO_ENABLE_WRITE_TOOLS" in result.content[0].text


def test_unknown_tool_and_missing_arguments_are_errors():
    unknown = asyncio.run(dispatch(_tools(False), None, "nope", {}))
    assert unknown.is_error and "Unknown tool" in unknown.content[0].text
    missing = asyncio.run(dispatch(_tools(False), None, "allegro_get_offer", {}))
    assert missing.is_error and "offer_id" in missing.content[0].text


def test_dispatch_returns_json_and_wraps_exceptions():
    class FakeClient:
        def get_offer(self, offer_id):
            return {"id": offer_id, "name": "Zażółć"}

        def get_me(self):
            raise RuntimeError("boom")

    ok = asyncio.run(dispatch(_tools(False), FakeClient(), "allegro_get_offer", {"offer_id": "7"}))
    assert not ok.is_error
    assert json.loads(ok.content[0].text) == {"id": "7", "name": "Zażółć"}
    failed = asyncio.run(dispatch(_tools(False), FakeClient(), "allegro_get_me", {}))
    assert failed.is_error and "boom" in failed.content[0].text


def test_tool_annotations_mark_write_tools():
    for spec in READ_TOOLS:
        assert spec.as_tool().annotations.read_only_hint is True
    for spec in WRITE_TOOLS:
        tool = spec.as_tool()
        assert tool.annotations.read_only_hint is False
        assert tool.annotations.destructive_hint is True


def test_build_server_without_client():
    assert build_server(client=None, enable_write_tools=False).name == "allegro-mcp-server"


def test_run_without_credentials_exits_with_message(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        server.run([])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "ALLEGRO_CLIENT_ID" in err and "ALLEGRO_CLIENT_SECRET" in err
