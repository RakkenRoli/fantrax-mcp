"""Parser for getStandings(view=SCHEDULE): the authoritative cumulative H2H table.

One call returns every scoring period ("tableList", one table per period). Each matchup
is two rows sharing a matchupId "<awayId>_<homeId>"; the first row is away (top), the
second home (bottom). Byes appear as "<teamId>_-1" with a "None/Bye" row, or the team is
simply absent (period 1). Playoff placeholders use seed ids like "S#1".

Cell values: `content` is display text ("501:31", "2.02", ".918"); `toolTip` carries
full precision (TOI decimal minutes "501.5164", GAA "2.024937"). We prefer toolTip.
gainColor is Fantrax's own per-category verdict: 1 win, -1 loss, 0 tie.
"""
from __future__ import annotations

import re
from typing import Any

from .categories import team_code

_SEED = re.compile(r"^S#\d+$")
VERDICT = {1: "win", -1: "loss", 0: "tie"}
# Keys Fantrax puts on the W/L/T/Pts columns before the category columns.
_SUMMARY_KEYS = {"win": "W", "loss": "L", "tie": "T", "cp": "Pts"}


def _num(cell: dict) -> float | int | None:
    for raw in (cell.get("toolTip"), cell.get("content")):
        if raw is None:
            continue
        s = str(raw).replace(",", "").strip()
        if s in ("", "-"):
            continue
        if ":" in s:  # "mm:ss" fallback when no toolTip
            m, sec = s.split(":", 1)
            try:
                return round(int(m) + int(sec) / 60, 4)
            except ValueError:
                continue
        try:
            v = float(s)
        except ValueError:
            continue
        return int(v) if v.is_integer() and "." not in s else v
    return None


def _columns(header: dict) -> list[tuple[str, str]]:
    """[(kind, name)] per cell: kind 'summary' (W/L/T/Pts) or 'cat' (scoring category)."""
    cols = []
    for c in header.get("cells", []):
        if c.get("key") in _SUMMARY_KEYS:
            cols.append(("summary", _SUMMARY_KEYS[c["key"]]))
        else:
            cols.append(("cat", c.get("shortName") or c.get("name")))
    return cols


def _side(row: dict, cols: list[tuple[str, str]], team_info: dict) -> dict:
    fixed = (row.get("fixedCells") or [{}])[0]
    tid = fixed.get("teamId")
    info = team_info.get(tid) or {}
    out: dict[str, Any] = {
        "team_id": tid,
        "code": team_code(tid, info.get("shortName")),
        "name": info.get("name") or fixed.get("content"),
    }
    cells = row.get("cells") or []
    summary: dict[str, Any] = {}
    totals: dict[str, Any] = {}
    verdicts: dict[str, Any] = {}
    for (kind, name), cell in zip(cols, cells):
        if kind == "summary":
            summary[name] = _num(cell)
        else:
            totals[name] = _num(cell)
            verdicts[name] = VERDICT.get(cell.get("gainColor"))
    played = any(v is not None for v in totals.values())
    out["wlt"] = [summary.get("W"), summary.get("L"), summary.get("T")] if played else None
    out["category_points"] = summary.get("Pts") if played else None
    out["totals"] = totals if played else None
    out["by_category"] = verdicts if played else None
    return out


def _is_real(tid: str | None) -> bool:
    return bool(tid) and tid != "-1" and not _SEED.match(tid)


def parse_period(table: dict, team_info: dict, period: int) -> dict:
    """One period's table -> pairings (away/home), byes, and a status note."""
    cols = _columns(table.get("header", {}))
    groups: dict[str, list[dict]] = {}
    for row in table.get("rows", []):
        groups.setdefault(row.get("matchupId", ""), []).append(row)

    pairings, byes, placeholders = [], [], 0
    for mid, rows in groups.items():
        ids = [((r.get("fixedCells") or [{}])[0]).get("teamId") for r in rows]
        real = [r for r, t in zip(rows, ids) if _is_real(t)]
        if len(real) == 2:
            away, home = (_side(r, cols, team_info) for r in real)
            pairings.append({"matchup_id": mid, "away": away, "home": home})
        elif len(real) == 1:
            byes.append(_side(real[0], cols, team_info))
        else:
            placeholders += 1   # playoff seeds not decided yet

    caption = table.get("caption", "")
    is_playoffs = caption.lower().startswith("playoffs")
    seen = {s["team_id"] for p in pairings for s in (p["away"], p["home"])} | {b["team_id"] for b in byes}
    if not is_playoffs:   # period 1 omits the bye team entirely instead of a "_-1" row
        for tid, info in team_info.items():
            if tid not in seen:
                byes.append({"team_id": tid, "code": team_code(tid, info.get("shortName")),
                             "name": info.get("name")})

    n_teams = len(team_info)
    note = None
    if is_playoffs:
        note = "Playoff round; pairings are seeds until the regular season ends." if placeholders \
            else "Playoff round."
    elif len(pairings) * 2 + len(byes) != n_teams:
        note = f"Team count mismatch: {len(pairings)} pairings + {len(byes)} byes for {n_teams} teams."
    elif len(byes) != n_teams % 2:
        note = (f"Fantrax schedule has {len(byes)} byes this period (league schedule, "
                f"not a parsing issue).")
    return {
        "period": period,
        "caption": caption,
        "dates": table.get("subCaption", "").strip("()"),
        "is_playoffs": is_playoffs,
        "pairings": pairings,
        "byes": [{"team_id": b["team_id"], "code": b["code"], "name": b["name"]} for b in byes],
        "seed_placeholders": placeholders,
        "note": note,
    }


def parse_schedule(data: dict) -> dict[int, dict]:
    """Whole getStandings(view=SCHEDULE) response -> {period: parsed period}."""
    info = data.get("fantasyTeamInfo") or {}
    out = {}
    for i, table in enumerate(data.get("tableList") or [], 1):
        m = re.search(r"(\d+)", table.get("caption", ""))
        # Playoff captions ("Playoffs - Round 1") have no period number; position is the period.
        period = int(m.group(1)) if m and table.get("caption", "").startswith("Scoring Period") else i
        out[period] = parse_period(table, info, period)
    return out


def find_team(period: dict, team_id: str) -> tuple[dict | None, str | None]:
    """(pairing, side) containing team_id, side 'away'/'home'; (None, None) on a bye."""
    for p in period["pairings"]:
        for side in ("away", "home"):
            if p[side]["team_id"] == team_id:
                return p, side
    return None, None


def slim_raw(data: dict, period: int) -> dict:
    """getStandings response with tableList cut down to one period (raw shape kept)."""
    tables = data.get("tableList") or []
    keep = [t for i, t in enumerate(tables, 1) if i == period]
    return {**{k: v for k, v in data.items() if k not in ("tableList", "matchupIdsPerTeam")},
            "tableList": keep, "_note": f"tableList filtered to period {period} of {len(tables)}"}
