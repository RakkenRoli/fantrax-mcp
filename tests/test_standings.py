"""Real getStandings(view=SCHEDULE) response captured 2026-10-03 (week 1 in progress)."""
import asyncio
import json
from pathlib import Path

import pytest


from fantrax_mcp.categories import (ALL_SCIP, GOALIE_SCIP, SKATER_SCIP, TEAM_CODES,  # noqa: E402
                                    check_against_live, label, label_keys)
from fantrax_mcp.standings import _num, parse_schedule, slim_raw  # noqa: E402

RAW = json.loads((Path(__file__).parent / "fixtures" / "standings_schedule_2026-10-03.json")
                 .read_text(encoding="utf-8"))
SCHED = parse_schedule(RAW)
CATS = ["G", "A", "PIM", "SOG", "PPG", "Hit", "TOI", "Tk", "PPA", "Blk", "FOW",
        "W", "GAA", "SV%", "SV"]
GTX, OC, ACP = "6ayw03ncmtr5w0js", "wge2dwlrmtr5w0js", "20lpzud7mtsa2wxf"


# ---------------------------------------------------------------- category ids
def test_category_ids_pinned():
    # From Fantrax's own tableHeader. G and A are NOT 2090/2130 in that order.
    assert SKATER_SCIP == {
        "2010#2130#-1": "G", "2010#2090#-1": "A", "2010#2170#-1": "PIM", "2010#2270#-1": "SOG",
        "2010#2210#-1": "PPG", "2010#2200#-1": "PPA", "2010#2147#-1": "Hit",
        "2010#2092#-1": "Blk", "2010#2295#-1": "Tk", "2010#2096#-1": "FOW", "2010#2300#-1": "TOI"}
    assert GOALIE_SCIP == {"2020#231b#-1": "W", "2020#2320#-1": "GAA",
                           "2020#2230#-1": "SV", "2020#2330#-1": "SV%"}
    assert ALL_SCIP["2020#2140#-1"] == "GA" and ALL_SCIP["2020#2280#-1"] == "SA"
    assert label("2130") == "G" and label("2090") == "A" and label("231b") == "W"
    assert label_keys({"_2010": {"2130": 3, "2096": 9}}) == {"_2010": {"G": 3, "FOW": 9}}


def test_category_drift_detection():
    live = {k: v for k, v in ALL_SCIP.items()} | {"2020#2280#-1": "SOGA", "2020#2298#-1": "Min"}
    assert check_against_live(live)["ok"]
    live["2010#2130#-1"] = "A"
    bad = check_against_live(live)
    assert not bad["ok"] and "2010#2130#-1" in bad["mismatched"]


# ---------------------------------------------------------------- team codes
def test_stable_team_codes_cover_league():
    assert set(RAW["fantasyTeamInfo"]) == set(TEAM_CODES)
    by_short = {RAW["fantasyTeamInfo"][t]["shortName"]: TEAM_CODES[t] for t in TEAM_CODES}
    assert by_short["WPG"] == "TN" and by_short["NT 9"] == "RSD" and by_short["NT 13"] == "ACP"
    assert len(set(TEAM_CODES.values())) == 13


# ---------------------------------------------------------------- week 1 matchups
def test_week1_pairings_and_bye():
    w = SCHED[1]
    got = {f"{p['away']['code']}-{p['home']['code']}" for p in w["pairings"]}
    assert got == {"AVR-UV", "BVB-TN", "DTG-RSD", "GTX-OC", "HOL-GBH", "KJD-KWC"}
    assert [b["code"] for b in w["byes"]] == ["ACP"] and w["note"] is None


def test_week1_totals_equal_standings_table_for_all_matchups():
    """Done-when for P1: every category total equals the Fantrax table, all 6 matchups."""
    table = RAW["tableList"][0]
    cols = [c["shortName"] for c in table["header"]["cells"]][4:]
    by_team = {r["fixedCells"][0]["teamId"]: r for r in table["rows"]}
    assert cols == CATS
    for p in SCHED[1]["pairings"]:
        for side in (p["away"], p["home"]):
            cells = by_team[side["team_id"]]["cells"]
            assert side["wlt"] == [int(cells[i]["content"]) for i in range(3)]
            for name, cell in zip(cols, cells[4:]):
                assert side["totals"][name] == pytest.approx(_num(cell)), (side["code"], name)
                assert side["by_category"][name] == {1: "win", -1: "loss", 0: "tie"}[cell["gainColor"]]


def test_week1_gtx_oc_matches_known_values():
    p = next(p for p in SCHED[1]["pairings"] if p["away"]["team_id"] == GTX)
    me, opp = p["away"], p["home"]
    assert (me["totals"]["G"], opp["totals"]["G"]) == (3, 6)
    assert round(me["totals"]["GAA"], 2) == 5.27 and round(opp["totals"]["GAA"], 2) == 1.99
    assert me["totals"]["TOI"] == pytest.approx(530.1666, abs=1e-3)   # decimal minutes
    assert me["wlt"] == [9, 5, 1] and me["category_points"] == 9.5
    assert me["by_category"]["GAA"] == "loss" and me["by_category"]["Hit"] == "win"


# ---------------------------------------------------------------- schedule shape
def test_every_period_has_six_pairings_and_one_bye_or_a_reason():
    for k, v in SCHED.items():
        if v["is_playoffs"]:
            assert v["note"] and k >= 23
        elif k in (20, 22):
            assert len(v["pairings"]) == 5 and len(v["byes"]) == 3 and "3 byes" in v["note"]
        else:
            assert len(v["pairings"]) == 6 and len(v["byes"]) == 1 and v["note"] is None, k


