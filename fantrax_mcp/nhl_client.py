"""NHL schedule via api-web.nhle.com (no auth)."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

import httpx

from .cache import TTLCache

BASE = "https://api-web.nhle.com/v1"
REGULAR_SEASON = 2


class NHLClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(base_url=BASE, timeout=20, follow_redirects=True)
        self._cache = TTLCache()

    async def _week_block(self, start: date) -> list[dict]:
        async def fetch() -> list[dict]:
            r = await self._http.get(f"/schedule/{start.isoformat()}")
            r.raise_for_status()
            return r.json().get("gameWeek", [])
        return await self._cache.get(("week", start), 6 * 3600, fetch)

    async def games_by_day(self, start: date, end: date) -> dict[date, list[tuple[str, str]]]:
        """{date: [(away, home), ...]} for regular-season games in [start, end]."""
        out: dict[date, list[tuple[str, str]]] = {}
        cursor = start
        while cursor <= end:
            for day in await self._week_block(cursor):
                d = date.fromisoformat(day["date"])
                if not (start <= d <= end):
                    continue
                out[d] = [
                    (g["awayTeam"]["abbrev"], g["homeTeam"]["abbrev"])
                    for g in day.get("games", [])
                    if g.get("gameType") == REGULAR_SEASON
                ]
            cursor += timedelta(days=7)
        d = start
        while d <= end:  # fill empty days explicitly
            out.setdefault(d, [])
            d += timedelta(days=1)
        return dict(sorted(out.items()))

    @staticmethod
    def teams_playing(games: list[tuple[str, str]]) -> set[str]:
        return {t for g in games for t in g}

    @staticmethod
    def summarize(games_by_day: dict[date, list[tuple[str, str]]], light_max: int) -> dict:
        per_team: dict[str, dict] = defaultdict(lambda: {"games": 0, "light_night_games": 0, "dates": []})
        nights = []
        for d, games in games_by_day.items():
            light = 0 < len(games) <= light_max
            nights.append({"date": d.isoformat(), "weekday": d.strftime("%a"),
                           "n_games": len(games), "light_night": light})
            for t in NHLClient.teams_playing(games):
                per_team[t]["games"] += 1
                per_team[t]["dates"].append(d.isoformat())
                if light:
                    per_team[t]["light_night_games"] += 1
        teams = dict(sorted(per_team.items(), key=lambda kv: (-kv[1]["games"], -kv[1]["light_night_games"])))
        return {"nights": nights, "teams": teams}

    async def aclose(self) -> None:
        await self._http.aclose()
