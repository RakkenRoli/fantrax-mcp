"""get_league_rosters: timeframe codes, per-timeframe isolation, diagnosable Fantrax errors.

season_options_2026-10-04.json is the real seasonOrProjections list (trimmed). On 2026-10-04
each of these was accepted live with statusOrTeamFilter=ALL_TAKEN, positionOrGroup=
HOCKEY_SKATING, maxResultsPerPage=500:
  PROJ_SEASON -> PROJECTION_0_31n_SEASON / PROJECTED_SEASON
  YTD         -> SEASON_31n_YEAR_TO_DATE / YEAR_TO_DATE
  LAST_SEASON -> SEASON_31l_YEAR_TO_DATE / YEAR_TO_DATE   (not 31m: that is 2025-26 playoffs)
"""
import asyncio
import json
from pathlib import Path

import pytest

from fantrax_mcp.fantrax_client import FantraxError

OPTS = json.loads((Path(__file__).parent / "fixtures" / "season_options_2026-10-04.json")
                  .read_text(encoding="utf-8"))
EXPECTED = {"PROJ_SEASON": ("PROJECTION_0_31n_SEASON", "PROJECTED_SEASON"),
            "YTD": ("SEASON_31n_YEAR_TO_DATE", "YEAR_TO_DATE"),
            "LAST_SEASON": ("SEASON_31l_YEAR_TO_DATE", "YEAR_TO_DATE")}
SK_HEADER = {"cells": [{"key": "status"}] + [{"scipId": f"2010#{i}#-1"} for i in
             ("2100", "2130", "2090", "2170", "2270", "2210", "2200", "2147", "2092", "2295", "2096", "2300")]}
G_HEADER = {"cells": [{"key": "status"}] + [{"scipId": f"2020#{i}#-1"} for i in
            ("2100", "231b", "2230", "2140", "2280", "2298")]}
OC = "wge2dwlrmtr5w0js"


def _row(pid, pos, vals):
    return {"scorer": {"scorerId": pid, "name": pid, "teamShortName": "BOS", "posShortNames": pos},
            "cells": [{"content": "OC", "teamId": OC}] + [{"content": v} for v in vals]}


@pytest.fixture
def srv(monkeypatch):
    from fantrax_mcp import server

    async def meta(group=None):
        return {"seasonOrProjections": OPTS["seasonOrProjections"]}

    async def teams():
        return {OC: "Onga Capitals"}

    async def roster(tid, timeframe):
        return {"team_id": tid, "players": [
            {"fantrax_id": "sk1", "name": "Skater", "positions": ["C"], "nhl_team": "BOS"},
            {"fantrax_id": "g1", "name": "Goalie", "positions": ["G"], "nhl_team": "BOS"}]}

    monkeypatch.setattr(server.FX, "_meta", meta)
    monkeypatch.setattr(server.FX, "teams", teams)
    monkeypatch.setattr(server.FX, "roster", roster)
    server.FX._cache = type(server.FX._cache)()
    return server


def _fake_fantrax(srv, monkeypatch, fail=None):
    sent = []

    async def call(method, **data):
        sent.append(data)
        tf = next(k for k, v in EXPECTED.items() if v[0] == data["seasonOrProjection"])
        group = data["positionOrGroup"]
        if fail and (tf, group) == fail:
            raise FantraxError(method, data, {"code": "INVALID_REQUEST"})
        if group == "POS_201":
            return {"tableHeader": G_HEADER, "statsTable": [_row("g1", "G", ["56", "29", "1396", "140", "1536", "3300:30"])]}
        toi = "" if tf == "PROJ_SEASON" else "1500:00"
        return {"tableHeader": SK_HEADER,
                "statsTable": [_row("sk1", "C", ["81", "29", "50", "20", "250", "8", "15", "40", "30", "0", "600", toi])]}

    monkeypatch.setattr(srv.FX, "call", call)
    return sent


def test_timeframe_codes_from_recorded_options(srv):
    codes = asyncio.run(srv.FX.season_codes())
    for tf, want in EXPECTED.items():
        assert codes[tf] == want


