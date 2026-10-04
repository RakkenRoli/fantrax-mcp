"""Fantrax + NHL schedule MCP server (read-only, streamable HTTP)."""
from __future__ import annotations

import asyncio
import functools
import hmac
import json
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent
from mcp.server.transport_security import TransportSecuritySettings

from .config import (GOALIE_CATS, GOALIE_MIN_GAMES, LIGHT_NIGHT_MAX_GAMES, LINEUP_SLOTS,
                     IR_SLOTS, ROSTER_SIZE, SKATER_CATS, Settings, current_week, nhl_abbrev,
                     parse_periods, week_ranges)
from .categories import TEAM_CODES, check_against_live, label, label_keys, team_code
from .fantrax_client import READ_METHODS, FantraxClient, FantraxError, NotLoggedIn
from .lineup import LineupPlayer, plan_week, simulate_week, starts_by_position
from .nhl_client import NHLClient
from .standings import find_team, slim_raw
from .league_standings import build_standings
from .league_data import blank_projection_gaps
S = Settings.load()
WEEKS = week_ranges(S.season_first_day, S.season_last_day, S.n_weeks)
FX = FantraxClient(S)
NHL = NHLClient()

INSTRUCTIONS = f"""
Read-only access to the user's Fantrax NHL league (their team: {S.team_name or S.team_id}) plus the NHL schedule.
League: H2H categories, {ROSTER_SIZE}-man roster cap plus {IR_SLOTS} IR slots, {S.n_weeks} weeks ending {S.season_last_day}.
Skater cats: {', '.join(SKATER_CATS)} (PPG and PPA are separate). Goalie cats: {', '.join(GOALIE_CATS)}; no shutouts.
Daily active lineup max {LINEUP_SLOTS}. Not every rostered player starts every day.
Goalie categories count only with >= {GOALIE_MIN_GAMES} goalie games in the week.
For add/drop questions, value = category impact x usable starts. Use lineup_capacity and
evaluate_add_drop, not raw team game counts. No write actions exist; the user makes moves in Fantrax.
""".strip()

mcp = MCPServer("fantrax", instructions=INSTRUCTIONS, version="0.4.2")
ET = ZoneInfo("America/New_York")


def fetch_stamp() -> dict:
    """When the data was fetched. nhl_date is the US-Eastern calendar date, which is how the
    NHL and Fantrax label game days; late games can finish after midnight ET."""
    now = datetime.now(timezone.utc)
    return {"fetched_at": now.isoformat(timespec="seconds"),
            "nhl_date": now.astimezone(ET).date().isoformat()}


def stamped(fn):
    """Add fetched_at / nhl_date to every tool response."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            out = await fn(*args, **kwargs)
        except ValueError as e:
            # Bad input (unknown team, week out of range...). Return the message instead of
            # letting the MCP layer collapse it into a bare "Error executing tool".
            out = {"error": str(e)}
        except FantraxError as e:
            # Fantrax rejected a request the tool could not do without (after retries).
            out = {"error": str(e), "request": {"method": e.method, "data": e.data}}
        except NotLoggedIn as e:
            out = {"error": str(e), "logged_in": False}
        if isinstance(out, dict):
            return {**out, **fetch_stamp()}
        return out
    return wrapper


# How results go on the wire. "text" (default): ONE compact JSON text block. "structured":
# only structuredContent. "both": the SDK default (structured + an indented JSON copy),
# which more than doubles the size of large results.
RESULT_FORMAT = os.environ.get("MCP_RESULT_FORMAT", "text").strip().lower()


def _encode(out: Any) -> CallToolResult:
    # {"error": ...} stays a normal result (isError false), as documented since 0.2.1.
    if RESULT_FORMAT == "structured" and isinstance(out, dict):
        return CallToolResult(content=[], structured_content=out)
    text = json.dumps(out, ensure_ascii=False, separators=(",", ":"), default=str)
    return CallToolResult(content=[TextContent(type="text", text=text)])


def tool():
    """Register an MCP tool: fetch timestamp on the result, readable errors, compact output.
    The module-level name stays the plain dict-returning function (used by tests)."""
    def deco(fn):
        plain = stamped(fn)
        if RESULT_FORMAT == "both":
            mcp.tool()(plain)
            return plain

        @functools.wraps(plain)
        async def wire(*args, **kwargs):
            return _encode(await plain(*args, **kwargs))
        wire.__annotations__ = {**plain.__annotations__, "return": CallToolResult}
        mcp.tool(structured_output=False)(wire)
        return plain
    return deco


# ---------------------------------------------------------------- helpers
async def _weeks() -> dict[int, tuple[date, date]]:
    """Fantrax's own period calendar (has double weeks); computed fallback if unavailable."""
    try:
        parsed = parse_periods(await FX.periods(), S.season_first_day.year)
        if parsed:
            return parsed
    except Exception:  # noqa: BLE001 — fall back rather than fail every tool
        pass
    return WEEKS


async def _week(week: int | None) -> tuple[int, date, date]:
    weeks = await _weeks()
    w = week or current_week(weeks)
    if w not in weeks:
        raise ValueError(f"week must be 1..{max(weeks)}")
    return (w, *weeks[w])


