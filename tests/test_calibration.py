"""Tests built from real Fantrax response shapes captured 2026-09-27."""
import asyncio
from datetime import date


from fantrax_mcp.config import current_week, parse_periods
from fantrax_mcp.fantrax_client import _cell_value, flatten_rows

PERIODS = ["1 (Sep 29 - Oct 4)", "2 (Oct 5 - Oct 11)", "14 (Dec 28 - Jan 3)",
           "19 (Feb 1 - Feb 14)", "25 (Mar 22 - Apr 4)"]


def test_parse_periods_handles_year_rollover_and_double_weeks():
    w = parse_periods(PERIODS, 2026)
    assert w[1] == (date(2026, 9, 29), date(2026, 10, 4))
    assert w[14] == (date(2026, 12, 28), date(2027, 1, 3))
    assert (w[19][1] - w[19][0]).days == 13 and (w[25][1] - w[25][0]).days == 13
    assert current_week(w, date(2026, 10, 1)) == 1


def test_cell_values():
    assert _cell_value({"content": "1636:29"}) == 1636.48   # TOI -> minutes
    assert _cell_value({"content": "0.62"}) == 0.62
    assert _cell_value({"content": "655"}) == 655
    assert _cell_value({"content": "FA"}) == "FA"


def test_flatten_free_agent_row():
    header = {"cells": [{"shortName": s} for s in ["Sta", "GP", "FOW", "TOI"]]}
    rows = [{"scorer": {"name": "Alexander Wennberg", "scorerId": "03169", "teamShortName": "SJS",
                        "posShortNames": "C"},
             "cells": [{"content": "FA"}, {"content": "80"}, {"content": "655"}, {"content": "1636:29"}]}]
    (p,) = flatten_rows(header, rows)
    assert p["positions"] == ["C"] and p["stats"]["FOW"] == 655 and p["stats"]["TOI"] == 1636.48
