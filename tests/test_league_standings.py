"""get_league_standings parser.

Two layers:
  * shape tests on a minimal hand-built response that mirrors the real getStandings
    layout (H2hRotisserie1 table, positional cells named by header keys, division tabs);
  * real-fixture tests that run once scripts/capture_standings_fixtures.py output is
    committed to tests/fixtures/ (standings_combined_*.json + standings_division_*.json).
"""
import asyncio
import json
from pathlib import Path

import pytest

from fantrax_mcp.categories import TEAM_CODES, team_code
from fantrax_mcp.league_standings import build_standings

FIX = Path(__file__).parent / "fixtures"
KEYS = ["win", "loss", "tie", "pts", "winpc", "div", "gb", "wwOrder",
        "maxClaimsFree", "maxClaimsWaiver", "maxClaimsSeason", "cpf", "cpa"]
EAST, WEST = "_east", "_west"


def _table(teams):
    """teams: [(team_id, name, cells_by_key)] in display order."""
    return {"tableType": "H2hRotisserie1",
            "header": {"cells": [{"key": k, "name": "2 points for win, 1 point for a tie"
                                  if k == "pts" else k} for k in KEYS]},
            "rows": [{"fixedCells": [{"content": str(i)}, {"content": name, "teamId": tid}],
                      "cells": [{"content": vals.get(k, "0")} for k in KEYS]}
                     for i, (tid, name, vals) in enumerate(teams, 1)]}


def _resp(teams, info):
    return {"tableList": [{"tableType": "H2hRotisserie2", "rows": []}, _table(teams)],
            "fantasyTeamInfo": info,
            "displayedLists": {"tabs": [{"id": "COMBINED", "name": "Combined"},
                                        {"id": EAST, "name": "East Side"},
                                        {"id": WEST, "name": "West Side"},
                                        {"id": "SCHEDULE", "name": "Schedule"}]}}


def _fake_league():
    ids = list(TEAM_CODES)                      # 13 real team ids
    info = {t: {"name": f"Team {TEAM_CODES[t]}", "shortName": f"NT {i}"}
            for i, t in enumerate(ids, 1)}
    info["unknown_team_id"] = {"name": "Newcomers", "shortName": "NEW"}
    ids.append("unknown_team_id")
    rows = [(t, info[t]["name"],
             {"win": "1" if i == 0 else "0", "pts": "2" if i == 0 else "0",
              "winpc": "1.000" if i == 0 else "-", "div": "1-0-0" if i == 0 else "0-0-0",
              "gb": "0" if i == 0 else "0.5", "cpf": "9.5" if i == 0 else "0",
              "cpa": "5.5" if i == 0 else "0"})
            for i, t in enumerate(ids)]
    east, west = rows[:6], rows[6:]
    views = {"COMBINED": _resp(rows, info), EAST: _resp(east, info), WEST: _resp(west, info)}

    async def call(method, **kw):
        assert method == "getStandings"
        return views[kw["view"]]
    return call


def test_shape_rows_divisions_codes_and_parsing():
    out = asyncio.run(build_standings(_fake_league(), code_for=team_code))
    rows = out["rows"]
    assert len(rows) == 14 and [r["rank"] for r in rows] == list(range(1, 15))
    assert out["rule"] == "2 points for win, 1 point for a tie"
    by_div = {}
    for r in rows:
        by_div.setdefault(r["division"], []).append(r["division_rank"])
    assert by_div == {"East Side": [1, 2, 3, 4, 5, 6], "West Side": list(range(1, 9))}
    top = rows[0]
    assert top["code"] == TEAM_CODES[top["team_id"]]
    assert (top["w"], top["pts"], top["win_pct"], top["div_wlt"]) == (1, 2, 1, [1, 0, 0])
    assert (top["cat_pts_for"], top["cat_pts_against"]) == (9.5, 5.5)
    assert rows[1]["win_pct"] is None and rows[1]["gb"] == 0.5     # "-" -> null
    assert rows[-1]["code"] == "NEW"   # id missing from TEAM_CODES -> Fantrax shortName
    assert "fetched_at" in out and "nhl_date" in out


# ---------------------------------------------------------------- real fixtures
_COMBINED = sorted(FIX.glob("standings_combined_*.json"))
_DIVISIONS = sorted(FIX.glob("standings_division_*.json"))
real = pytest.mark.skipif(not (_COMBINED and _DIVISIONS),
                          reason="run scripts/capture_standings_fixtures.py and commit the output")


def _replay():
    combined = json.loads(_COMBINED[-1].read_text(encoding="utf-8"))
    tabs = {t["name"]: t["id"] for t in combined["displayedLists"]["tabs"]}
    views = {"COMBINED": combined}
    for f in _DIVISIONS:
        data = json.loads(f.read_text(encoding="utf-8"))
        name = f.stem.removeprefix("standings_division_").rsplit("_", 1)[0].replace("_", " ")
        tid = next(v for k, v in tabs.items() if k.lower() == name)
        views[tid] = data

    async def call(method, **kw):
        return views[kw["view"]]
    return call


@real
def test_real_fixture_13_rows_two_divisions_all_coded():
    out = asyncio.run(build_standings(_replay(), code_for=team_code))
    rows = out["rows"]
    assert len(rows) == 13
    assert {r["division"] for r in rows} == {"East Side", "West Side"}
    assert sorted(len([r for r in rows if r["division"] == d]) for d in ("East Side", "West Side")) == [6, 7]
    assert all(r["team_id"] in TEAM_CODES and r["code"] == TEAM_CODES[r["team_id"]] for r in rows)
    assert all(isinstance(r["div_wlt"], list) and len(r["div_wlt"]) == 3 for r in rows)
    assert all(r["win_pct"] is None or isinstance(r["win_pct"], (int, float)) for r in rows)
