# Changelog

Changes to tool output that client code should know about. Newest first.

## 0.3.0

- Additive only: every player record gains `start_status` ("confirmed" |
  "expected" | null, goalies) and `injury_status`; `league_lineup_capacity` rows gain
  `goalie_gp_so_far` and `goalie_gp_needed` (null if the daily lookup failed, with
  `goalie_gp_so_far_error` at the top level). New tools: `get_league_rosters`,
  `lineup_plan`, `get_daily_player_stats`. Under `PROJ_SEASON`, `Tk` and `TOI` are null in
  `get_league_rosters` (Fantrax does not project them); other tools are unchanged.

## 0.2.1

- Tools no longer raise on bad input: an unknown `team` or out-of-range `week`
  returns `{"error": "...", "fetched_at", "nhl_date"}`. Check for `error` before reading fields.
- New tool `get_league_standings()`; `team` codes now work in every tool that
  takes `team` (previously `get_roster` and `lineup_capacity` rejected codes like `AVR`).