def roster_counts(players: list[dict]) -> dict:
    """Roster cap excludes IR: IR players fill the separate IR slots instead."""
    ir = sum(1 for p in players if p.get("roster_status") == "injured_reserve")
    rostered = len(players) - ir
    return {"roster_count": rostered, "open_roster_spots": max(0, ROSTER_SIZE - rostered),
            "ir_count": ir, "open_ir_spots": max(0, IR_SLOTS - ir),
            "over_cap": rostered > ROSTER_SIZE or ir > IR_SLOTS}


async def _resolve(team: str) -> tuple[str, str]:
    """Team lookup by 'me', code (GTX, RSD, TN...), id or name part — see FX.resolve_team."""
    return await FX.resolve_team(team)


def _to_lineup(players: list[dict]) -> list[LineupPlayer]:
    return [
        LineupPlayer(
            name=p["name"], positions=set(p["positions"]), nhl_team=p["nhl_team"],
            available=p.get("roster_status") != "injured_reserve",
            meta={"fantrax_id": p.get("fantrax_id")},
        )
        for p in players
    ]


async def _teams_by_day(start: date, end: date) -> dict[date, set[str]]:
    games = await NHL.games_by_day(start, end)
    return {d: NHL.teams_playing(g) for d, g in games.items()}


def _find(players: list[dict], name: str) -> dict | None:
    low = name.casefold()
    exact = [p for p in players if (p["name"] or "").casefold() == low]
    part = [p for p in players if low in (p["name"] or "").casefold()]
    return (exact or part or [None])[0]


def _num(v: Any) -> float:
    return float(v) if isinstance(v, (int, float)) else 0.0


def _per_game(stats: dict) -> dict:
    gp = _num(stats.get("GP"))
    if gp <= 0:
        return stats
    skip = {"GP", "Rk", "Sta", "Opp", "Score", "Ros", "+/-", "GAA", "SV%"}
    return {k: (round(v / gp, 3) if isinstance(v, (int, float)) and k not in skip else v)
            for k, v in stats.items()}


# ---------------------------------------------------------------- raw data tools
@tool()
async def league_info() -> dict:
    """League teams (id -> name), which team is mine, rules, and the fantasy week calendar
    (from Fantrax; includes double weeks)."""
    tid, name = await FX.resolve_team("me")
    weeks = await _weeks()
    return {
        "my_team": {"id": tid, "code": team_code(tid), "name": name},
        "teams": await FX.teams(),
        "team_codes": {tid: team_code(tid) for tid in await FX.teams()},
        "lineup_slots": LINEUP_SLOTS,
        "roster_size": ROSTER_SIZE,
        "ir_slots": IR_SLOTS,
        "skater_categories": SKATER_CATS,
        "goalie_categories": GOALIE_CATS,
        "goalie_min_games": GOALIE_MIN_GAMES,
        "goalie_min_rule": {
            "min_games": GOALIE_MIN_GAMES,
            "penalty_if_missed": os.environ.get("GOALIE_MIN_PENALTY") or None,
            "source": "GOALIE_MIN_PENALTY setting" if os.environ.get("GOALIE_MIN_PENALTY") else
                      "not exposed by Fantrax's read API; set GOALIE_MIN_PENALTY from the league Rules page",
        },
        "current_week": current_week(weeks),
        "weeks": {k: [s.isoformat(), e.isoformat()] for k, (s, e) in weeks.items()},
        "timeframes": list((await FX.season_codes()).keys()),
    }


@tool()
async def get_roster(team: str = "me", timeframe: str = "PROJ_SEASON") -> dict:
    """Roster for a team ('me', a team id, or part of a team name): positions, NHL team,
    roster status (active/reserve/IR), injury notes, and category stats for `timeframe`
    (PROJ_SEASON, PROJ_GAME, YTD, LAST_SEASON). TOI is in minutes."""
    ros = await FX.roster(team, timeframe)
    ros.update(roster_counts(ros["players"]))
    return ros


@tool()
async def get_free_agents(position: str = "SKATERS", timeframe: str = "PROJ_SEASON",
                          sort_by: str | None = None, per_game: bool = False,
                          limit: int = 25, pool: int = 150,
                          include_inactive: bool = False) -> dict:
    """Available players with category stats.
    position: SKATERS, C, LW, RW, D, or G. timeframe: PROJ_SEASON, PROJ_GAME, YTD, LAST_SEASON.
    sort_by: a category short name (e.g. FOW, Hit, Blk, PIM, SV) — sorted locally over the top
    `pool` players by Fantrax score; default keeps Fantrax's score order.
    per_game divides counting stats by GP. include_inactive adds minors/unsigned players."""
    rows = await FX.free_agents(position, timeframe, min(pool, 300), 1, include_inactive)
    if per_game:
        rows = [{**r, "stats": _per_game(r["stats"])} for r in rows]
    if sort_by:
        rev = sort_by != "GAA"
        rows.sort(key=lambda r: _num(r["stats"].get(sort_by)), reverse=rev)
    return {"position": position, "timeframe": timeframe, "players": rows[:limit]}


def _side_out(side: dict) -> dict:
    return {"id": side["team_id"], "code": side["code"], "name": side["name"],
            "wlt": side["wlt"], "category_points": side["category_points"],
            "totals": side["totals"], "by_category": side["by_category"]}


