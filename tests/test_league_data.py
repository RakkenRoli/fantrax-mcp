"""League-wide data tools. Row / header / roster shapes copied from live fxpa responses
captured 2026-10-03 (getPlayerStats BY_DATE 2026-10-02, getTeamRosterInfo for GTX)."""
import asyncio
from datetime import date, timedelta

import pytest

from fantrax_mcp.league_data import (GOALIE_RAW, SKATER_RAW, blank_projection_gaps,
                                     injury_status, owner_team_id, parse_day_periods,
                                     raw_value, roster_day_status, row_stats, scip_index,
                                     start_status)
from fantrax_mcp.lineup import LineupPlayer, plan_week

OC, RSD, GTX = "wge2dwlrmtr5w0js", "hzvy7u89mtr5w0js", "6ayw03ncmtr5w0js"
SK_HEADER = {"cells": [{"key": "status", "shortName": "Sta"}, {"key": "opponent"},
                       {"key": "score"}, {"shortName": "Ros"}, {"shortName": "+/-"}]
             + [{"scipId": f"2010#{i}#-1"} for i in
                ("2100", "2130", "2090", "2170", "2270", "2210", "2200", "2147", "2092",
                 "2295", "2096", "2300")]}


def _sk_row(sta, gp, toi, pid="03924", name="Jack Eichel", team="VGK", pos="C"):
    # Eichel 2026-10-02: GP 1, G 0, A 2, PIM 0, SOG 2, PPG 0, PPA 1, Hit 0, Blk 1, Tk 2, FOW 12, TOI 22:07
    vals = [gp, "0", "2", "0", "2", "0", "1", "0", "1", "2", "12", toi]
    return {"scorer": {"scorerId": pid, "name": name, "teamShortName": team, "posShortNames": pos},
            "cells": [sta, {"content": ""}, {"content": "100"}, {"content": "99%"},
                      {"content": "0%"}] + [{"content": v} for v in vals]}


OWNED = {"content": "OC", "toolTip": "Onga Capitals", "teamId": OC}
FA = {"content": "FA", "toolTip": "Free Agent"}


# ---------------------------------------------------------------- parsers
def test_cells_scip_mapping_and_owner():
    idx = scip_index(SK_HEADER, SKATER_RAW)
    st = row_stats(_sk_row(OWNED, "1", "22:07"), idx)
    assert st == {"GP": 1, "G": 0, "A": 2, "PIM": 0, "SOG": 2, "PPG": 0, "PPA": 1, "Hit": 0,
                  "Blk": 1, "Tk": 2, "FOW": 12, "TOI": round(22 + 7 / 60, 4)}
    assert owner_team_id(_sk_row(OWNED, "1", "1:00")) == OC
    assert owner_team_id(_sk_row(FA, "1", "1:00")) is None
    assert raw_value({"content": ""}) is None and raw_value({"content": ".789"}) == 0.789
    assert "GAA" not in GOALIE_RAW.values() and "SV%" not in GOALIE_RAW.values()


def test_projection_gaps_are_null_not_zero():
    st = {"GP": 81, "G": 29, "Tk": 0, "TOI": None}
    assert blank_projection_gaps(st, "PROJ_SEASON") == {"GP": 81, "G": 29, "Tk": None, "TOI": None}
    assert blank_projection_gaps({"Tk": 4}, "YTD") == {"Tk": 4}


def test_goalie_start_and_injury_notes():
    assert start_status(["Playing in upcoming/current game", "Oct 3, 5:00 PM: x"]) == "confirmed"
    assert start_status(["Expected to play in upcoming/current game"]) == "expected"
    assert start_status(["Oct 3, 4:17 PM: Thompson turned aside 31 of 33 shots"]) is None
    assert injury_status(["Contract dispute - Out Indefinitely",
                          "Sep 30, 4:09 PM: Edvinsson, who is a restricted free..."]) \
        == "Contract dispute - Out Indefinitely"
    assert injury_status(["Expected to play in upcoming/current game",
                          "Sep 30, 7:59 AM: Jarry allowed six goals"]) is None


def test_daily_period_calendar():
    days = parse_day_periods(["1 (Tue Sep 29)", "5 (Sat Oct 3)", "95 (Fri Jan 1)",
                              "188 (Sun Apr 4)", "junk"], 2026)
    assert days == {date(2026, 9, 29): 1, date(2026, 10, 3): 5, date(2027, 1, 1): 95,
                    date(2027, 4, 4): 188}


