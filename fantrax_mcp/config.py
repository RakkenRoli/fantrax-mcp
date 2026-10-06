"""Settings and league constants."""
from __future__ import annotations

import sys
import os
import re
from dataclasses import dataclass
from datetime import date, timedelta

from dotenv import load_dotenv

load_dotenv()

# Daily active lineup caps (not the roster cap)
LINEUP_SLOTS: dict[str, int] = {"C": 3, "LW": 3, "RW": 3, "D": 6, "G": 2}
GOALIE_MIN_GAMES = 3
ROSTER_SIZE = int(os.environ.get("ROSTER_SIZE", "23"))  # active + bench cap; IR not counted
IR_SLOTS = int(os.environ.get("IR_SLOTS", "6"))         # separate injured-reserve slots
SKATER_CATS = ["G", "A", "PIM", "SOG", "PPG", "PPA", "Hit", "Blk", "Tk", "FOW", "TOI"]
GOALIE_CATS = ["W", "GAA", "SV", "SV%"]
LIGHT_NIGHT_MAX_GAMES = 7  # nights with <= this many NHL games

# Fantrax short codes -> NHL API abbreviations (unmapped codes pass through)
FANTRAX_TO_NHL = {
    "TB": "TBL", "NJ": "NJD", "LA": "LAK", "SJ": "SJS", "CLS": "CBJ",
    "MON": "MTL", "WAS": "WSH", "NAS": "NSH", "CAL": "CGY", "WIN": "WPG",
    "VEG": "VGK", "UTAH": "UTA", "ARI": "UTA",
}


def nhl_abbrev(code: str | None) -> str | None:
    if not code:
        return None
    code = code.strip().upper()
    return FANTRAX_TO_NHL.get(code, code)


def _req(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required env var {name}")
    return val


def _auth_token() -> str | None:
    """MCP_AUTH_TOKEN is required: the server is published through Tailscale Funnel.

    MCP_ALLOW_NO_AUTH=1 turns this off for local tests only.
    """
    tok = (os.environ.get("MCP_AUTH_TOKEN") or "").strip()
    if tok:
        if len(tok) < 24:
            raise RuntimeError("MCP_AUTH_TOKEN is too short (need at least 24 characters)")
        return tok
    if os.environ.get("MCP_ALLOW_NO_AUTH") == "1":
        print("WARNING: MCP_ALLOW_NO_AUTH=1, server runs WITHOUT auth", file=sys.stderr)
        return None
    raise RuntimeError(
        "Missing required env var MCP_AUTH_TOKEN (the server is public through Tailscale "
        "Funnel). Set MCP_ALLOW_NO_AUTH=1 only for local tests."
    )


@dataclass(frozen=True)
class Settings:
    league_id: str
    team_name: str
    team_id: str | None
    cookie_file: str
    auth_token: str | None
    host: str
    port: int
    season_first_day: date
    season_last_day: date
    n_weeks: int
    nhl_season: str
    allowed_hosts: list[str]

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            league_id=_req("FANTRAX_LEAGUE_ID"),
            team_name=os.environ.get("FANTRAX_TEAM_NAME", "").strip().strip('"'),
            team_id=os.environ.get("FANTRAX_TEAM_ID") or None,
            cookie_file=os.environ.get("FANTRAX_COOKIE_FILE", "fantrax_cookies.json"),
            auth_token=_auth_token(),
            host=os.environ.get("MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("MCP_PORT", "8765")),
            season_first_day=date.fromisoformat(os.environ.get("SEASON_FIRST_DAY", "2026-10-06")),
            season_last_day=date.fromisoformat(os.environ.get("SEASON_LAST_DAY", "2027-04-04")),
            n_weeks=int(os.environ.get("N_WEEKS", "25")),
            nhl_season=os.environ.get("NHL_SEASON", "20262027"),
            allowed_hosts=[h.strip() for h in os.environ.get(
                "MCP_ALLOWED_HOSTS", "127.0.0.1:*,localhost:*").split(",") if h.strip()],
        )


def week_ranges(first_day: date, last_day: date, n_weeks: int) -> dict[int, tuple[date, date]]:
    """Mon-Sun weeks counted back from the last day; week 1 absorbs opening days.

    Assumption to verify against Fantrax's own schedule page. If Fantrax
    splits week 1 differently, change SEASON_FIRST_DAY or override here.
    """
    weeks: dict[int, tuple[date, date]] = {}
    for k in range(n_weeks, 1, -1):
        end = last_day - timedelta(days=7 * (n_weeks - k))
        weeks[k] = (end - timedelta(days=6), end)
    weeks[1] = (first_day, weeks[2][0] - timedelta(days=1))
    return dict(sorted(weeks.items()))


def current_week(weeks: dict[int, tuple[date, date]], today: date | None = None) -> int:
    today = today or date.today()
    for k, (s, e) in weeks.items():
        if s <= today <= e:
            return k
    return 1 if today < weeks[1][0] else max(weeks)


_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
_PERIOD_RE = re.compile(r"^(\d+) \((\w{3}) (\d+) - (\w{3}) (\d+)\)$")


def parse_periods(period_list: list[str], season_start_year: int) -> dict[int, tuple[date, date]]:
    """Parse Fantrax periodList entries like '19 (Feb 1 - Feb 14)'.

    Months Jul-Dec belong to the season's start year, Jan-Jun to the next year.
    """
    def mk(mon: str, day: str) -> date:
        m = _MONTHS[mon]
        return date(season_start_year if m >= 7 else season_start_year + 1, m, int(day))

    out: dict[int, tuple[date, date]] = {}
    for entry in period_list:
        m = _PERIOD_RE.match(entry.strip())
        if m:
            n, m1, d1, m2, d2 = m.groups()
            out[int(n)] = (mk(m1, d1), mk(m2, d2))
    return out
