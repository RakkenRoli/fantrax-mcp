"""Goalie min/max rule (getTeamRosterInfo view=GAMES_PER_POS) and owner scrubbing.

Row shape mirrors the live response checked via fantrax_raw on 2026-10-04: a
"Games Played - Goalies (GP)" row with min "1" / max "No max" for scoring period 13.
Replace the hand-built rows with a captured fixture once one is committed.
"""
import asyncio

import pytest

from fantrax_mcp.goalie_rule import (build_rule, limit_value, min_for, parse_min_max,
                                     scrub_owners)


def _mm(mn, mx, min_note="Goalie categories count as losses", max_note=None, keyed=True):
    goalie = ({"name": "Games Played - Goalies (GP)", "min": mn, "max": mx} if keyed
              else {"cells": ["Games Played - Goalies (GP)", mn, mx]})
    skater = ({"name": "Games Played - Skaters (GP)", "min": "No min", "max": "No max"}
              if keyed else {"cells": ["Games Played - Skaters (GP)", "No min", "No max"]})
    return {"scMinMaxData": {"tableData": [skater, goalie],
                             "minNote": min_note, "maxNote": max_note}}


@pytest.mark.parametrize("raw,want", [("1", 1), ("3", 3), (2, 2), ("No min", None),
                                      ("No max", None), ("", None), (None, None),
                                      ({"content": "4"}, 4)])
def test_limit_value(raw, want):
    assert limit_value(raw) == want


@pytest.mark.parametrize("keyed", [True, False])
def test_parse_period_13(keyed):
    got = parse_min_max(_mm("1", "No max", keyed=keyed))
    assert got == {"min": 1, "max": None,
                   "min_note": "Goalie categories count as losses", "max_note": None}


def test_parse_no_goalie_row():
    with pytest.raises(ValueError):
        parse_min_max({"scMinMaxData": {"tableData": []}})


def test_build_rule_and_min_for():
    per = {1: parse_min_max(_mm("2", "No max")), 2: parse_min_max(_mm("3", "No max")),
           13: parse_min_max(_mm("1", "No max")), 20: parse_min_max(_mm("No min", "5", None, "Excess games dropped"))}
    rule = build_rule(per)
    assert rule["by_period"] == {"1": 2, "2": 3, "13": 1, "20": None}
    assert rule["max_by_period"] == {"1": None, "2": None, "13": None, "20": 5}
    assert rule["penalty_if_missed"] == "Goalie categories count as losses"
    assert rule["penalty_if_max"] == "Excess games dropped"
    assert rule["source"] == "fantrax"
    assert min_for(rule, 2, 3) == 3
    assert min_for(rule, 20, 3) is None      # explicit "No min"
    assert min_for(rule, 7, 3) == 3          # period not fetched -> default


def test_scrub_owners():
    data = {"teamHeadingInfo": {"name": "Gazdagréti Taxisok", "owners": [{"name": "Real Name"}]},
            "nested": [{"teamHeadingInfo": {"owners": "x", "keep": 1}}]}
    out = scrub_owners(data)
    assert "owners" not in out["teamHeadingInfo"] and out["teamHeadingInfo"]["name"]
    assert out["nested"][0]["teamHeadingInfo"] == {"keep": 1}


def test_client_fetches_every_period_and_scrubs(monkeypatch):
    from fantrax_mcp import server as srv
    calls = []
    mins = {2: "3", 13: "1"}

    async def fake_post(method, data):
        calls.append((method, data))
        return scrub_owners({"teamHeadingInfo": {"owners": ["Real Name"]},
                             **_mm(mins.get(int(data["scoringPeriod"]), "2"), "No max")})
    monkeypatch.setattr(srv.FX, "_post", fake_post)
    monkeypatch.setattr(srv.FX, "_cache", type(srv.FX._cache)())
    monkeypatch.setattr(srv.FX, "_any_team_id", lambda: _const("T1"))
    rule = asyncio.run(srv.FX.goalie_min_rule(25))
    assert len(calls) == 25
    assert all(d["scoringPeriod"] == str(i) and d["view"] == "GAMES_PER_POS" and "period" not in d
               for i, (_, d) in enumerate(calls, 1))
    assert rule["by_period"]["2"] == 3 and rule["by_period"]["13"] == 1 and rule["by_period"]["1"] == 2
    asyncio.run(srv.FX.goalie_min_rule(25))  # served from the weekly copy
    assert len(calls) == 25
    assert "Real Name" not in repr(rule)


async def _const(v):
    return v