def test_roster_day_status_and_period_guard():
    resp = {"displayedSelections": {"displayedPeriod": 5},
            "tables": [{"rows": [{"scorer": {"scorerId": "0045u"}, "statusId": "1"},
                                 {"scorer": {"scorerId": "02aez"}, "statusId": "2"},
                                 {"statusId": "1"},                       # empty slot
                                 {"scorer": {"scorerId": "05nzz"}, "statusId": "3"}]}]}
    assert roster_day_status(resp, 5) == {"0045u": "active", "02aez": "bench", "05nzz": "ir"}
    with pytest.raises(RuntimeError, match="asked for 3"):
        roster_day_status(resp, 3)


def test_plan_week_returns_who_plays_where():
    def P(name, pos, team, pid):
        return LineupPlayer(name, set(pos.split(",")), team, True, {"fantrax_id": pid})
    roster = [P("c1", "C", "A", "1"), P("c2", "C", "A", "2"), P("c3", "C", "A", "3"),
              P("flex", "C,LW", "A", "4"), P("c4", "C", "A", "5"), P("off", "D", "B", "6")]
    d = date(2026, 10, 12)
    day = plan_week(roster, {d: {"A"}}, {"C": 3, "LW": 3, "RW": 3, "D": 6, "G": 2})[d.isoformat()]
    assert {(r["slot"], r["fantrax_id"]) for r in day["lineup"]} >= {("LW", "4")}
    assert len(day["lineup"]) == 4 and [u["fantrax_id"] for u in day["unused"]] == ["5"]
    assert all(r["fantrax_id"] != "6" for r in day["lineup"] + day["unused"])   # no game


# ---------------------------------------------------------------- client paging
def test_stats_rows_paging_stops_after_idle_pages(monkeypatch):
    from fantrax_mcp import server
    calls = []

    async def season_codes():
        return {"BY_DATE": ("SEASON_31n_BY_DATE", "BY_DATE"), "YTD": ("SEASON_31n_YEAR_TO_DATE", "YEAR_TO_DATE")}

    async def cached(ttl, method, **kw):
        calls.append(kw)
        page = int(kw["pageNumber"])
        gp = "1" if page == 1 else "0"
        assert kw["startDate"] == kw["endDate"] == "2026-10-02"
        assert kw["seasonOrProjection"] == "SEASON_31n_BY_DATE"
        return {"tableHeader": SK_HEADER, "paginatedResultSet": {"totalNumPages": 16},
                "statsTable": [_sk_row(OWNED, gp, "10:00", pid=f"p{page}")]}

    monkeypatch.setattr(server.FX, "season_codes", season_codes)
    monkeypatch.setattr(server.FX, "cached", cached)
    rows = asyncio.run(server.FX.stats_rows("ALL", "HOCKEY_SKATING", day=date(2026, 10, 2),
                                            stop_after_idle_pages=2, played_key="GP"))
    assert len(calls) == 3 and [r["fantrax_id"] for r in rows] == ["p1", "p2", "p3"]
    assert rows[0]["owner_team_id"] == OC


# ---------------------------------------------------------------- tools
@pytest.fixture
def srv(monkeypatch):
    from fantrax_mcp import server

    async def teams():
        return {OC: "Onga Capitals", RSD: "RS Devils", GTX: "Gazdagréti Taxisok"}

    async def roster_on(tid, d, ttl=300):
        return {OC: {"03924": "active"}, RSD: {"03kra": "bench", "g1": "active"},
                GTX: {"g2": "active", "g3": "bench"}}[tid]

    monkeypatch.setattr(server.FX, "teams", teams)
    monkeypatch.setattr(server.FX, "roster_on", roster_on)
    return server


def _rec(pid, name, pos, team, owner, stats):
    return {"fantrax_id": pid, "name": name, "positions": pos, "nhl_team": team,
            "owner_team_id": owner, "stats": stats}


