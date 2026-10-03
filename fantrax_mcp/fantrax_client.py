"""Read-only Fantrax client over the internal fxpa/req endpoint.

Calibrated against the live league (2026-09-27):
- team list: getStandings -> fantasyTeamInfo {teamId: {name}}
- player stats: getPlayerStats with statusOrTeamFilter / positionOrGroup /
  seasonOrProjection + timeframeTypeCode (both required); server-side sort is ignored
- week calendar: getPlayerStats -> periodList
- matchups: getLiveScoringStats(period) -> statsPerTeam.allTeamsStats[teamId].ACTIVE,
  keyed by a shared matchup pair id like "11|4" ("-1" = bye)
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from .cache import TTLCache
from .categories import GOALIE_COMPONENT_SCIP, GOALIE_SCIP, TEAM_CODES
from .config import Settings, nhl_abbrev
from .league_data import (GOALIE_RAW, SKATER_RAW, injury_status, owner_team_id,
                          parse_day_periods, roster_day_status, row_stats, scip_index,
                          start_status)
from .standings import parse_schedule

FXPA_URL = "https://www.fantrax.com/fxpa/req"

# Only read methods may ever be sent. No claims/drops/lineup changes.
READ_METHODS = {
    "getFantasyLeagueInfo",
    "getFantasyTeams",
    "getTeamRosterInfo",
    "getStandings",
    "getPlayerStats",
    "getLiveScoringStats",
    "getTransactionDetailsHistory",
    "getPendingTransactions",
    "getTradeBlocks",
    "getPlayerProfile",
}

ROSTER_STATUS = {"1": "active", "2": "reserve", "3": "injured_reserve", "9": "minors"}
POS_GROUP = {"SKATERS": "HOCKEY_SKATING", "C": "POS_206", "LW": "POS_203",
             "RW": "POS_204", "D": "POS_202", "G": "POS_201"}
TIMEFRAMES = ("PROJ_SEASON", "PROJ_GAME", "YTD", "LAST_SEASON")
_TOI_RE = re.compile(r"^(\d+):(\d{2})$")


class NotLoggedIn(RuntimeError):
    pass


def load_cookies(path: str) -> httpx.Cookies:
    """Accepts Cookie-Editor JSON export, a {name: value} dict, or a raw Cookie header."""
    text = Path(path).read_text(encoding="utf-8").strip()
    jar = httpx.Cookies()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        for part in text.removeprefix("Cookie:").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                jar.set(k, v, domain=".fantrax.com")
        return jar
    if isinstance(data, dict):
        for k, v in data.items():
            jar.set(k, str(v), domain=".fantrax.com")
    else:
        for c in data:
            jar.set(c["name"], c["value"], domain=c.get("domain", ".fantrax.com"))
    return jar


def _cell_value(cell: Any) -> Any:
    if isinstance(cell, dict):
        cell = cell.get("content", cell.get("value"))
    if isinstance(cell, str):
        s = cell.replace(",", "").strip()
        t = _TOI_RE.match(s)
        if t:  # TOI "mm:ss" -> minutes
            return round(int(t.group(1)) + int(t.group(2)) / 60, 2)
        try:
            return float(s) if "." in s else int(s)
        except ValueError:
            return cell
    return cell


def _header_names(header: Any) -> list[str]:
    cells = header.get("cells", header) if isinstance(header, dict) else header or []
    return [c.get("shortName") or c.get("name") or f"col{i}" for i, c in enumerate(cells)]


def _scorer_info(scorer: dict) -> dict:
    pos = scorer.get("posShortNames") or ""
    icons = scorer.get("icons") or []
    notes = [i.get("tooltip") for i in icons if isinstance(i, dict) and i.get("tooltip")]
    return {
        "name": scorer.get("name"),
        "fantrax_id": scorer.get("scorerId"),
        "nhl_team": nhl_abbrev(scorer.get("teamShortName")),
        "positions": [p.strip() for p in pos.replace("/", ",").split(",") if p.strip()],
        "notes": notes,
        "start_status": start_status(notes) if pos.strip() == "G" else None,
        "injury_status": injury_status(notes),
    }


def flatten_rows(header: Any, rows: list[dict]) -> list[dict]:
    names = _header_names(header)
    out = []
    for row in rows:
        scorer = row.get("scorer")
        if not scorer:
            continue  # empty roster slot
        rec = _scorer_info(scorer)
        if "statusId" in row:
            rec["roster_status"] = ROSTER_STATUS.get(str(row["statusId"]), str(row["statusId"]))
        rec["stats"] = {n: _cell_value(c) for n, c in zip(names, row.get("cells", []))}
        out.append(rec)
    return out


class FantraxClient:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._http = httpx.AsyncClient(
            timeout=30,
            cookies=load_cookies(settings.cookie_file),
            headers={"User-Agent": "Mozilla/5.0 (fantrax-mcp read-only)"},
        )
        self._cache = TTLCache()

    async def call(self, method: str, **data: Any) -> dict:
        if method not in READ_METHODS:
            raise PermissionError(f"{method} is not an allowlisted read method")
        try:
            return await self._post(method, data)
        except NotLoggedIn:
            # Cookie may have been refreshed on disk: reload once and retry, no restart needed.
            self._http.cookies = load_cookies(self.s.cookie_file)
            return await self._post(method, data)

    async def _post(self, method: str, data: dict) -> dict:
        payload = {"msgs": [{"method": method, "data": {"leagueId": self.s.league_id, **data}}]}
        r = await self._http.post(FXPA_URL, params={"leagueId": self.s.league_id}, json=payload)
        r.raise_for_status()
        body = r.json()
        err = body.get("pageError")
        if err:
            if err.get("code") == "WARNING_NOT_LOGGED_IN":
                raise NotLoggedIn("Fantrax cookie expired — re-export it from the browser")
            raise RuntimeError(f"Fantrax error: {err}")
        return body["responses"][0]["data"]

    async def cached(self, ttl: float, method: str, **data: Any) -> dict:
        key = (method, json.dumps(data, sort_keys=True))
        return await self._cache.get(key, ttl, lambda: self.call(method, **data))

    # ---------- league ----------
    async def teams(self) -> dict[str, str]:
        data = await self.cached(3600, "getStandings")
        info = data.get("fantasyTeamInfo") or {}
        return {tid: t.get("name", tid) for tid, t in info.items()}

    async def standings_schedule_raw(self, ttl: float = 120) -> dict:
        """getStandings(view=SCHEDULE): every period's cumulative H2H table (~800 KB).
        Fantrax ignores a period param here and always returns all periods."""
        return await self.cached(ttl, "getStandings", view="SCHEDULE")

    async def schedule(self, ttl: float = 120) -> dict[int, dict]:
        """Parsed {period: {pairings, byes, ...}} from the standings table."""
        return parse_schedule(await self.standings_schedule_raw(ttl))

    async def resolve_team(self, team: str = "me") -> tuple[str, str]:
        """'me', a stable code (GTX, AVR...), a Fantrax teamId, or part of a team name."""
        teams = await self.teams()
        team = (team or "me").strip()
        if team.casefold() == "me":
            if self.s.team_id:
                return self.s.team_id, teams.get(self.s.team_id, self.s.team_name)
            team = self.s.team_name
        for tid, code in TEAM_CODES.items():
            if code == team.upper():
                return tid, teams.get(tid, code)
        if team in teams:
            return team, teams[team]
        low = team.casefold()
        for tid, name in teams.items():
            if low in name.casefold():
                return tid, name
        known = sorted(f"{TEAM_CODES.get(t, t)} ({n})" for t, n in teams.items())
        raise ValueError(f"No team matching {team!r}. Use a code, team id or name part: {known}")

    # ---------- metadata ----------
    async def _meta(self, group: str | None = None) -> dict:
        kw = {"statusOrTeamFilter": "ALL_AVAILABLE", "maxResultsPerPage": "1", "pageNumber": "1"}
        if group:
            kw["positionOrGroup"] = group
        return await self.cached(12 * 3600, "getPlayerStats", **kw)

    async def periods(self) -> list[str]:
        return (await self._meta()).get("periodList") or []

    async def season_codes(self) -> dict[str, tuple[str, str]]:
        """Map our timeframe names to (seasonOrProjection code, timeframeTypeCode)."""
        opts = (await self._meta()).get("seasonOrProjections") or []
        out: dict[str, tuple[str, str]] = {}
        for o in opts:
            code, tf = o.get("code", ""), o.get("timeframeTypeCode", "")
            if code.startswith("PROJECTION") and tf == "PROJECTED_SEASON":
                out.setdefault("PROJ_SEASON", (code, tf))
            elif code.startswith("PROJECTION") and tf == "PROJECTED_WEEKLY":
                out.setdefault("PROJ_GAME", (code, tf))
        reg_ytd = [o for o in opts if o.get("code", "").startswith("SEASON_")
                   and o.get("timeframeTypeCode") == "YEAR_TO_DATE" and "Reg Season" in o.get("name", "")]
        if reg_ytd:
            out["YTD"] = (reg_ytd[0]["code"], "YEAR_TO_DATE")
        if len(reg_ytd) > 1:
            out["LAST_SEASON"] = (reg_ytd[1]["code"], "YEAR_TO_DATE")
        by_date = [o for o in opts if o.get("code", "").startswith("SEASON_")
                   and o.get("timeframeTypeCode") == "BY_DATE" and "Reg Season" in o.get("name", "")]
        if by_date:
            out["BY_DATE"] = (by_date[0]["code"], "BY_DATE")
        return out

    async def category_codes(self) -> dict[str, str]:
        """Scoring-category ids ('2010#2130#-1') -> short names ('G'), skaters + goalies."""
        out: dict[str, str] = {}
        for grp in ("HOCKEY_SKATING", "POS_201"):
            for c in ((await self._meta(grp)).get("tableHeader") or {}).get("cells", []):
                if c.get("scipId"):
                    out[c["scipId"]] = c.get("shortName") or c["scipId"]
        return out

    # ---------- player stats ----------
    async def player_stats(self, status_filter: str, position: str = "SKATERS",
                           timeframe: str = "PROJ_SEASON", limit: int = 100, page: int = 1) -> list[dict]:
        codes = await self.season_codes()
        if timeframe not in codes:
            raise ValueError(f"timeframe must be one of {sorted(codes)}")
        code, tf = codes[timeframe]
        group = POS_GROUP.get(position.upper())
        if not group:
            raise ValueError(f"position must be one of {sorted(POS_GROUP)}")
        data = await self.cached(
            900, "getPlayerStats",
            statusOrTeamFilter=status_filter, positionOrGroup=group,
            seasonOrProjection=code, timeframeTypeCode=tf,
            maxResultsPerPage=str(limit), pageNumber=str(page),
        )
        return flatten_rows(data.get("tableHeader", {}), data.get("statsTable", []))

    async def free_agents(self, position: str = "SKATERS", timeframe: str = "PROJ_SEASON",
                          limit: int = 100, page: int = 1, include_inactive: bool = False) -> list[dict]:
        status = "ALL_AVAILABLE" if include_inactive else "ACTIVE_AVAILABLE"
        return await self.player_stats(status, position, timeframe, limit, page)

    async def team_stats(self, team_id: str, timeframe: str = "PROJ_SEASON") -> dict[str, dict]:
        """{fantrax_id: stats} for a fantasy team's skaters and goalies."""
        out: dict[str, dict] = {}
        for pos in ("SKATERS", "G"):
            for p in await self.player_stats(f"FANTASY_TEAM_{team_id}", pos, timeframe, 50):
                out[p["fantrax_id"]] = p["stats"]
        return out

    async def goalie_components(self, status_filter: str = "ALL", timeframe: str = "YTD",
                                limit: int = 100, page: int = 1) -> list[dict]:
        """Goalie W/GAA/SV/SV% plus GA, SA (shots against) and MIN, from the "Standard"
        category view (scoringCategoryType=1). The default "Tracked" view hides GA/SA/MIN."""
        codes = await self.season_codes()
        if timeframe not in codes:
            raise ValueError(f"timeframe must be one of {sorted(codes)}")
        code, tf = codes[timeframe]
        data = await self.cached(
            900, "getPlayerStats",
            statusOrTeamFilter=status_filter, positionOrGroup="POS_201",
            seasonOrProjection=code, timeframeTypeCode=tf, scoringCategoryType="1",
            maxResultsPerPage=str(limit), pageNumber=str(page),
        )
        header = data.get("tableHeader") or {}
        wanted = {**GOALIE_SCIP, **GOALIE_COMPONENT_SCIP}
        idx = {i: wanted[c["scipId"]] for i, c in enumerate(header.get("cells", []))
               if c.get("scipId") in wanted}
        out = []
        for row in data.get("statsTable", []):
            scorer = row.get("scorer")
            if not scorer:
                continue
            cells = row.get("cells", [])
            stats = {name: _cell_value(cells[i]) for i, name in idx.items() if i < len(cells)}
            out.append({"name": scorer.get("name"), "fantrax_id": scorer.get("scorerId"),
                        "nhl_team": nhl_abbrev(scorer.get("teamShortName")), "stats": stats})
        return out

    # ---------- league-wide raw stats ----------
    async def stats_rows(self, status_filter: str, group: str, timeframe: str | None = None,
                         day: date | None = None, standard: bool = False, ttl: float = 900,
                         per_page: int = 500, max_pages: int = 30,
                         stop_after_idle_pages: int | None = None,
                         played_key: str | None = None) -> list[dict]:
        """Every getPlayerStats row for a filter, flattened to identity + owner + raw stats
        (keyed by our names, mapped by scipId). Either `timeframe` (YTD, LAST_SEASON,
        PROJ_SEASON...) or `day` (single NHL date, BY_DATE). standard=True switches to the
        "Standard" category view, which is the only one with goalie GA/SA/MIN.
        stop_after_idle_pages: stop paging after that many consecutive pages in which no row
        has played_key > 0 (used for single-date pulls of the ~7,600-player pool)."""
        codes = await self.season_codes()
        key = "BY_DATE" if day else timeframe
        if key not in codes:
            raise ValueError(f"timeframe must be one of {sorted(k for k in codes if k != 'BY_DATE')}")
        code, tf = codes[key]
        kw: dict[str, Any] = {"statusOrTeamFilter": status_filter, "positionOrGroup": group,
                              "seasonOrProjection": code, "timeframeTypeCode": tf,
                              "maxResultsPerPage": str(per_page)}
        if day:
            kw["startDate"] = kw["endDate"] = day.isoformat()
        if standard:
            kw["scoringCategoryType"] = "1"
        wanted = GOALIE_RAW if group == "POS_201" else SKATER_RAW
        out: list[dict] = []
        idle = 0
        for page in range(1, max_pages + 1):
            data = await self.cached(ttl, "getPlayerStats", **kw, pageNumber=str(page))
            idx = scip_index(data.get("tableHeader") or {}, wanted)
            rows = []
            for row in data.get("statsTable") or []:
                scorer = row.get("scorer")
                if not scorer:
                    continue
                rec = _scorer_info(scorer)
                rec["owner_team_id"] = owner_team_id(row)
                rec["stats"] = row_stats(row, idx)
                rows.append(rec)
            out += rows
            if stop_after_idle_pages and played_key:
                idle = 0 if any((r["stats"].get(played_key) or 0) > 0 for r in rows) else idle + 1
                if idle >= stop_after_idle_pages:
                    break
            pages = (data.get("paginatedResultSet") or {}).get("totalNumPages") or 1
            if page >= pages or not rows:
                break
        return out

    # ---------- daily lineups ----------
    async def day_periods(self) -> dict[date, int]:
        """{date: Fantrax daily period index}; roster pages are addressed by this index."""
        data = await self.cached(12 * 3600, "getTeamRosterInfo", teamId=await self._any_team_id())
        lst = (data.get("displayedLists") or {}).get("periodList") or []
        return parse_day_periods(lst, self.s.season_first_day.year)

    async def _any_team_id(self) -> str:
        if self.s.team_id:
            return self.s.team_id
        return (await self.resolve_team("me"))[0]

    async def roster_on(self, team_id: str, day: date, ttl: float = 300) -> dict[str, str]:
        """{fantrax_id: active|bench|ir} for one fantasy team on one date."""
        periods = await self.day_periods()
        if day not in periods:
            raise ValueError(f"{day} is outside the Fantrax season calendar")
        idx = periods[day]
        data = await self.cached(ttl, "getTeamRosterInfo", teamId=team_id, period=str(idx))
        return roster_day_status(data, idx)

    # ---------- session ----------
    # Cookies that rotate on their own and say nothing about the Fantrax login.
    _NOISE_COOKIE_PREFIXES = ("__cf", "_cf", "cf_", "_ga", "_gid", "_gcl", "_fbp", "_hj",
                              "__gads", "__gpi", "_pb", "OptanonConsent", "usprivacy")

    def cookie_expiry(self, now: float | None = None) -> dict:
        """Earliest future expiry among Fantrax cookies that could carry the login.

        Skips Cloudflare/analytics cookies and cookies already expired: if login still
        works, an expired cookie evidently isn't needed."""
        import time
        now = time.time() if now is None else now
        try:
            data = json.loads(Path(self.s.cookie_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"known": False}
        if not isinstance(data, list):
            return {"known": False, "note": "cookie file has no expiry info (not a Cookie-Editor export)"}
        relevant, skipped = [], []
        for c in data:
            if not isinstance(c, dict):
                continue
            name, ts = c.get("name", ""), c.get("expirationDate")
            if name.startswith(self._NOISE_COOKIE_PREFIXES) or "fantrax" not in c.get("domain", "fantrax"):
                skipped.append(name)
            elif c.get("session") or not ts:
                continue
            elif ts <= now:
                skipped.append(name)
            else:
                relevant.append((ts, name))
        if not relevant:
            return {"known": False, "ignored": sorted(skipped),
                    "note": "no persistent Fantrax cookie with a future expiry"}
        ts, name = min(relevant)
        return {"known": True, "earliest_unix": ts, "earliest_cookie": name,
                "ignored": sorted(skipped)}

    async def check_login(self) -> tuple[bool, str | None]:
        """Uncached tiny request; (True, None) when the session is valid."""
        try:
            await self._post("getPlayerStats", {"statusOrTeamFilter": "ALL_AVAILABLE",
                                                "maxResultsPerPage": "1", "pageNumber": "1"})
            return True, None
        except NotLoggedIn as e:
            self._http.cookies = load_cookies(self.s.cookie_file)  # pick up a fresh export
            try:
                await self._post("getPlayerStats", {"statusOrTeamFilter": "ALL_AVAILABLE",
                                                    "maxResultsPerPage": "1", "pageNumber": "1"})
                return True, None
            except NotLoggedIn:
                return False, str(e)
        except Exception as e:  # noqa: BLE001 — health check reports, never raises
            return False, f"{type(e).__name__}: {e}"

    # ---------- rosters ----------
    async def roster(self, team: str = "me", timeframe: str | None = "PROJ_SEASON") -> dict:
        tid, name = await self.resolve_team(team)
        data = await self.cached(300, "getTeamRosterInfo", teamId=tid)
        players = []
        for table in data.get("tables", []):
            players += flatten_rows(table.get("header", {}), table.get("rows", []))
        if timeframe:
            stats = await self.team_stats(tid, timeframe)
            for p in players:
                if p["fantrax_id"] in stats:
                    p["stats"] = stats[p["fantrax_id"]]
        return {"team_id": tid, "team_name": name, "timeframe": timeframe, "players": players}

    # ---------- matchups ----------
    async def live_scoring(self, period: int) -> dict:
        return await self.cached(120, "getLiveScoringStats", period=str(period))

    async def aclose(self) -> None:
        await self._http.aclose()