async def _live_projection(w: int, tid: str) -> dict:
    """Fantrax's projected W-L-T and per-category projection for one team (live scoring).
    Live scoring is NOT used for totals: its statsMap2 only covers a slice of the period."""
    try:
        live = await FX.live_scoring(w)
    except Exception as e:  # noqa: BLE001 — projections are optional
        return {"projection_error": f"{type(e).__name__}: {e}"}
    per = ((live.get("statsPerTeam") or {}).get("allTeamsStats")) or {}
    mine = (per.get(tid) or {}).get("ACTIVE") or {}
    key = next(iter(mine.get("wltPerMchupProjected") or mine.get("wltPerMchup") or {}), None)
    if not key or "-1" in key.split("|"):
        return {}
    lab = {1.0: "win", 0.0: "loss", 0.5: "tie"}
    proj = {label(k): lab.get(v, v)
            for k, v in (mine.get("totPtsPerMchupProjected", {}).get(key) or {}).items() if "#" in k}
    return {"projected_wlt": (mine.get("wltPerMchupProjected") or {}).get(key),
            "projected_by_category": proj}


PROJECTION_NOTE = ("Fantrax has no Tk or TOI projection: projected_by_category always shows "
                   "them as 'tie'. Treat those two as unknown, not tied.")


@tool()
async def get_matchup(week: int | None = None, team: str = "me") -> dict:
    """H2H matchup for a week (default current) for `team` ('me', id, code, or name part):
    opponent, live W-L-T, cumulative category totals for both sides and per-category
    win/loss/tie, all from Fantrax's standings table (the numbers Fantrax scores on), plus
    Fantrax's projected result. TOI is decimal minutes; GAA and SV% are full precision."""
    w, start, end = await _week(week)
    tid, name = await _resolve(team)
    sched = await FX.schedule()
    period = sched.get(w)
    base = {"week": w, "dates": [start.isoformat(), end.isoformat()],
            "team": {"id": tid, "code": team_code(tid), "name": name}}
    if not period:
        return {**base, "note": f"No standings table for period {w}."}
    pairing, side = find_team(period, tid)
    if not pairing:
        if any(b["team_id"] == tid for b in period["byes"]):
            return {**base, "bye": True, "note": period["note"] or "Bye week."}
        return {**base, "note": period["note"] or "Team not in this period's schedule (playoffs?)."}
    other = "home" if side == "away" else "away"
    me, opp = pairing[side], pairing[other]
    out = {
        **base,
        "home_or_away": side,
        "opponent": {"id": opp["team_id"], "code": opp["code"], "name": opp["name"]},
        "current_wlt": me["wlt"],
        "category_points": {"team": me["category_points"], "opponent": opp["category_points"]},
        "current_by_category": me["by_category"],
        "totals": {"team": me["totals"], "opponent": opp["totals"]},
        "source": "getStandings view=SCHEDULE (cumulative for the whole period)",
    }
    proj = await _live_projection(w, tid)
    if proj:
        out.update(proj)
        out["projection_note"] = PROJECTION_NOTE
    if me["wlt"] is None:
        out["note"] = "No games scored in this period yet."
    return out


@tool()
async def get_league_matchups(week: int | None = None) -> dict:
    """Every H2H pairing for a week (default current): away/home team codes, live W-L-T,
    category points, cumulative totals and per-category verdicts for both sides, plus the
    bye team(s). Parsed from Fantrax's standings table, so the totals are the ones Fantrax
    scores on. A note explains any period without 6 pairings + 1 bye."""
    w, start, end = await _week(week)
    period = (await FX.schedule()).get(w)
    if not period:
        return {"week": w, "note": f"No standings table for period {w}."}
    return {
        "week": w,
        "dates": [start.isoformat(), end.isoformat()],
        "caption": period["caption"],
        "is_playoffs": period["is_playoffs"],
        "pairings": [{"matchup": f"{p['away']['code']}-{p['home']['code']}",
                      "away": _side_out(p["away"]), "home": _side_out(p["home"])}
                     for p in period["pairings"]],
        "byes": period["byes"],
        "seed_placeholders": period["seed_placeholders"],
        "note": period["note"],
        "units": {"TOI": "decimal minutes", "GAA": "goals against per 60", "SV%": "fraction 0-1"},
    }


@tool()
async def get_league_schedule() -> dict:
    """All periods: pairings as team codes, byes, and notes (double weeks, 3-bye weeks,
    playoff seeds). Use for schedule checks; use get_league_matchups for one week's data."""
    sched = await FX.schedule(ttl=3600)
    return {"periods": {
        k: {"caption": v["caption"], "dates": v["dates"],
            "pairings": [f"{p['away']['code']}-{p['home']['code']}" for p in v["pairings"]],
            "byes": [b["code"] for b in v["byes"]],
            "seed_placeholders": v["seed_placeholders"], "note": v["note"]}
        for k, v in sched.items()}}


@tool()
async def get_nhl_schedule(start: str, end: str) -> dict:
    """NHL regular-season games between two ISO dates (inclusive): per-night game counts
    (with light-night flag) and per-team game totals."""
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    if (e - s).days > 62:
        raise ValueError("Range too large (max ~2 months)")
    games = await NHL.games_by_day(s, e)
    out = NHL.summarize(games, LIGHT_NIGHT_MAX_GAMES)
    out["games"] = {d.isoformat(): [f"{a}@{h}" for a, h in g] for d, g in games.items()}
    return out