def test_get_daily_player_stats(srv, monkeypatch):
    sk = {"GP": 1, "G": 0, "A": 2, "PIM": 0, "SOG": 2, "PPG": 0, "PPA": 1, "Hit": 0,
          "Blk": 1, "Tk": 2, "FOW": 12, "TOI": 22.1167}

    async def stats_rows(flt, group, timeframe=None, day=None, standard=False, **kw):
        assert flt == "ALL" and day == date(2026, 10, 2)
        if group == "POS_201":
            assert standard
            return [_rec("g1", "Goalie One", ["G"], "NJD", RSD,
                         {"GP": 1, "W": 1, "GA": 2, "SA": 25, "SV": 23, "MIN": 60.0}),
                    _rec("g9", "Backup", ["G"], "NJD", None, {"GP": 0})]
        return [_rec("03924", "Jack Eichel", ["C"], "VGK", OC, sk),
                _rec("02fro", "Adam Lowry", ["C"], "WPG", None, {**sk, "FOW": 11}),
                _rec("03kra", "Kyle Connor", ["LW"], "WPG", RSD, sk),
                _rec("zzz", "Did Not Play", ["D"], "WPG", None, {**sk, "GP": 0})]

    monkeypatch.setattr(srv.FX, "stats_rows", stats_rows)
    out = asyncio.run(srv.get_daily_player_stats("2026-10-02"))
    by = {p["fantrax_id"]: p for p in out["players"]}
    assert set(by) == {"03924", "02fro", "03kra", "g1"}
    assert by["03924"]["owner"] == "OC" and by["03924"]["slot_status"] == "active"
    assert by["03kra"]["owner"] == "RSD" and by["03kra"]["slot_status"] == "bench"
    assert by["02fro"]["owner"] == "FA" and by["02fro"]["slot_status"] is None
    assert by["g1"]["stats"] == {"W": 1, "GA": 2, "SA": 25, "SV": 23, "MIN": 60.0}
    assert "GP" not in by["03924"]["stats"] and by["03924"]["stats"]["TOI"] == 22.1167
    assert out["counts"] == {"skaters": 3, "goalies": 1, "rostered": 3}
    assert "error" in asyncio.run(srv.get_daily_player_stats("10/02/2026"))


def test_get_league_rosters(srv, monkeypatch):
    async def roster(tid, timeframe):
        players = {OC: [{"fantrax_id": "03924", "name": "Jack Eichel", "positions": ["C"],
                         "nhl_team": "VGK", "roster_status": "active", "injury_status": None,
                         "start_status": None}],
                   RSD: [{"fantrax_id": "g1", "name": "Goalie One", "positions": ["G"],
                          "nhl_team": "NJD", "roster_status": "active", "injury_status": None,
                          "start_status": "confirmed"}],
                   GTX: []}[tid]
        return {"team_id": tid, "players": players}

    async def stats_rows(flt, group, timeframe=None, day=None, standard=False, **kw):
        assert flt == "ALL_TAKEN"
        if group == "POS_201":
            return [_rec("g1", "", ["G"], "", RSD, {"GP": 56, "W": 29, "SV": 1396, "GA": 140,
                                                     "SA": 1536, "MIN": 3300.5})]
        return [_rec("03924", "", ["C"], "", OC, {"GP": 81, "G": 29, "Tk": 0, "TOI": None,
                                                  "FOW": 618})]

    monkeypatch.setattr(srv.FX, "roster", roster)
    monkeypatch.setattr(srv.FX, "stats_rows", stats_rows)
    out = asyncio.run(srv.get_league_rosters(["PROJ_SEASON", "YTD"]))
    t = {x["code"]: x for x in out["teams"]}
    eich = t["OC"]["players"][0]["stats"]
    assert eich["PROJ_SEASON"]["Tk"] is None and eich["PROJ_SEASON"]["TOI"] is None
    assert eich["YTD"]["Tk"] == 0 and eich["PROJ_SEASON"]["GP"] == 81
    g = t["RSD"]["players"][0]
    assert g["start_status"] == "confirmed" and set(g["stats"]["YTD"]) == {"GP", "W", "SV", "GA", "SA", "MIN"}
    assert "error" in asyncio.run(srv.get_league_rosters(["PROJ_GAME"]))


def test_goalie_gp_so_far(srv, monkeypatch):
    start = date(2026, 10, 5)
    monkeypatch.setattr(srv, "_et_today", lambda: start + timedelta(days=1))

    async def stats_rows(flt, group, timeframe=None, day=None, standard=False, **kw):
        return [_rec("g1", "", ["G"], "", RSD, {"GP": 1}), _rec("g2", "", ["G"], "", GTX, {"GP": 1}),
                _rec("g3", "", ["G"], "", GTX, {"GP": 1})]

    monkeypatch.setattr(srv.FX, "stats_rows", stats_rows)
    got = asyncio.run(srv._goalie_gp_so_far([OC, RSD, GTX], start, start + timedelta(days=6)))
    # 2 days so far; g3 played but sat on GTX's bench, so it does not count.
    assert got == {OC: 0, RSD: 2, GTX: 2}
