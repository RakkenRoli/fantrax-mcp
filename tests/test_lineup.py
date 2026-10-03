from datetime import date, timedelta

from fantrax_mcp.config import LINEUP_SLOTS, week_ranges
from fantrax_mcp.lineup import LineupPlayer, assign_day, simulate_week


def P(name, pos, team, avail=True):
    return LineupPlayer(name, set(pos.split(",")), team, avail)


def test_multi_position_matching_is_optimal():
    # Greedy would put the C/LW guy at C and bench a pure LW; matching must not.
    players = [P("flex", "C,LW", "A")] + [P(f"c{i}", "C", "A") for i in range(3)] + \
              [P(f"lw{i}", "LW", "A") for i in range(2)]
    assert len(assign_day(players, LINEUP_SLOTS)) == 6


def test_d_overflow_and_ir_excluded():
    roster = [P(f"d{i}", "D", "A") for i in range(8)] + [P("hurt", "D", "A", avail=False)]
    d = date(2026, 10, 12)
    sim = simulate_week(roster, {d: {"A"}}, LINEUP_SLOTS)
    assert sim["total_starts"] == 6 and sim["total_wasted_games"] == 2
    assert next(p for p in sim["players"] if p["name"] == "hurt")["team_games"] == 0


def test_add_on_off_night_gains_starts():
    base = [P(f"c{i}", "C", "A") for i in range(4)]
    d1, d2 = date(2026, 10, 12), date(2026, 10, 13)
    tbd = {d1: {"A"}, d2: {"B"}}
    before = simulate_week(base, tbd, LINEUP_SLOTS)["total_starts"]
    after = simulate_week(base[:3] + [P("streamer", "C", "B")], tbd, LINEUP_SLOTS)["total_starts"]
    assert (before, after) == (3, 4)


def test_week_calendar():
    w = week_ranges(date(2026, 10, 6), date(2027, 4, 4), 25)
    assert len(w) == 25 and w[25] == (date(2027, 3, 29), date(2027, 4, 4))
    assert w[1][0] == date(2026, 10, 6) and w[1][1] + timedelta(days=1) == w[2][0]
    assert all(w[k][0].weekday() == 0 for k in range(2, 26))