# ---------------------------------------------------------------- computed tools
@tool()
async def get_fantasy_week(week: int | None = None) -> dict:
    """Fantasy week dates plus games per NHL team and games on light nights
    (<= LIGHT_NIGHT_MAX_GAMES NHL games, when bench overflow is rare)."""
    w, s, e = await _week(week)
    out = NHL.summarize(await NHL.games_by_day(s, e), LIGHT_NIGHT_MAX_GAMES)
    return {"week": w, "start": s.isoformat(), "end": e.isoformat(), **out}


@tool()
async def lineup_capacity(week: int | None = None, team: str = "me",
                          from_date: str | None = None) -> dict:
    """Simulate the daily lineup for a week: usable starts, games wasted on the bench,
    and open slots by position per day. from_date limits the sim to the rest of the week."""
    w, s, e = await _week(week)
    if from_date:
        s = max(s, date.fromisoformat(from_date))
    ros = await FX.roster(team, None)
    lineup = _to_lineup(ros["players"])
    sim = simulate_week(lineup, await _teams_by_day(s, e), LINEUP_SLOTS)
    return {"week": w, "range": [s.isoformat(), e.isoformat()], "team": ros["team_name"], **sim}


@tool()
async def evaluate_add_drop(add: str, drop: str, week: int | None = None,
                            from_date: str | None = None, timeframe: str = "PROJ_SEASON",
                            add_positions: list[str] | None = None,
                            add_team: str | None = None) -> dict:
    """Net usable starts from swapping `drop` (on my roster) for `add` (a free agent) this week,
    plus both players' category stats in the same timeframe. If the FA lookup fails, pass
    add_positions (e.g. ["C","LW"]) and add_team (NHL abbrev)."""
    w, s, e = await _week(week)
    if from_date:
        s = max(s, date.fromisoformat(from_date))
    ros = (await FX.roster("me", timeframe))["players"]
    drop_p = _find(ros, drop)
    if not drop_p:
        raise ValueError(f"{drop!r} not on roster")
    add_p = None
    for pos in ("SKATERS", "G"):
        add_p = _find(await FX.free_agents(pos, timeframe, 300, 1, True), add)
        if add_p:
            break
    if add_positions and add_team:
        add_p = {**(add_p or {"name": add, "stats": None}),
                 "positions": add_positions, "nhl_team": nhl_abbrev(add_team)}
    if not add_p:
        raise ValueError(f"{add!r} not found among available players; pass add_positions/add_team")
    tbd = await _teams_by_day(s, e)
    before_roster = _to_lineup(ros)
    after_roster = _to_lineup([p for p in ros if p is not drop_p] + [add_p])
    before = simulate_week(before_roster, tbd, LINEUP_SLOTS)
    after = simulate_week(after_roster, tbd, LINEUP_SLOTS)
    pick = lambda sim, n: next((p for p in sim["players"] if p["name"] == n), {})
    return {
        "week": w, "range": [s.isoformat(), e.isoformat()], "timeframe": timeframe,
        "add": {**pick(after, add_p["name"]), "stats": add_p.get("stats")},
        "drop": {**pick(before, drop_p["name"]), "stats": drop_p.get("stats")},
        "total_starts_before": before["total_starts"],
        "total_starts_after": after["total_starts"],
        "net_starts": after["total_starts"] - before["total_starts"],
        "starts_by_position_before": dict(starts_by_position(before, before_roster)),
        "starts_by_position_after": dict(starts_by_position(after, after_roster)),
        "note": "Net starts only; weigh against the category profiles in the stats fields.",
    }


@tool()
async def goalie_check(week: int | None = None, from_date: str | None = None,
                       start_share: dict[str, float] | None = None,
                       default_share: float = 0.6, team: str = "me") -> dict:
    """Projected goalie starts vs the weekly minimum for `team` ('me', id, code, or name).
    start_share maps goalie name -> expected share of team starts (e.g. {"Vasilevskiy": 0.75});
    others use default_share."""
    w, s, e = await _week(week)
    if from_date:
        s = max(s, date.fromisoformat(from_date))
    tid, tname = await _resolve(team)
    ros = (await FX.roster(tid, None))["players"]
    tbd = await _teams_by_day(s, e)
    share = {k.casefold(): v for k, v in (start_share or {}).items()}
    goalies = []
    for p in ros:
        if "G" not in p["positions"] or p.get("roster_status") == "injured_reserve":
            continue
        games = sum(1 for teams in tbd.values() if p["nhl_team"] in teams)
        sh = next((v for k, v in share.items() if k in p["name"].casefold()), default_share)
        goalies.append({"name": p["name"], "nhl_team": p["nhl_team"], "team_games": games,
                        "start_share": sh, "est_starts": round(games * sh, 1)})
    est = round(sum(g["est_starts"] for g in goalies), 1)
    return {
        "week": w, "range": [s.isoformat(), e.isoformat()],
        "team": {"id": tid, "code": team_code(tid), "name": tname},
        "goalies": goalies, "est_total_starts": est, "minimum": GOALIE_MIN_GAMES,
        "at_risk": est < GOALIE_MIN_GAMES + 0.5,
        "note": "Team games are a ceiling; confirm starters closer to game day.",
    }


