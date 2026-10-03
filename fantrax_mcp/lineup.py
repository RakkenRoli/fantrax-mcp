"""Daily-lineup simulation: how many starts does a roster actually get?

Each day, players whose NHL team plays compete for the active slots
(3C/3LW/3RW/6D/2G). Multi-position eligibility makes this a bipartite
matching problem; we solve it exactly (Kuhn's augmenting paths).
Players earlier in the list win ties, so pass them in priority order.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date


@dataclass
class LineupPlayer:
    name: str
    positions: set[str]
    nhl_team: str | None
    available: bool = True  # False for IR / out
    meta: dict = field(default_factory=dict)


def _expand(slots: dict[str, int]) -> list[str]:
    return [pos for pos, n in slots.items() for _ in range(n)]


def assign_day(players: list[LineupPlayer], slots: dict[str, int]) -> dict[int, str]:
    """Return {player_index: slot_position} for a maximum lineup."""
    slot_list = _expand(slots)
    owner = [-1] * len(slot_list)

    def augment(pi: int, seen: list[bool]) -> bool:
        for si, pos in enumerate(slot_list):
            if pos in players[pi].positions and not seen[si]:
                seen[si] = True
                if owner[si] == -1 or augment(owner[si], seen):
                    owner[si] = pi
                    return True
        return False

    for pi in range(len(players)):
        augment(pi, [False] * len(slot_list))
    return {pi: slot_list[si] for si, pi in enumerate(owner) if pi != -1}


def simulate_week(
    roster: list[LineupPlayer],
    teams_by_day: dict[date, set[str]],
    slots: dict[str, int],
) -> dict:
    starts: Counter[str] = Counter()
    team_games: Counter[str] = Counter()
    days = []
    for d, teams in teams_by_day.items():
        playing = [p for p in roster if p.available and p.nhl_team in teams]
        for p in playing:
            team_games[p.name] += 1
        assigned = assign_day(playing, slots)
        used = Counter(assigned.values())
        for pi in assigned:
            starts[playing[pi].name] += 1
        benched = [playing[i].name for i in range(len(playing)) if i not in assigned]
        days.append({
            "date": d.isoformat(), "weekday": d.strftime("%a"),
            "playing": len(playing), "started": len(assigned),
            "benched_while_playing": benched,
            "open_slots": {pos: n - used.get(pos, 0) for pos, n in slots.items() if n - used.get(pos, 0) > 0},
        })
    open_totals: Counter[str] = Counter()
    for day in days:
        open_totals.update(day["open_slots"])
    players = [
        {"name": p.name, "positions": sorted(p.positions), "nhl_team": p.nhl_team,
         "available": p.available, "team_games": team_games[p.name], "usable_starts": starts[p.name],
         "wasted_games": team_games[p.name] - starts[p.name]}
        for p in roster
    ]
    return {
        "total_starts": sum(starts.values()),
        "total_wasted_games": sum(pl["wasted_games"] for pl in players),
        "open_slot_days_by_position": dict(open_totals),
        "days": days,
        "players": sorted(players, key=lambda x: (-x["wasted_games"], x["name"])),
    }


def starts_by_position(sim: dict, roster: list[LineupPlayer]) -> Counter:
    pos_of = {p.name: "G" if "G" in p.positions else "/".join(sorted(p.positions)) for p in roster}
    c: Counter[str] = Counter()
    for pl in sim["players"]:
        c[pos_of[pl["name"]]] += pl["usable_starts"]
    return c
