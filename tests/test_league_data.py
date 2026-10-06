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


# ------------------------------------------- 0.6.0: owner and slot as of the date
UV, KWC, BVB = "mc5s8r57mtr5w0js", "r27c7dt2mtr5w0js", "089wx6w1mtr5w0js"
SK1 = {"GP": 1, "G": 0, "A": 1, "PIM": 0, "SOG": 3, "PPG": 0, "PPA": 0, "Hit": 1,
       "Blk": 0, "Tk": 0, "FOW": 0, "TOI": 16.5}


def _asof_server(monkeypatch, rosters_by_day, stats_by_day, failing=()):
    """rosters_by_day: {date: {team_id: {pid: slot}}}; stats rows carry TODAY's owner."""
    from fantrax_mcp import server

    async def teams():
        return {GTX: "Gazdagréti Taxisok", UV: "Utah Vultures", KWC: "Kistarcsa Wildcocks",
                BVB: "BVB Eishockeyverein", OC: "Onga Capitals"}

    async def roster_on(tid, d, ttl=300):
        if tid in failing:
            raise RuntimeError("Fantrax returned day period 9, asked for 2")
        return rosters_by_day[d].get(tid, {})

    async def stats_rows(flt, group, timeframe=None, day=None, standard=False, **kw):
        return [] if group == "POS_201" else stats_by_day[day]

    monkeypatch.setattr(server.FX, "teams", teams)
    monkeypatch.setattr(server.FX, "roster_on", roster_on)
    monkeypatch.setattr(server.FX, "stats_rows", stats_rows)
    return server


def _week1(monkeypatch, failing=()):
    """Week 1 cases from the spec. Today (Oct 6) Pettersson, Buchnevich, Bjorkstrand and
    Sandin Pellikka are FA; Perfetti is GTX and Skinner BVB but were FA on Oct 2."""
    d29, d02 = date(2026, 9, 29), date(2026, 10, 2)
    rosters = {
        d29: {UV: {"pett": "active"}, GTX: {"buch": "active"}},
        d02: {GTX: {"buch": "active", "bjo": "active"}, KWC: {"asp": "active"},
              UV: {"pett": "bench"}},
    }
    stats = {
        d29: [_rec("pett", "Elias Pettersson", ["C"], "VAN", None, SK1)],
        d02: [_rec("buch", "Pavel Buchnevich", ["LW", "RW"], "STL", None, SK1),
              _rec("bjo", "Oliver Bjorkstrand", ["RW"], "SEA", None, SK1),
              _rec("asp", "Axel Sandin Pellikka", ["D"], "DET", None, SK1),
              _rec("perf", "Cole Perfetti", ["C", "LW"], "WPG", GTX, SK1),
              _rec("skin", "Stuart Skinner", ["G"], "EDM", BVB, SK1),
              _rec("eich", "Jack Eichel", ["C"], "VGK", OC, SK1)],
    }
    return _asof_server(monkeypatch, rosters, stats, failing)


def test_dropped_after_date_keeps_that_dates_team(monkeypatch):
    srv = _week1(monkeypatch)
    by = {p["name"]: p for p in asyncio.run(srv.get_daily_player_stats("2026-09-29"))["players"]}
    assert (by["Elias Pettersson"]["owner"], by["Elias Pettersson"]["slot_status"]) == ("UV", "active")
    out = asyncio.run(srv.get_daily_player_stats("2026-10-02"))
    by = {p["name"]: p for p in out["players"]}
    for name, team in [("Pavel Buchnevich", "GTX"), ("Oliver Bjorkstrand", "GTX"),
                       ("Axel Sandin Pellikka", "KWC")]:
        assert (by[name]["owner"], by[name]["slot_status"]) == (team, "active"), name
    assert out["complete"] is True and out["attribution"] == "as_of_date"


def test_added_after_date_is_fa_on_that_date(monkeypatch):
    srv = _week1(monkeypatch)
    by = {p["name"]: p for p in asyncio.run(srv.get_daily_player_stats("2026-10-02"))["players"]}
    for name in ("Cole Perfetti", "Stuart Skinner"):
        assert (by[name]["owner"], by[name]["slot_status"]) == ("FA", None), name


def test_owned_on_date_never_has_null_slot(monkeypatch):
    srv = _week1(monkeypatch)
    for day in ("2026-09-29", "2026-10-02"):
        out = asyncio.run(srv.get_daily_player_stats(day))
        assert all((p["owner"] == "FA") == (p["slot_status"] is None) for p in out["players"])
        assert out["counts"]["rostered"] == sum(p["owner"] != "FA" for p in out["players"])


def test_stats_untouched_by_attribution(monkeypatch):
    srv = _week1(monkeypatch)
    out = asyncio.run(srv.get_daily_player_stats("2026-10-02"))
    expect = {k: v for k, v in SK1.items() if k != "GP"}
    assert all(p["stats"] == expect for p in out["players"] if "G" not in p["positions"])


def test_failed_team_roster_marks_result_incomplete(monkeypatch):
    srv = _week1(monkeypatch, failing=(KWC,))
    out = asyncio.run(srv.get_daily_player_stats("2026-10-02"))
    by = {p["name"]: p for p in out["players"]}
    assert out["complete"] is False and "KWC" in out["slot_status_errors"]
    assert by["Axel Sandin Pellikka"]["owner"] is None      # unknown, not silently FA
    assert by["Pavel Buchnevich"]["owner"] == "GTX"           # other teams still placed


def test_traded_on_date_goes_to_active_slot(monkeypatch):
    d = date(2026, 10, 3)
    srv = _asof_server(monkeypatch, {d: {GTX: {"x": "bench"}, UV: {"x": "active"}}},
                       {d: [_rec("x", "Traded Guy", ["C"], "TOR", GTX, SK1)]})
    out = asyncio.run(srv.get_daily_player_stats("2026-10-03"))
    assert (out["players"][0]["owner"], out["players"][0]["slot_status"]) == ("UV", "active")
    assert sorted(out["attribution_conflicts"]["x"]) == ["GTX", "UV"]


def test_attribute_day_pure():
    from fantrax_mcp.league_data import attribute_day
    owner, clash = attribute_day({"b": {"p": "ir", "q": "bench"}, "a": {"p": "ir"}})
    assert owner == {"p": ("a", "ir"), "q": ("b", "bench")} and clash == {"p": ["a", "b"]}