def recompute_goalie_rates(ga: float, sa: float, minutes: float) -> dict:
    """GAA = GA * 60 / MIN, SV% = (SA - GA) / SA. Sum GA/SA/MIN across games or goalies
    first, then call this: averaging per-game GAA or SV% gives the wrong answer."""
    return {"GAA": round(ga * 60 / minutes, 6) if minutes else None,
            "SV%": round((sa - ga) / sa, 6) if sa else None}


@tool()
async def get_goalie_stats(team: str | None = None, timeframe: str = "YTD",
                           limit: int = 50) -> dict:
    """Goalie W/GAA/SV/SV% plus the raw components GA, SA (shots against) and MIN (decimal
    minutes) for `timeframe` (YTD, LAST_SEASON, PROJ_SEASON...). team: 'me', id, code, or
    name; omit for all goalies league-wide. Includes GAA/SV% recomputed from the components,
    and the pooled rates for the whole list."""
    if team:
        tid, tname = await _resolve(team)
        flt = f"FANTASY_TEAM_{tid}"
    else:
        tid, tname, flt = None, None, "ALL"
    rows = await FX.goalie_components(flt, timeframe, min(limit, 300))
    tot = {"GA": 0.0, "SA": 0.0, "MIN": 0.0, "SV": 0.0, "W": 0.0}
    for r in rows:
        st = r["stats"]
        r["recomputed"] = recompute_goalie_rates(_num(st.get("GA")), _num(st.get("SA")),
                                                 _num(st.get("MIN")))
        for k in tot:
            tot[k] += _num(st.get(k))
    return {
        "team": {"id": tid, "code": team_code(tid), "name": tname} if tid else None,
        "timeframe": timeframe,
        "goalies": rows,
        "pooled": {**{k: round(v, 2) for k, v in tot.items()},
                   **recompute_goalie_rates(tot["GA"], tot["SA"], tot["MIN"])},
        "note": "Rostered-goalie totals. Fantrax weekly team GAA/SV% count only goalies in an "
                "active slot; use get_league_matchups for the scored weekly values.",
    }


@tool()
async def league_lineup_capacity(week: int | None = None, from_date: str | None = None) -> dict:
    """Compact lineup simulation for every team in one call: usable starts, games wasted on
    the bench, and goalie team-games (ceiling on goalie starts) vs the weekly minimum."""
    w, s, e = await _week(week)
    week_start = s
    if from_date:
        s = max(s, date.fromisoformat(from_date))
    tbd = await _teams_by_day(s, e)
    teams = await FX.teams()
    try:
        so_far = await _goalie_gp_so_far(list(teams), week_start, e)
        so_far_err = None
    except Exception as ex:  # noqa: BLE001 — optional enrichment
        so_far, so_far_err = {}, f"{type(ex).__name__}: {ex}"
    rows = []
    for tid, name in teams.items():
        try:
            ros = (await FX.roster(tid, None))["players"]
        except Exception as ex:  # noqa: BLE001 — one bad roster must not sink the table
            rows.append({"code": team_code(tid), "name": name, "error": str(ex)})
            continue
        lineup = _to_lineup(ros)
        sim = simulate_week(lineup, tbd, LINEUP_SLOTS)
        by_pos = starts_by_position(sim, lineup)
        g_games = sum(p["team_games"] for p in sim["players"] if "G" in p["positions"])
        played = so_far.get(tid)
        rows.append({"code": team_code(tid), "name": name,
                     "starts": sim["total_starts"], "wasted": sim["total_wasted_games"],
                     "goalie_starts": by_pos.get("G", 0), "goalie_team_games": g_games,
                     "goalie_min_met": by_pos.get("G", 0) >= GOALIE_MIN_GAMES,
                     "goalie_gp_so_far": played,
                     "goalie_gp_needed": None if played is None else max(0, GOALIE_MIN_GAMES - played)})
    rows.sort(key=lambda r: -r.get("starts", -1))
    out = {"week": w, "range": [s.isoformat(), e.isoformat()], "teams": rows,
           "note": "goalie_starts is the lineup ceiling (team games that fit a G slot), "
                   "not confirmed starts; tandems will start fewer. goalie_gp_so_far counts "
                   "goalie games already played in an active G slot since the week began "
                   "(today included once the game has started)."}
    if so_far_err:
        out["goalie_gp_so_far_error"] = so_far_err
    return out


# ---------------------------------------------------------------- league-wide data
ROSTER_TIMEFRAMES = ("PROJ_SEASON", "YTD", "LAST_SEASON")
SKATER_OUT = ["GP", *SKATER_CATS]
GOALIE_OUT = ["GP", "W", "SV", "GA", "SA", "MIN"]
DAILY_GOALIE_OUT = ["W", "GA", "SA", "SV", "MIN"]


async def _gather(coros, limit: int = 4) -> list:
    """Run coroutines with bounded concurrency; exceptions are returned, not raised."""
    sem = asyncio.Semaphore(limit)

    async def run(c):
        async with sem:
            return await c
    return await asyncio.gather(*(run(c) for c in coros), return_exceptions=True)


def _et_today() -> date:
    return datetime.now(ET).date()


def _day_ttl(d: date) -> float:
    """Recent dates still change (late games, stat corrections); older ones are settled."""
    return 120 if d >= _et_today() - timedelta(days=1) else 6 * 3600


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"date must be YYYY-MM-DD (US-Eastern game date), got {value!r}") from None


