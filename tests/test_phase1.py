import httpx
import pytest

import config
import db
from fpl_client import FPLClient, FPLError, FPLNotFound
from main import format_squad


def make_client(handler, **kw):
    transport = httpx.MockTransport(handler)
    c = httpx.AsyncClient(base_url=config.FPL_BASE_URL, transport=transport, headers={"User-Agent": config.FPL_USER_AGENT})
    return FPLClient(client=c, backoff=0, **kw)


@pytest.mark.asyncio
async def test_retries_then_succeeds_and_sends_user_agent():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        assert "Mozilla" in req.headers["user-agent"]
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, json={"ok": 1})

    assert await make_client(handler).entry(1) == {"ok": 1}
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_gives_up_after_retries():
    with pytest.raises(FPLError):
        await make_client(lambda r: httpx.Response(500)).entry(1)


@pytest.mark.asyncio
async def test_404_not_retried():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(404)

    with pytest.raises(FPLNotFound):
        await make_client(handler).entry(1)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_bootstrap_cached_and_stale_fallback():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json={"events": [{"id": 5, "is_current": True}]})

    c = make_client(handler)
    await c.bootstrap()
    await c.bootstrap()
    assert calls["n"] == 1
    assert await c.current_gameweek() == 5
    c._bootstrap_at = 0  # expire
    c._client = httpx.AsyncClient(base_url=config.FPL_BASE_URL, transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    c.retries = 1
    assert (await c.bootstrap())["events"][0]["id"] == 5  # stale served


def test_db_and_admin_bypass(tmp_path, monkeypatch):
    p = str(tmp_path / "t.db")
    db.init_db(p)
    db.set_team(42, 1234, p)
    db.set_team(42, 5678, p)
    assert db.get_user(42, p)["fpl_team_id"] == 5678
    assert not db.is_subscribed(42, p)
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_IDS", frozenset({99}))
    assert db.is_subscribed(99, p)  # admin, not even in table


def test_parse_admin_ids():
    assert config._parse_ids("1, 2,x,3") == frozenset({1, 2, 3})


def test_format_squad():
    players = {i: {"web_name": f"P{i}", "team": 1, "element_type": 1 if i < 3 else 3} for i in range(1, 16)}
    picks = [{"element": i, "is_captain": i == 3, "is_vice_captain": i == 4} for i in range(1, 16)]
    out = format_squad(picks, players, {1: {"short_name": "ARS"}})
    assert "P3 (C)" in out and "Bench" in out and "ARS" in out
