"""Pure parsers for the league-wide data tools (no I/O, unit-tested).

Shapes calibrated against live fxpa responses on 2026-10-03:

getPlayerStats (statsTable rows)
  * cells[0] is the "Sta" column: {"content": "OC", "toolTip": "Onga Capitals",
    "teamId": "wge2..."} for rostered players, {"content": "FA"} (no teamId) otherwise.
  * Category columns carry scipId in tableHeader.cells; map by scipId, never by position.
    GP is "2010#2100#-1" (skaters) / "2020#2100#-1" (goalies).
  * BY_DATE: seasonOrProjection=SEASON_<id>_BY_DATE, timeframeTypeCode=BY_DATE,
    startDate=endDate=YYYY-MM-DD gives a single NHL date (displayedStartDate echoes it).
    statusOrTeamFilter=ALL returns every player, including ~7,000 who did not play.
  * TOI is "mm:ss" in `content`; projections leave Tk = 0 and TOI = "" (not projected).

getTeamRosterInfo
  * displayedLists.periodList is DAILY: "5 (Sat Oct 3)". The day's lineup is selected with
    period=<that index>; displayedSelections.displayedPeriod echoes the day shown.
  * rows[].statusId: "1" active slot, "2" reserve (bench), "3" injured reserve.
  * Goalie start notes are scorer.icons tooltips: "Playing in upcoming/current game",
    "Expected to play in upcoming/current game".
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from .categories import GOALIE_COMPONENT_SCIP, GOALIE_SCIP, SKATER_SCIP

SKATER_GP = "2010#2100#-1"
GOALIE_GP = "2020#2100#-1"
SKATER_RAW: dict[str, str] = {SKATER_GP: "GP", **SKATER_SCIP}
# Raw goalie components only; GAA and SV% are rates and are left out on purpose.
GOALIE_RAW: dict[str, str] = {
    GOALIE_GP: "GP",
    "2020#231b#-1": "W",
    "2020#2140#-1": "GA",
    "2020#2280#-1": "SA",
    "2020#2230#-1": "SV",
    "2020#2298#-1": "MIN",
}
assert set(GOALIE_RAW) <= {GOALIE_GP, *GOALIE_SCIP, *GOALIE_COMPONENT_SCIP}

DAY_STATUS = {"1": "active", "2": "bench", "3": "ir"}
NOT_PROJECTED = ("Tk", "TOI")          # Fantrax has no projection for these
_NEWS = re.compile(r"^[A-Z][a-z]{2} \d{1,2}, \d{1,2}:\d{2} [AP]M:")  # "Oct 2, 5:18 AM: ..."
_DAY = re.compile(r"^(\d+) \(\w{3} (\w{3}) (\d{1,2})\)$")
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


# ---------------------------------------------------------------- cell values
def raw_value(cell: Any) -> int | float | None:
    """Raw number from a stats cell. '' / '-' -> None, 'mm:ss' -> decimal minutes (4 dp)."""
    if isinstance(cell, dict):
        cell = cell.get("content", cell.get("value"))
    if cell is None:
        return None
    if isinstance(cell, (int, float)):
        return cell
    s = str(cell).replace(",", "").strip()
    if s in ("", "-"):
        return None
    if ":" in s:
        m, sec = s.split(":", 1)
        try:
            return round(int(m) + int(sec) / 60, 4)
        except ValueError:
            return None
    try:
        v = float(s)
    except ValueError:
        return None
    return int(v) if v.is_integer() and "." not in s else v


def scip_index(header: dict, wanted: dict[str, str]) -> dict[int, str]:
    """{cell position: our name} for the header cells whose scipId we want."""
    return {i: wanted[c["scipId"]] for i, c in enumerate(header.get("cells", []))
            if c.get("scipId") in wanted}


def row_stats(row: dict, idx: dict[int, str]) -> dict[str, Any]:
    cells = row.get("cells") or []
    return {name: raw_value(cells[i]) if i < len(cells) else None for i, name in idx.items()}


def owner_team_id(row: dict) -> str | None:
    """Fantasy team id from the Sta cell, None for free agents / waivers."""
    cells = row.get("cells") or []
    return (cells[0] or {}).get("teamId") if cells and isinstance(cells[0], dict) else None


# ---------------------------------------------------------------- notes
def start_status(notes: list[str] | None) -> str | None:
    """Goalie start status from Fantrax's icon tooltips."""
    for n in notes or []:
        low = n.casefold()
        if low.startswith("playing in upcoming/current game"):
            return "confirmed"
        if low.startswith("expected to play in upcoming/current game"):
            return "expected"
    return None