def _pick(stats: dict | None, keys: list[str]) -> dict | None:
    return None if stats is None else {k: stats.get(k) for k in keys}


async def _goalie_gp_so_far(team_ids: list[str], start: date, end: date) -> dict[str, int]:
    """Goalie games played in an active G slot per team, from start to today (ET)."""
    last = min(end, _et_today())
    days = [start + timedelta(days=i) for i in range((last - start).days + 1)] if last >= start else []
    out = {tid: 0 for tid in team_ids}
    for d in days:
        goalies = await FX.stats_rows("ALL", "POS_201", day=d, standard=True, ttl=_day_ttl(d))
        played = {g["fantrax_id"] for g in goalies if (g["stats"].get("GP") or 0) > 0}
        if not played:
            continue
        statuses = await _gather([FX.roster_on(tid, d, _day_ttl(d)) for tid in team_ids])
        for tid, st in zip(team_ids, statuses):
            if isinstance(st, Exception):
                raise st
            out[tid] += sum(1 for pid, slot in st.items() if slot == "active" and pid in played)
    return out


# Fantrax data for these timeframes changes at different speeds.
_TF_TTL = {"PROJ_SEASON": 6 * 3600, "YTD": 3600, "LAST_SEASON": 24 * 3600}


# How old a last good copy may be when Fantrax rejects a refresh.
_STALE_MAX = {"PROJ_SEASON": 48 * 3600, "LAST_SEASON": 48 * 3600, "YTD": 24 * 3600}