def test_three_bye_weeks_even_out_byes():
    """Why weeks 20 and 22 have 3 byes: every team ends the regular season with exactly 2."""
    from collections import Counter
    c = Counter(b["code"] for v in SCHED.values() if not v["is_playoffs"] for b in v["byes"])
    assert set(c.values()) == {2} and len(c) == 13


def test_slim_raw_keeps_one_period():
    s = slim_raw(RAW, 3)
    assert len(s["tableList"]) == 1 and s["tableList"][0]["caption"] == "Scoring Period 3"


# ---------------------------------------------------------------- tools
@pytest.fixture
def server(monkeypatch):
    from fantrax_mcp import server as srv

    async def schedule(ttl=120):
        return SCHED

    async def teams():
        return {k: v["name"] for k, v in RAW["fantasyTeamInfo"].items()}

    async def periods():
        return ["1 (Sep 29 - Oct 4)", "2 (Oct 5 - Oct 11)"]

    async def live(period):
        raise RuntimeError("offline")

    monkeypatch.setattr(srv.FX, "schedule", schedule)
    monkeypatch.setattr(srv.FX, "teams", teams)
    monkeypatch.setattr(srv.FX, "periods", periods)
    monkeypatch.setattr(srv.FX, "live_scoring", live)
    return srv


def test_get_matchup_uses_cumulative_table(server):
    m = asyncio.run(server.get_matchup(1))
    assert m["opponent"]["code"] == "OC" and m["home_or_away"] == "away"
    assert m["totals"]["team"]["G"] == 3 and m["totals"]["opponent"]["G"] == 6
    assert m["current_wlt"] == [9, 5, 1]
    assert "projection_error" in m           # live scoring down must not break totals
    assert m["fetched_at"] and m["nhl_date"]


def test_get_matchup_by_code_and_bye(server):
    m = asyncio.run(server.get_matchup(1, team="ACP"))
    assert m["bye"] is True and m["team"]["code"] == "ACP"
    m = asyncio.run(server.get_matchup(1, team="OC"))
    assert m["opponent"]["code"] == "GTX" and m["home_or_away"] == "home"


def test_get_league_matchups(server):
    lm = asyncio.run(server.get_league_matchups(1))
    assert sorted(p["matchup"] for p in lm["pairings"]) == \
        ["AVR-UV", "BVB-TN", "DTG-RSD", "GTX-OC", "HOL-GBH", "KJD-KWC"]
    assert lm["byes"][0]["code"] == "ACP"
    assert all(len(p["away"]["totals"]) == 15 for p in lm["pairings"])


def test_goalie_rate_recompute():
    from fantrax_mcp.server import recompute_goalie_rates
    # Swayman YTD 2026-10-03: GA 3, SA 57, MIN 122:25 -> Fantrax GAA 1.47, SV% .947
    r = recompute_goalie_rates(3, 57, 122 + 25 / 60)
    assert round(r["GAA"], 2) == 1.47 and round(r["SV%"], 3) == 0.947
    assert recompute_goalie_rates(0, 0, 0) == {"GAA": None, "SV%": None}


def test_goalie_components_parse(monkeypatch):
    """Header + row shape from getPlayerStats POS_201 scoringCategoryType=1, 2026-10-03."""
    from fantrax_mcp import server as srv
    ids = ["2020#2100#-1", "2020#2298#-1", "2020#231b#-1", "2020#2150#-1", "2020#2160#-1",
           "2020#2320#-1", "2020#2330#-1", "2020#2290#-1", "2020#2140#-1", "2020#2280#-1",
           "2020#2230#-1"]
    header = {"cells": [{"shortName": "Rk"}, {"shortName": "Sta"}, {"shortName": "Opp"},
                        {"shortName": "Score"}, {"shortName": "Ros"}, {"shortName": "+/-"}]
              + [{"scipId": i} for i in ids]}
    vals = ["2", "122:25", "2", "0", "0", "1.47", ".947", "1", "3", "57", "54"]
    resp = {"tableHeader": header, "statsTable": [{
        "scorer": {"name": "Jeremy Swayman", "scorerId": "04f24", "teamShortName": "BOS"},
        "cells": [{"content": c} for c in ["1", "OC", "", "100", "97%", "0%"] + vals]}]}

    async def codes():
        return {"YTD": ("SEASON_31n_YEAR_TO_DATE", "YEAR_TO_DATE")}

    async def cached(ttl, method, **data):
        assert data["scoringCategoryType"] == "1" and data["positionOrGroup"] == "POS_201"
        return resp

    monkeypatch.setattr(srv.FX, "season_codes", codes)
    monkeypatch.setattr(srv.FX, "cached", cached)
    (g,) = asyncio.run(srv.FX.goalie_components("ALL", "YTD"))
    assert g["stats"] == {"GP": 2, "MIN": 122.42, "W": 2, "GAA": 1.47, "SV%": 0.947,
                          "GA": 3, "SA": 57, "SV": 54}


def test_healthz_endpoint(monkeypatch):
    from fantrax_mcp import server as srv

    async def health():
        return {"ok": False, "error": "cookie expired"}

    monkeypatch.setattr(srv, "_health", health)
    sent = []

    async def send(msg):
        sent.append(msg)

    async def app(scope, receive, send_):
        raise AssertionError("must not reach MCP app")

    guard = srv.BearerAuth(app, "tok")
    scope = {"type": "http", "path": "/healthz", "headers": [(b"authorization", b"Bearer tok")]}
    asyncio.run(guard(scope, None, send))
    assert sent[0]["status"] == 503 and b"cookie expired" in sent[1]["body"]
    sent.clear()
    asyncio.run(guard({**scope, "headers": []}, None, send))
    assert sent[0]["status"] == 401
