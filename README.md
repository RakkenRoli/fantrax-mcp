# fantrax-mcp

Read-only MCP server for the Fantrax H2H-categories league (Gazdagréti Taxisok)
plus the NHL schedule. Runs in an LXC on a home server, reached over Tailscale via
Caddy, and is consumed by Claude Desktop through `mcp-remote`.

## Tools

| Tool | What it does |
|---|---|
| `league_info` | Teams (with stable codes), my team id, rules, week calendar |
| `get_roster(team)` | Roster with positions, NHL team, status, category stats |
| `get_free_agents(position, timeframe, ...)` | Available players with category stats (`{"players": [...]}`) |
| `get_matchup(week, team)` | One team's H2H: live W-L-T, cumulative totals both sides, per-category verdicts, Fantrax projection |
| `get_league_matchups(week)` | All pairings for a week + bye team(s), totals for both sides |
| `get_league_schedule()` | All 25 periods: pairings, byes, notes (3-bye weeks, playoff seeds) |
| `get_goalie_stats(team, timeframe)` | Goalie W/GAA/SV/SV% plus GA, SA, MIN and recomputed/pooled rates |
| `get_nhl_schedule(start, end)` | Games per night / per team |
| `get_fantasy_week(week)` | Week dates, team game counts, light-night games |
| `lineup_capacity(week, team, from_date)` | Daily lineup sim: starts, bench waste, open slots |
| `league_lineup_capacity(week, from_date)` | Compact lineup sim for all 13 teams in one call |
| `evaluate_add_drop(add, drop, week, ...)` | Net usable starts from a swap |
| `goalie_check(week, team, start_share, ...)` | Projected goalie starts vs 3-game minimum, any team |
| `get_league_standings()` | Standings in COMBINED order: division + division rank, W-L-T, pts, win%, div record, GB, category points for/against (include the in-progress week) |
| `session_health()` | Login validity, cookie expiry, category-map drift; `ok=false` = don't publish |
| `fantrax_raw(method, data, period, ...)` | Allowlisted read-only passthrough; `period` slims `getStandings`, ids labeled |

Every dict response carries `fetched_at` (UTC) and `nhl_date` (US-Eastern date).
`team` arguments accept `me`, a Fantrax team id, a stable code (`GTX`, `RSD`, `TN`...) or part of a name.
Bad input (unknown team, week out of range) returns `{"error": "..."}` instead of failing the call.

### Data sources

- **Matchup totals** come from `getStandings(view=SCHEDULE)`, the table Fantrax scores on.
  `getLiveScoringStats` is only used for projections; its `statsMap2` covers a slice of the week.
- **Category ids** are pinned in `categories.py` from Fantrax's header (G = 2130, A = 2090).
- **Team codes** are fixed per team id in `categories.TEAM_CODES`; Fantrax shortNames drift.
- **Schedule:** 13 teams, so one bye per week, except weeks 20 and 22 which Fantrax schedules
  with 3 byes so every team ends the regular season with exactly 2. Weeks 23-25 are playoffs.

### Daily job health gate

```bash
curl -fsS -H "Authorization: Bearer $MCP_AUTH_TOKEN" https://<host>/healthz >/dev/null \
  || { echo "Fantrax session unhealthy, not publishing" >&2; exit 1; }
```

`/healthz` returns 200 when logged in and the category map matches, 503 otherwise, 401 without the token.

No write methods exist. The raw passthrough rejects anything outside `READ_METHODS`.

## Install (Debian 12 LXC)

```bash
apt install -y python3-venv git
useradd -r -s /usr/sbin/nologin fantrax
mkdir -p /opt/fantrax-mcp /etc/fantrax-mcp
# copy this repo to /opt/fantrax-mcp
cd /opt/fantrax-mcp && python3 -m venv .venv && .venv/bin/pip install .
cp .env.example /etc/fantrax-mcp/env        # edit values
chown -R root:fantrax /etc/fantrax-mcp && chmod 750 /etc/fantrax-mcp && chmod 640 /etc/fantrax-mcp/*
cp deploy/fantrax-mcp.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now fantrax-mcp
```

Generate the token with `openssl rand -hex 32`.

## Fantrax cookie

Fantrax login can't be scripted reliably, so reuse a browser session:

1. Log in at fantrax.com and tick "remember me".
2. Export fantrax.com cookies with the Cookie-Editor extension (JSON).
3. Save the export as `/etc/fantrax-mcp/fantrax_cookies.json` (mode 640).

When it expires, tools raise `NotLoggedIn`. Drop a fresh export in place; the
server reloads it automatically on the next call.

## Caddy + Claude Desktop

- Add `deploy/Caddyfile.snippet` to Caddy. `tls internal` issues a cert from Caddy's
  local CA, which Node does not trust by default.
- Copy Caddy's root cert from the Caddy LXC
  (`/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt`, or `/data/caddy/...`
  in Docker) to the desktop and point `NODE_EXTRA_CA_CERTS` at it.
- Merge `deploy/claude_desktop_config.snippet.json` into `~/.config/Claude/claude_desktop_config.json`.
  The token goes through an env var because mcp-remote args must not contain spaces.

## First-run calibration

The Fantrax parsers mark unconfirmed request params and response keys with `VERIFY`.
In the first session, ask Claude to run `fantrax_raw` for `getFantasyLeagueInfo`,
`getTeamRosterInfo` (with `teamId`), `getPlayerStats`, `getStandings` (`view: SCHEDULE`),
and `getLiveScoringStats`, then fix the parsers against the real shapes.

Also check the week calendar from `league_info` against Fantrax's schedule page
(week 1 is assumed to run from `SEASON_FIRST_DAY` to the first Sunday before a full Mon–Sun week).

## Tests

```bash
.venv/bin/pip install pytest && .venv/bin/python -m pytest -q tests
```

Tests are isolated by `tests/conftest.py`: the cookie path is forced to the committed
`tests/fixtures/dummy_session.json` (or a per-test `tmp_path` copy), even when the service
env is loaded in the shell. No test reads or writes `/etc/fantrax-mcp/`.

Real `getStandings` fixtures for `test_league_standings.py` come from
`scripts/capture_standings_fixtures.py --out /tmp/fixtures` (run where the login works,
then commit the files to `tests/fixtures/`); until then those tests are skipped.

## Breaking changes for the website code

- **0.2.1** — tools no longer raise on bad input: an unknown `team` or out-of-range `week`
  returns `{"error": "...", "fetched_at", "nhl_date"}`. Check for `error` before reading fields.
- **0.2.1** — new tool `get_league_standings()`; `team` codes now work in every tool that
  takes `team` (previously `get_roster` and `lineup_capacity` rejected codes like `AVR`).

## Troubleshooting

- **401**: token mismatch between `/etc/fantrax-mcp/env` and the Desktop config.
- **421 Invalid Host header**: add the hostname Caddy forwards to `MCP_ALLOWED_HOSTS`.
- **mcp-remote TLS error**: `NODE_EXTRA_CA_CERTS` is missing or points at the wrong cert.
- **Stale mcp-remote auth state**: `rm -rf ~/.mcp-auth` and restart Claude Desktop.