def injury_status(notes: list[str] | None) -> str | None:
    """First note that is neither a dated news blurb nor a goalie start note
    (e.g. "Day-to-Day", "Contract dispute - Out Indefinitely")."""
    for n in notes or []:
        if not n or _NEWS.match(n) or start_status([n]):
            continue
        return n
    return None


# ---------------------------------------------------------------- daily periods
def parse_day_periods(period_list: list[str], season_year: int) -> dict[date, int]:
    """getTeamRosterInfo periodList ("5 (Sat Oct 3)") -> {date: period index}.
    Months Jul-Dec belong to season_year, Jan-Jun to season_year + 1."""
    out: dict[date, int] = {}
    for item in period_list or []:
        m = _DAY.match(item.strip())
        if not m:
            continue
        month = _MONTHS.get(m.group(2))
        if not month:
            continue
        year = season_year if month >= 7 else season_year + 1
        out[date(year, month, int(m.group(3)))] = int(m.group(1))
    return out


def roster_day_status(resp: dict, expect_period: int | None = None) -> dict[str, str]:
    """{fantrax_id: active|bench|ir} from one getTeamRosterInfo(period=N) response.
    Raises if Fantrax shows a different day than requested (param ignored or renamed)."""
    if expect_period is not None:
        shown = (resp.get("displayedSelections") or {}).get("displayedPeriod")
        if shown is not None and int(shown) != int(expect_period):
            raise RuntimeError(f"Fantrax returned day period {shown}, asked for {expect_period}")
    out: dict[str, str] = {}
    for table in resp.get("tables") or []:
        for row in table.get("rows") or []:
            sc = row.get("scorer")
            if sc and sc.get("scorerId"):
                out[sc["scorerId"]] = DAY_STATUS.get(str(row.get("statusId")), str(row.get("statusId")))
    return out


_SLOT_RANK = {"active": 0, "bench": 1, "ir": 2}


def attribute_day(day_rosters: dict[str, dict[str, str]]
                  ) -> tuple[dict[str, tuple[str, str]], dict[str, list[str]]]:
    """Invert every team's roster for ONE date into {fantrax_id: (team_id, slot)}.

    day_rosters = {team_id: roster_day_status(...)} for that date. Ownership comes only from
    these date rosters, never from the stats row's Sta cell (that is today's owner).
    A player on two rosters that day (trade on the date) goes to the team where his slot
    ranks highest (active > bench > ir, then team id for a stable result); the clash is
    reported as {fantrax_id: [team ids]}."""
    seen: dict[str, list[tuple[str, str]]] = {}
    for tid in sorted(day_rosters):
        for pid, slot in (day_rosters[tid] or {}).items():
            seen.setdefault(pid, []).append((tid, slot))
    owner: dict[str, tuple[str, str]] = {}
    conflicts: dict[str, list[str]] = {}
    for pid, hits in seen.items():
        hits.sort(key=lambda h: (_SLOT_RANK.get(h[1], 9), h[0]))
        owner[pid] = hits[0]
        if len(hits) > 1:
            conflicts[pid] = [t for t, _ in hits]
    return owner, conflicts


def blank_projection_gaps(stats: dict, timeframe: str) -> dict:
    """Projections carry Tk = 0 and TOI = '' for everyone: report both as None."""
    if timeframe.startswith("PROJ"):
        return {k: (None if k in NOT_PROJECTED else v) for k, v in stats.items()}
    return stats