def _iso_minute(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


async def _timeframe_stats(status_filter: str, tfs: list[str], errors: list[dict],
                           label: str, stale: list[str] | None = None,
                           ) -> tuple[dict[str, dict[str, dict]], dict[str, dict]]:
    """({tf: {fantrax_id: raw stats}}, {fantrax_id: identity row}) for one status filter.
    A failed (timeframe, group) is logged in `errors`; if a last good copy is young enough
    it is used instead and listed in `stale` ("YTD/skaters@2026-10-04T06:00Z")."""
    stats: dict[str, dict[str, dict]] = {}
    ident: dict[str, dict] = {}
    prefix = "" if label == "rostered" else "FA:"
    for tf in tfs:
        stats[tf] = {}
        for group, standard in (("HOCKEY_SKATING", False), ("POS_201", True)):
            gname = "skaters" if group == "HOCKEY_SKATING" else "goalies"
            key = f"stats|{status_filter}|{group}|{tf}"
            try:
                rows = await FX.stats_rows(status_filter, group, tf, standard=standard,
                                           ttl=_TF_TTL.get(tf, 900))
                FX.last_good.put(key, rows)
            except Exception as ex:  # noqa: BLE001 — report per timeframe, keep the rest
                hit = FX.last_good.get(key, _STALE_MAX.get(tf, 24 * 3600))
                err = {"scope": label, "timeframe": tf, "group": gname,
                       "error": f"{type(ex).__name__}: {ex}",
                       "request": getattr(ex, "data", None),
                       "served_stale": _iso_minute(hit[0]) if hit else None}
                errors.append(err)
                if not hit:
                    continue
                rows = hit[1]
                if stale is not None:
                    stale.append(f"{prefix}{tf}/{gname}@{_iso_minute(hit[0])}")
            for r in rows:
                stats[tf][r["fantrax_id"]] = blank_projection_gaps(r["stats"], tf)
                ident.setdefault(r["fantrax_id"], r)
    return stats, ident


def _player_out(p: dict, stats: dict[str, dict[str, dict]], tfs: list[str]) -> dict:
    keys = GOALIE_OUT if "G" in p["positions"] else SKATER_OUT
    return {"fantrax_id": p["fantrax_id"], "name": p["name"], "positions": p["positions"],
            "nhl_team": p["nhl_team"], "roster_status": p.get("roster_status"),
            "injury_status": p.get("injury_status"), "start_status": p.get("start_status"),
            "stats": {tf: _pick(stats.get(tf, {}).get(p["fantrax_id"]), keys) for tf in tfs}}


def _fa_out(p: dict, stats: dict[str, dict[str, dict]], tfs: list[str]) -> dict:
    """Slim free-agent record: the site uses FAs only as a stats baseline."""
    keys = GOALIE_OUT if "G" in p["positions"] else SKATER_OUT
    return {"fantrax_id": p["fantrax_id"], "name": p["name"], "positions": p["positions"],
            "nhl_team": p["nhl_team"],
            "stats": {tf: _pick(stats.get(tf, {}).get(p["fantrax_id"]), keys) for tf in tfs}}


@tool()
async def get_league_rosters(timeframes: list[str] | None = None,
                             include_free_agents: bool = False,
                             fa_require_ytd_gp: bool = False) -> dict:
    """Every team's roster in one call. Per player: fantrax_id, name, positions, nhl_team,
    roster_status (active/reserve/injured_reserve), injury_status, start_status (goalies),
    and per timeframe the RAW totals plus GP (no per-game division, no rounding).
    Skaters: GP G A PIM SOG PPG PPA Hit Blk Tk FOW TOI. Goalies: GP W SV GA SA MIN.
    timeframes: any of PROJ_SEASON, YTD, LAST_SEASON (default all three). Fantrax does
    not project Tk or TOI: they are null under PROJ_SEASON. A timeframe value is null when
    Fantrax has no row for the player (e.g. no NHL games last season).
    include_free_agents: also return unrostered players as an extra team with code "FA",
    limited to players with >= 1 GP in YTD or LAST_SEASON. Free agents carry only
    fantrax_id, name, positions, nhl_team and stats (no status/injury/start fields).
    fa_require_ytd_gp: keep only free agents with >= 1 GP in YTD (much smaller early season).
    The first call pulls the whole player pool and can take a minute; later calls are cached.
    If a request fails, the rest is still returned: see `errors` and `incomplete`."""
    tfs = list(timeframes or ROSTER_TIMEFRAMES)
    bad = [t for t in tfs if t not in ROSTER_TIMEFRAMES]
    if bad:
        raise ValueError(f"timeframes must be from {list(ROSTER_TIMEFRAMES)}; got {bad}. "
                         "PROJ_GAME is not per game and is not offered here.")
    teams = await FX.teams()
    rosters = await _gather([FX.roster(tid, None) for tid in teams])
    errors: list[dict] = []
    stale: list[str] = []
    # Rostered pool first: the projection depends on it. Free agents last, so their much
    # bigger pull can't trip Fantrax's burst limit before the rostered data is in.
    stats, _ = await _timeframe_stats("ALL_TAKEN", tfs, errors, "rostered", stale)
    out = []
    for (tid, name), ros in zip(teams.items(), rosters):
        if isinstance(ros, Exception):
            out.append({"team_id": tid, "code": team_code(tid), "name": name, "error": str(ros)})
            continue
        out.append({"team_id": tid, "code": team_code(tid), "name": name,
                    "players": [_player_out(p, stats, tfs) for p in ros["players"]]})

    if include_free_agents:
        # YTD and LAST_SEASON decide who is kept, even when not requested for output.
        pull = list(dict.fromkeys(tfs + ["YTD", "LAST_SEASON"]))
        fa_stats, fa_ident = await _timeframe_stats("ALL_AVAILABLE", pull, errors, "free_agents", stale)
        gate_tfs = ("YTD",) if fa_require_ytd_gp else ("YTD", "LAST_SEASON")
        played = {pid for tf in gate_tfs
                  for pid, st in fa_stats.get(tf, {}).items() if (st.get("GP") or 0) >= 1}
        fa_players = [_fa_out(fa_ident[pid], fa_stats, tfs)
                      for pid in sorted(played, key=lambda i: fa_ident[i]["name"] or "")]
        out.append({"team_id": None, "code": "FA", "name": "Free agents", "players": fa_players})

    res = {"timeframes": tfs, "teams": out}
    if errors:
        # Failed and not covered by a last good copy -> values null, listed in "incomplete".
        # Covered by a last good copy -> values present, listed in "stale".
        res["errors"] = errors
        missing = sorted({("" if e["scope"] == "rostered" else "FA:") + f"{e['timeframe']}/{e['group']}"
                          for e in errors if not e["served_stale"]})
        if missing:
            res["incomplete"] = missing
    if stale:
        res["stale"] = sorted(stale)
    return res


@tool()
async def lineup_plan(week: int | None = None, from_date: str | None = None) -> dict:
    """For every team, per day of the week: the slot assignment from the lineup matching
    (date -> lineup [{slot, fantrax_id, name}]) plus `unused` players whose NHL team plays
    that day but who get no slot. Same algorithm as lineup_capacity; uses current rosters
    for every day, so future days assume no roster moves."""
    w, s, e = await _week(week)
    if from_date:
        s = max(s, _parse_day(from_date))
    tbd = await _teams_by_day(s, e)
    teams = await FX.teams()
    rosters = await _gather([FX.roster(tid, None) for tid in teams])
    out = []
    for (tid, name), ros in zip(teams.items(), rosters):
        if isinstance(ros, Exception):
            out.append({"team_id": tid, "code": team_code(tid), "name": name, "error": str(ros)})
            continue
        out.append({"team_id": tid, "code": team_code(tid), "name": name,
                    "days": plan_week(_to_lineup(ros["players"]), tbd, LINEUP_SLOTS)})
    return {"week": w, "range": [s.isoformat(), e.isoformat()], "slots": LINEUP_SLOTS,
            "teams": out}


@tool()
async def get_daily_player_stats(date: str) -> dict:  # noqa: A002 — public arg name
    """Every NHL player who played on `date` (YYYY-MM-DD, US-Eastern game date), rostered or
    free agent, one row each: fantrax_id, name, positions, nhl_team, owner (team code or
    "FA"), slot_status that day for rostered players (active / bench / ir), and raw stats.
    Skaters: G A PIM SOG PPG PPA Hit Blk Tk FOW TOI (TOI decimal minutes).
    Goalies: W GA SA SV MIN (raw components, no rates)."""
    d = _parse_day(date)
    ttl = _day_ttl(d)
    sk = await FX.stats_rows("ALL", "HOCKEY_SKATING", day=d, ttl=ttl,
                             stop_after_idle_pages=2, played_key="GP")
    go = await FX.stats_rows("ALL", "POS_201", day=d, standard=True, ttl=ttl,
                             stop_after_idle_pages=2, played_key="GP")
    played = [r for r in sk + go if (r["stats"].get("GP") or 0) > 0]
    owners = sorted({r["owner_team_id"] for r in played if r["owner_team_id"]})
    statuses = await _gather([FX.roster_on(tid, d, ttl) for tid in owners])
    slot_of: dict[str, dict[str, str]] = {}
    errors = {}
    for tid, st in zip(owners, statuses):
        if isinstance(st, Exception):
            errors[team_code(tid)] = f"{type(st).__name__}: {st}"
        else:
            slot_of[tid] = st
    rows = []
    for r in played:
        tid = r["owner_team_id"]
        goalie = "G" in r["positions"]
        rows.append({
            "fantrax_id": r["fantrax_id"], "name": r["name"], "positions": r["positions"],
            "nhl_team": r["nhl_team"], "owner": team_code(tid) if tid else "FA",
            "slot_status": slot_of.get(tid, {}).get(r["fantrax_id"]) if tid else None,
            "stats": _pick(r["stats"], DAILY_GOALIE_OUT if goalie else SKATER_CATS),
        })
    rows.sort(key=lambda x: (x["owner"] == "FA", x["owner"], x["name"] or ""))
    out = {"date": d.isoformat(), "players": rows,
           "counts": {"skaters": sum(1 for x in rows if "G" not in x["positions"]),
                      "goalies": sum(1 for x in rows if "G" in x["positions"]),
                      "rostered": sum(1 for x in rows if x["owner"] != "FA")}}
    if errors:
        out["slot_status_errors"] = errors
    return out


async def _health() -> dict:
    ok, err = await FX.check_login()
    exp = FX.cookie_expiry()
    out: dict[str, Any] = {"ok": ok, "logged_in": ok, "error": err, "cookie_expiry": exp}
    if exp.get("known"):
        left = exp["earliest_unix"] - datetime.now(timezone.utc).timestamp()
        exp["earliest_iso"] = datetime.fromtimestamp(exp["earliest_unix"], timezone.utc).isoformat()
        exp["days_left"] = round(left / 86400, 1)
        if left < 3 * 86400:
            out["warning"] = "A Fantrax cookie expires within 3 days; re-export it."
    if ok:
        try:
            out["category_map"] = check_against_live(await FX.category_codes())
            out["ok"] = out["category_map"]["ok"]
        except Exception as e:  # noqa: BLE001
            out["category_map"] = {"ok": False, "error": str(e)}
            out["ok"] = False
    return {**out, **fetch_stamp()}


@tool()
async def session_health() -> dict:
    """Fantrax login validity (uncached request), cookie expiry, and whether the pinned
    category-id map still matches Fantrax. ok=false means do not publish data."""
    return await _health()


@tool()
async def fantrax_raw(method: str, data: dict | None = None, period: int | None = None,
                      label_categories: bool = True, max_kb: int = 400) -> dict:
    """Debug: call an allowlisted READ-ONLY Fantrax fxpa method and return the raw response.
    period: for getStandings, keep only that period's table (the full response is ~800 KB).
    label_categories renames category-id keys ('2010#2130#-1', '2130') to names ('G').
    Responses larger than max_kb are refused with their size, so narrow the request."""
    if method not in READ_METHODS:
        return {"error": "not allowlisted", "allowed": sorted(READ_METHODS)}
    out = await FX.call(method, **(data or {}))
    if period is not None and method == "getStandings":
        out = slim_raw(out, period)
    if label_categories:
        out = label_keys(out)
    size_kb = len(json.dumps(out, ensure_ascii=False)) / 1024
    if size_kb > max_kb:
        return {"error": "response too large", "size_kb": round(size_kb),
                "max_kb": max_kb, "keys": sorted(out)[:40],
                "hint": "pass period= for getStandings, lower maxResultsPerPage, or raise max_kb"}
    return out

@tool()
async def get_league_standings() -> dict:
    """League standings in COMBINED order: division + division rank, W-L-T,
    points (2 per matchup win, 1 per tie), win%, division record, games back,
    season category points for/against (cat_pts_* include the in-progress week)."""
    return await build_standings(FX.call, code_for=team_code)
# ---------------------------------------------------------------- transport + auth
class BearerAuth:
    """Minimal ASGI guard: every HTTP request must carry the shared token."""

    def __init__(self, app, token: str | None) -> None:
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/healthz":
            if not self._authorized(scope):
                return await self._send(send, 401, b"unauthorized", b"text/plain")
            h = await _health()
            return await self._send(send, 200 if h["ok"] else 503,
                                    json.dumps(h).encode(), b"application/json")
        if scope["type"] == "http" and self.token:
            headers = dict(scope.get("headers") or [])
            got = headers.get(b"authorization", b"").decode()
            if not hmac.compare_digest(got, f"Bearer {self.token}"):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self.app(scope, receive, send)

    def _authorized(self, scope) -> bool:
        if not self.token:
            return True
        got = dict(scope.get("headers") or []).get(b"authorization", b"").decode()
        return hmac.compare_digest(got, f"Bearer {self.token}")

    @staticmethod
    async def _send(send, status: int, body: bytes, ctype: bytes) -> None:
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", ctype)]})
        await send({"type": "http.response.body", "body": body})


def main() -> None:
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=S.allowed_hosts,  # Host headers Caddy forwards, e.g. fantrax.example.lan
    )
    inner = mcp.streamable_http_app(transport_security=security, host=S.host)
    app = BearerAuth(inner, S.auth_token)
    uvicorn.run(app, host=S.host, port=S.port, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