@pytest.mark.parametrize("tf", list(EXPECTED))
def test_each_timeframe_alone(srv, monkeypatch, tf):
    sent = _fake_fantrax(srv, monkeypatch)
    out = asyncio.run(srv.get_league_rosters([tf]))
    assert "errors" not in out
    assert {(d["statusOrTeamFilter"], d["positionOrGroup"], d["seasonOrProjection"], d["timeframeTypeCode"])
            for d in sent} == {("ALL_TAKEN", g, *EXPECTED[tf]) for g in ("HOCKEY_SKATING", "POS_201")}
    assert all(d.get("scoringCategoryType") == "1" for d in sent if d["positionOrGroup"] == "POS_201")
    players = {p["fantrax_id"]: p for p in out["teams"][0]["players"]}
    sk, g = players["sk1"]["stats"][tf], players["g1"]["stats"][tf]
    assert sk["GP"] == 81 and sk["FOW"] == 600
    assert (sk["Tk"], sk["TOI"]) == ((None, None) if tf == "PROJ_SEASON" else (0, 1500))
    assert g == {"GP": 56, "W": 29, "SV": 1396, "GA": 140, "SA": 1536, "MIN": 3300.5}


def test_one_failing_timeframe_does_not_sink_the_rest(srv, monkeypatch):
    _fake_fantrax(srv, monkeypatch, fail=("PROJ_SEASON", "HOCKEY_SKATING"))
    out = asyncio.run(srv.get_league_rosters(["PROJ_SEASON", "YTD", "LAST_SEASON"]))
    assert out["incomplete"] == ["PROJ_SEASON/skaters"]
    err = out["errors"][0]
    assert err["timeframe"] == "PROJ_SEASON" and err["group"] == "skaters"
    assert "INVALID_REQUEST" in err["error"]
    assert err["request"]["seasonOrProjection"] == "PROJECTION_0_31n_SEASON"
    players = {p["fantrax_id"]: p for p in out["teams"][0]["players"]}
    assert players["sk1"]["stats"]["PROJ_SEASON"] is None          # failed part -> null
    assert players["sk1"]["stats"]["YTD"]["GP"] == 81             # rest intact
    assert players["g1"]["stats"]["PROJ_SEASON"]["W"] == 29       # goalies of same tf intact


