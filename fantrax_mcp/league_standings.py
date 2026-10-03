"""League standings builder for get_league_standings (separate from standings.py,
which holds find_team/slim_raw).

Calibrated against live fxpa getStandings (2026-10-03):
  * tableList[] has two tables; standings = tableType "H2hRotisserie1"
    (the other, "H2hRotisserie2", is the current-period category grid).
  * Row cells are positional; header.cells[i].key names them:
      win, loss, tie, pts, winpc, div, gb, wwOrder, maxClaims*, cpf, cpa
  * fixedCells[0] = rank, fixedCells[1] = {content: name, teamId}.
  * COMBINED has no division column -> one extra call per division tab
    (displayedLists.tabs ids starting with "_"), view=<tab id>.
  * winpc is "-" before any matchup is final; div is "W-L-T".
  * cpf/cpa INCLUDE the in-progress week.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

_NON_DIVISION_TABS = {"ALL", "COMBINED", "SCHEDULE", "SEASON_STATS", "PLAYOFFS"}

Call = Callable[..., Awaitable[dict]]   # e.g. FX.call(method, **kwargs)


def _num(s):
    if s is None:
        return None
    s = str(s).strip()
    if s in ("", "-"):
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def _wlt(s) -> list[int]:
    try:
        parts = [int(x) for x in str(s).split("-")]
        return parts if len(parts) == 3 else [0, 0, 0]
    except ValueError:
        return [0, 0, 0]


def _standings_table(resp: dict) -> dict:
    for t in resp.get("tableList", []):
        if t.get("tableType") == "H2hRotisserie1":
            return t
    raise ValueError("getStandings: no H2hRotisserie1 table - Fantrax layout changed?")


def _parse_rows(table: dict) -> list[dict]:
    keys = [c.get("key") for c in table["header"]["cells"]]
    rows = []
    for r in table.get("rows", []):
        fixed = r["fixedCells"]
        vals = {k: c.get("toolTip", c.get("content")) for k, c in zip(keys, r["cells"])}
        rows.append({
            "rank": int(fixed[0]["content"]),
            "team_id": fixed[1]["teamId"],
            "name": fixed[1]["content"],
            "vals": vals,
        })
    return rows


def _nhl_date() -> str:
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


async def build_standings(call: Call,
                          code_for: Callable[[str, str], str | None] | None = None) -> dict:
    """code_for(team_id, name) -> league code (e.g. categories.team_code).
    Falls back to Fantrax's shortName (several are stale: "NT 13", "NT 9", "WPG")."""
    combined = await call("getStandings", view="COMBINED")
    table = _standings_table(combined)
    team_info = combined.get("fantasyTeamInfo", {})

    division_of: dict[str, tuple[str, int]] = {}
    for tab in combined.get("displayedLists", {}).get("tabs", []):
        if tab.get("id") in _NON_DIVISION_TABS:
            continue
        div_resp = await call("getStandings", view=tab["id"])
        for row in _parse_rows(_standings_table(div_resp)):
            division_of[row["team_id"]] = (tab["name"], row["rank"])

    rule = next(
        (c.get("name") for c in table["header"]["cells"] if c.get("key") == "pts"),
        "2 points per matchup win, 1 per tie",
    )

    rows = []
    for row in _parse_rows(table):
        v, tid = row["vals"], row["team_id"]
        division, division_rank = division_of.get(tid, (None, None))
        rows.append({
            "rank": row["rank"],
            "code": (code_for(tid, team_info.get(tid, {}).get("shortName")) if code_for
                     else team_info.get(tid, {}).get("shortName")),
            "team_id": tid,
            "name": row["name"],
            "division": division,
            "division_rank": division_rank,
            "w": _num(v.get("win")) or 0,
            "l": _num(v.get("loss")) or 0,
            "t": _num(v.get("tie")) or 0,
            "pts": _num(v.get("pts")) or 0,
            "win_pct": _num(v.get("winpc")),
            "div_wlt": _wlt(v.get("div")),
            "gb": _num(v.get("gb")) or 0,
            "cat_pts_for": _num(v.get("cpf")) or 0,
            "cat_pts_against": _num(v.get("cpa")) or 0,
        })

    return {
        "rule": rule,
        "rows": rows,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "nhl_date": _nhl_date(),
    }
