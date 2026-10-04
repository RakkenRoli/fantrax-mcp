"""Goalie games minimum / maximum per scoring period, read from Fantrax (no I/O here).

Verified via fantrax_raw on 2026-10-04:
  getTeamRosterInfo {teamId: <any team>, scoringPeriod: "13", view: "GAMES_PER_POS"}
  -> scMinMaxData.tableData[]: row "Games Played - Goalies (GP)" with min "1", max "No max"
  -> scMinMaxData.minNote / maxNote: the penalty text
The setting is league-wide, so one team id is enough. The param must be `scoringPeriod`;
`period` is ignored for this view (period 2 returns min "3").

Also here: scrub_owners(), which drops teamHeadingInfo.owners (a GM's real name) from any
Fantrax response before it can reach a tool result.
"""
from __future__ import annotations

import re
from typing import Any

GOALIE_ROW_RE = re.compile(r"goalie", re.I)
_INT_RE = re.compile(r"-?\d+")
_MIN_KEYS = ("min", "minValue", "minimum", "minGames")
_MAX_KEYS = ("max", "maxValue", "maximum", "maxGames")


def limit_value(v: Any) -> int | None:
    """'1' -> 1, 3 -> 3, 'No min' / 'No max' / '' / None -> None."""
    if isinstance(v, dict):
        v = v.get("content", v.get("value"))
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if not s or s.casefold().startswith("no "):
        return None
    m = _INT_RE.search(s)
    return int(m.group()) if m else None


def _row_label(row: Any) -> str:
    """Every string in the row joined, so the goalie row is found whatever key holds it."""
    if isinstance(row, dict):
        return " ".join(_row_label(v) for v in row.values())
    if isinstance(row, list):
        return " ".join(_row_label(v) for v in row)
    return row if isinstance(row, str) else ""


def _row_limits(row: Any) -> tuple[Any, Any]:
    if isinstance(row, dict):
        mn = next((row[k] for k in _MIN_KEYS if k in row), None)
        mx = next((row[k] for k in _MAX_KEYS if k in row), None)
        if mn is not None or mx is not None:
            return mn, mx
        cells = row.get("cells")
    else:
        cells = row
    # Positional fallback: [label, min, max]
    if isinstance(cells, list) and len(cells) >= 3:
        return cells[-2], cells[-1]
    return None, None


def parse_min_max(data: dict) -> dict:
    """getTeamRosterInfo(view=GAMES_PER_POS) -> {"min", "max", "min_note", "max_note"}.
    Raises ValueError when there is no goalie row (shape changed: capture a new fixture)."""
    mm = (data or {}).get("scMinMaxData") or {}
    for row in mm.get("tableData") or []:
        if GOALIE_ROW_RE.search(_row_label(row)):
            mn, mx = _row_limits(row)
            return {"min": limit_value(mn), "max": limit_value(mx),
                    "min_note": (mm.get("minNote") or "").strip() or None,
                    "max_note": (mm.get("maxNote") or "").strip() or None}
    raise ValueError("scMinMaxData has no goalie games row")


def build_rule(per_period: dict[int, dict]) -> dict:
    """{period: parse_min_max(...)} -> the league_info.goalie_min_rule shape.
    Penalty text is league-wide; the first non-empty note wins."""
    keys = sorted(per_period)
    notes = [per_period[k] for k in keys]
    return {
        "by_period": {str(k): per_period[k]["min"] for k in keys},
        "max_by_period": {str(k): per_period[k]["max"] for k in keys},
        "penalty_if_missed": next((n["min_note"] for n in notes if n["min_note"]), None),
        "penalty_if_max": next((n["max_note"] for n in notes if n["max_note"]), None),
        "source": "fantrax",
    }


def min_for(rule: dict | None, period: int, default: int | None) -> int | None:
    """Goalie minimum for one period: Fantrax value if known (None = no minimum),
    else `default` when the period is missing from the rule."""
    by = (rule or {}).get("by_period") or {}
    return by[str(period)] if str(period) in by else default


def scrub_owners(obj: Any) -> Any:
    """Drop `owners` from every teamHeadingInfo, recursively, in place. Returns obj."""
    if isinstance(obj, dict):
        thi = obj.get("teamHeadingInfo")
        if isinstance(thi, dict):
            thi.pop("owners", None)
        for v in obj.values():
            scrub_owners(v)
    elif isinstance(obj, list):
        for v in obj:
            scrub_owners(v)
    return obj