def test_invalid_request_backoff_2_5_15_then_reported_with_the_request(srv, monkeypatch):
    import httpx
    bad = {"pageError": {"code": "INVALID_REQUEST"}}
    bodies = [bad, bad, {"responses": [{"data": {"ok": 1}}]},      # call 1: wins on attempt 3
              bad, bad, bad, bad]                                    # call 2: gives up after 4
    posts, sleeps = [], []

    async def post(url, params=None, json=None):
        posts.append(json)
        return httpx.Response(200, json=bodies[len(posts) - 1], request=httpx.Request("POST", url))

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(srv.FX._http, "post", post)
    monkeypatch.setattr("fantrax_mcp.fantrax_client.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(srv.FX, "_min_interval", 0.0)
    monkeypatch.setattr(srv.FX, "_backoff", [2.0, 5.0, 15.0])
    assert asyncio.run(srv.FX._post("getPlayerStats", {"x": "1"})) == {"ok": 1}
    assert sleeps == [2.0, 5.0]
    sleeps.clear()
    with pytest.raises(FantraxError) as ei:
        asyncio.run(srv.FX._post("getPlayerStats", {"statusOrTeamFilter": "ALL_TAKEN"}))
    assert sleeps == [2.0, 5.0, 15.0] and len(posts) == 7
    assert ei.value.code == "INVALID_REQUEST"
    assert '"statusOrTeamFilter": "ALL_TAKEN"' in str(ei.value)
    assert "cookie" not in str(ei.value).lower()


def test_requests_are_paced(srv, monkeypatch):
    import httpx
    sleeps = []
    clock = [100.0]

    async def post(url, params=None, json=None):
        return httpx.Response(200, json={"responses": [{"data": {}}]}, request=httpx.Request("POST", url))

    async def fake_sleep(s):
        sleeps.append(round(s, 3))
        clock[0] += s

    monkeypatch.setattr(srv.FX._http, "post", post)
    monkeypatch.setattr("fantrax_mcp.fantrax_client.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("fantrax_mcp.fantrax_client.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(srv.FX, "_min_interval", 0.75)
    monkeypatch.setattr(srv.FX, "_last_request", 0.0)

    async def three():
        for _ in range(3):
            await srv.FX._post("getPlayerStats", {})
    asyncio.run(three())
    assert sleeps == [0.75, 0.75]          # first request immediate, then spaced


def test_free_agents_entry_filtered_by_gp(srv, monkeypatch):
    sent = []

    async def call(method, **data):
        sent.append(data)
        tf = next(k for k, v in EXPECTED.items() if v[0] == data["seasonOrProjection"])
        if data["statusOrTeamFilter"] == "ALL_TAKEN":
            return {"tableHeader": SK_HEADER if data["positionOrGroup"] != "POS_201" else G_HEADER,
                    "statsTable": []}
        if data["positionOrGroup"] == "POS_201":
            gp = {"PROJ_SEASON": "40", "YTD": "0", "LAST_SEASON": "12"}[tf]
            return {"tableHeader": G_HEADER, "statsTable": [
                {**_row("fag", "G", [gp, "5", "300", "30", "330", "700:00"]),
                 "cells": [{"content": "FA"}] + [{"content": v} for v in [gp, "5", "300", "30", "330", "700:00"]]}]}
        rows = []
        for pid, gp in (("fa_played", {"PROJ_SEASON": "70", "YTD": "2", "LAST_SEASON": "0"}),
                        ("fa_never", {"PROJ_SEASON": "5", "YTD": "0", "LAST_SEASON": "0"})):
            vals = [gp[tf], "1", "1", "0", "5", "0", "0", "3", "2", "1", "0", "30:00"]
            rows.append({"scorer": {"scorerId": pid, "name": pid, "teamShortName": "BOS", "posShortNames": "D"},
                         "cells": [{"content": "FA"}] + [{"content": v} for v in vals]})
        return {"tableHeader": SK_HEADER, "statsTable": rows}

    monkeypatch.setattr(srv.FX, "call", call)
    out = asyncio.run(srv.get_league_rosters(["PROJ_SEASON"], include_free_agents=True))
    fa = next(t for t in out["teams"] if t["code"] == "FA")
    ids = [p["fantrax_id"] for p in fa["players"]]
    assert ids == ["fa_played", "fag"]                     # fa_never has 0 GP in YTD and LAST_SEASON
    p = fa["players"][0]
    assert set(p) == {"fantrax_id", "name", "positions", "nhl_team", "stats"}   # slim FA record
    assert set(p["stats"]) == {"PROJ_SEASON"}
    assert p["stats"]["PROJ_SEASON"]["GP"] == 70 and p["stats"]["PROJ_SEASON"]["TOI"] is None
    assert fa["players"][1]["stats"]["PROJ_SEASON"]["SA"] == 330
    # YTD + LAST_SEASON were pulled for the filter although only PROJ_SEASON was asked for
    fa_tfs = {d["seasonOrProjection"] for d in sent if d["statusOrTeamFilter"] == "ALL_AVAILABLE"}
    assert fa_tfs == {v[0] for v in EXPECTED.values()}
    assert "errors" not in out


def test_free_agent_failure_is_partial(srv, monkeypatch):
    _fake_fantrax(srv, monkeypatch)
    real_call = srv.FX.call

    async def call(method, **data):
        if data["statusOrTeamFilter"] == "ALL_AVAILABLE" and data["seasonOrProjection"].startswith("SEASON_31l"):
            raise FantraxError(method, data, {"code": "INVALID_REQUEST"})
        return await real_call(method, **data)

    monkeypatch.setattr(srv.FX, "call", call)
    out = asyncio.run(srv.get_league_rosters(["YTD"], include_free_agents=True))
    assert out["incomplete"] == ["FA:LAST_SEASON/goalies", "FA:LAST_SEASON/skaters"]
    assert all(e["scope"] == "free_agents" for e in out["errors"])
    assert any(t["code"] == "FA" for t in out["teams"])


def test_failed_timeframe_falls_back_to_last_good_copy(srv, monkeypatch):
    _fake_fantrax(srv, monkeypatch)
    first = asyncio.run(srv.get_league_rosters(["YTD", "LAST_SEASON"]))     # stores good copies
    assert "stale" not in first and "errors" not in first
    srv.FX._cache = type(srv.FX._cache)()                                   # force refetch
    _fake_fantrax(srv, monkeypatch, fail=("LAST_SEASON", "HOCKEY_SKATING"))
    out = asyncio.run(srv.get_league_rosters(["YTD", "LAST_SEASON"]))
    assert "incomplete" not in out
    assert len(out["stale"]) == 1 and out["stale"][0].startswith("LAST_SEASON/skaters@")
    assert out["errors"][0]["served_stale"] == out["stale"][0].split("@")[1]
    sk = next(p for p in out["teams"][0]["players"] if p["fantrax_id"] == "sk1")
    assert sk["stats"]["LAST_SEASON"]["GP"] == 81                           # value, not null


def test_last_good_copy_too_old_is_not_used(srv, monkeypatch):
    _fake_fantrax(srv, monkeypatch)
    asyncio.run(srv.get_league_rosters(["YTD"]))
    key = "stats|ALL_TAKEN|HOCKEY_SKATING|YTD"
    ts, rows = srv.FX.last_good._mem[key]
    srv.FX.last_good._mem[key] = (ts - 25 * 3600, rows)                    # YTD limit is 24 h
    srv.FX._cache = type(srv.FX._cache)()
    _fake_fantrax(srv, monkeypatch, fail=("YTD", "HOCKEY_SKATING"))
    out = asyncio.run(srv.get_league_rosters(["YTD"]))
    assert out["incomplete"] == ["YTD/skaters"] and "stale" not in out


def test_rostered_pool_is_fetched_before_free_agents(srv, monkeypatch):
    sent = _fake_fantrax(srv, monkeypatch)
    asyncio.run(srv.get_league_rosters(["YTD"], include_free_agents=True))
    filters = [d["statusOrTeamFilter"] for d in sent]
    assert filters.index("ALL_AVAILABLE") > max(i for i, f in enumerate(filters) if f == "ALL_TAKEN")


def test_team_list_failure_uses_last_good_list(srv, monkeypatch):
    from fantrax_mcp.fantrax_client import FantraxClient
    srv.FX.last_good.put("teams", {OC: "Onga Capitals"})

    async def cached(ttl, method, **kw):
        raise FantraxError(method, kw, {"code": "INVALID_REQUEST"})
    monkeypatch.setattr(srv.FX, "cached", cached)
    # call the real method (the fixture stubs FX.teams on the instance)
    assert asyncio.run(FantraxClient.teams(srv.FX)) == {OC: "Onga Capitals"}


def test_unrecoverable_fantrax_error_is_a_readable_tool_result(srv, monkeypatch):
    async def teams():
        raise FantraxError("getStandings", {"view": "COMBINED"}, {"code": "INVALID_REQUEST"})
    monkeypatch.setattr(srv.FX, "teams", teams)
    out = asyncio.run(srv.get_league_rosters(["YTD"]))
    assert "INVALID_REQUEST" in out["error"]
    assert out["request"] == {"method": "getStandings", "data": {"view": "COMBINED"}}


def test_fa_require_ytd_gp_drops_last_season_only_players(srv, monkeypatch):
    _fake_fantrax(srv, monkeypatch)
    real = srv.FX.call

    async def call(method, **data):
        if data["statusOrTeamFilter"] != "ALL_AVAILABLE":
            return await real(method, **data)
        tf = next(k for k, v in EXPECTED.items() if v[0] == data["seasonOrProjection"])
        if data["positionOrGroup"] == "POS_201":
            return {"tableHeader": G_HEADER, "statsTable": []}
        rows = []
        for pid, gp in (("ytd_player", {"YTD": "1", "LAST_SEASON": "0", "PROJ_SEASON": "70"}),
                        ("last_only", {"YTD": "0", "LAST_SEASON": "60", "PROJ_SEASON": "50"})):
            vals = [gp[tf], "1", "1", "0", "5", "0", "0", "3", "2", "1", "0", "30:00"]
            rows.append({"scorer": {"scorerId": pid, "name": pid, "teamShortName": "BOS", "posShortNames": "D"},
                         "cells": [{"content": "FA"}] + [{"content": v} for v in vals]})
        return {"tableHeader": SK_HEADER, "statsTable": rows}

    monkeypatch.setattr(srv.FX, "call", call)
    both = asyncio.run(srv.get_league_rosters(["YTD"], include_free_agents=True))
    srv.FX._cache = type(srv.FX._cache)()
    ytd = asyncio.run(srv.get_league_rosters(["YTD"], include_free_agents=True, fa_require_ytd_gp=True))
    ids = lambda out: [p["fantrax_id"] for p in next(t for t in out["teams"] if t["code"] == "FA")["players"]]
    assert ids(both) == ["last_only", "ytd_player"] and ids(ytd) == ["ytd_player"]


def test_wire_format_is_one_compact_text_block():
    from fantrax_mcp import server
    out = {"teams": [{"code": "FA", "players": [{"name": "Žemlička", "stats": {"YTD": None}}]}]}
    r = server._encode(out)
    assert r.structured_content is None and len(r.content) == 1
    text = r.content[0].text
    assert json.loads(text) == out and ": " not in text and "\n" not in text   # compact
    assert "Žemlička" in text                                               # no \u escapes
