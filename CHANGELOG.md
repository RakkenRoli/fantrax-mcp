# Changelog

Changes to tool output that client code should know about. Newest first.

## 0.4.1

Breaking changes for the website code: none; additive.

- `get_league_rosters` gains `stale`: parts served from the last good copy after Fantrax
  rejected the refresh, e.g. `"YTD/skaters@2026-10-04T06:00Z"` (`FA:` prefix for free
  agents). Values are present, not null. Max age: 48 h PROJ_SEASON / LAST_SEASON, 24 h YTD.
- `incomplete` now lists only parts with no usable copy; it is omitted when empty.
- `errors[]` entries gain `served_stale` (timestamp or null).
- A Fantrax failure the tool cannot work around now returns
  `{"error": "...", "request": {"method", "data"}}` instead of failing the call
  (2026-10-04 run 2: the team-list request was rejected, which was not covered by the
  per-timeframe handling; it now also falls back to the last good team list).
- Fantrax requests: 0.75 s apart, retries after 2 / 5 / 15 s on `INVALID_REQUEST`, 429, 5xx.
  The rostered pool is always fetched before the free-agent pool.

## 0.4.0

Breaking changes for the website code: none; all additive.

- `get_league_rosters(..., include_free_agents=True)` adds a team entry with `code: "FA"`
  and `team_id: null`: unrostered players with >= 1 GP in YTD or LAST_SEASON, same
  per-player fields and timeframes, `roster_status: null`. Failures of the free-agent
  pulls show up in `incomplete` with an `FA:` prefix (e.g. `"FA:LAST_SEASON/goalies"`).
- `errors[]` entries in `get_league_rosters` gain `scope` (`"rostered"` | `"free_agents"`).
- `league_info` gains `goalie_min_rule`: `{min_games, penalty_if_missed, source}`.
  `penalty_if_missed` is null unless `GOALIE_MIN_PENALTY` is set; Fantrax's read API does
  not expose the rule text.
- Optional cookie write-back (`FANTRAX_COOKIE_WRITEBACK=1`); no output change.

## 0.3.1

- `get_league_rosters` no longer fails as a whole when Fantrax rejects one request. A
  failed timeframe/group leaves those values `null` and is listed in `errors`
  (`timeframe`, `group`, `error`, `request`) and `incomplete` (e.g. `"PROJ_SEASON/skaters"`).
  Check `incomplete` before treating a `null` as "no data".
- Fantrax errors now name the request that failed (method + parameters, never cookies).
- Requests to Fantrax run one at a time by default (`FANTRAX_MAX_CONCURRENCY`), and an
  `INVALID_REQUEST` is retried once.

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
