"""resolve_team / get_roster accept 'me', code, id and name part for all 13 teams."""
import asyncio

import pytest

from fantrax_mcp.categories import TEAM_CODES

NAMES = {"le98eijtmtr5w0js": "Avas Raiders", "6ayw03ncmtr5w0js": "Gazdagréti Taxisok",
         "hzvy7u89mtr5w0js": "RS Devils", "pcn509g7mtr5w0js": "Dropping the Gloves"}


@pytest.fixture
def srv(monkeypatch):
    from fantrax_mcp import server

    async def teams():
        return {t: NAMES.get(t, f"Team {c}") for t, c in TEAM_CODES.items()}

    async def roster(team, timeframe):
        tid, name = await server.FX.resolve_team(team)
        return {"team_id": tid, "team_name": name, "players": []}

    monkeypatch.setattr(server.FX, "teams", teams)
    monkeypatch.setattr(server.FX, "roster", roster)
    return server


@pytest.mark.parametrize("tid,code", sorted(TEAM_CODES.items()))
def test_every_code_resolves(srv, tid, code):
    for q in (code, code.lower(), f" {code} ", tid):
        assert asyncio.run(srv.FX.resolve_team(q))[0] == tid


def test_me_and_name_part(srv):
    assert asyncio.run(srv.FX.resolve_team("me"))[0] == "6ayw03ncmtr5w0js"
    assert asyncio.run(srv.FX.resolve_team("dropping"))[0] == "pcn509g7mtr5w0js"


def test_get_roster_by_code(srv):
    out = asyncio.run(srv.get_roster("AVR"))
    assert out["team_id"] == "le98eijtmtr5w0js" and out["team_name"] == "Avas Raiders"


def test_unknown_team_is_a_readable_error(srv):
    out = asyncio.run(srv.get_roster("XYZ"))
    assert "error" in out and "AVR (Avas Raiders)" in out["error"]
