# fantrax-mcp

An [MCP](https://modelcontextprotocol.io) server that lets Claude (or any MCP client) read
your **Fantrax fantasy hockey league** and the **NHL schedule**, so you can ask things like:

- "How is my matchup going this week, category by category?"
- "Which free-agent centers would help my faceoffs and still get games this week?"
- "If I drop X for Y, how many extra starts do I actually get?"
- "Will I hit the goalie minimum this week?"
- "Show the standings by division." / "Who played last night, and was he in someone's lineup?"

It is **read-only**: it never makes roster moves, claims, trades or lineup changes. You stay
in charge in Fantrax; the server just gives Claude accurate numbers to reason with.

> Unofficial project. It uses the same internal web API the Fantrax site uses, with your own
> logged-in browser session. Fantrax can change that API at any time, so expect occasional
> breakage. Use it for your own league and in line with Fantrax's terms.

---

## What it can do

| Tool | What you get |
|---|---|
| `league_info` | Teams, your team, roster rules, the week calendar (incl. double weeks) |
| `get_league_standings` | Standings with division, division rank, W-L-T, points, win %, games back, category points |
| `get_matchup(week, team)` | One H2H matchup: totals for both sides, who wins each category, Fantrax's projection |
| `get_league_matchups(week)` | Every pairing for a week, plus bye teams |
| `get_league_schedule` | All periods: pairings, byes, playoff rounds |
| `get_roster(team)` | A roster with positions, NHL team, status, injury notes and stats |
| `get_league_rosters(timeframes)` | Every roster in one call, with raw season totals per timeframe |
| `get_free_agents(position, ...)` | Available players, sortable by any category |
| `get_goalie_stats(team)` | Goalie W / GAA / SV / SV% plus goals against, shots against and minutes |
| `get_daily_player_stats(date)` | Everyone who played on a date: owner, lineup slot that day, raw stats |
| `get_nhl_schedule(start, end)` | NHL games per night and per team |
| `get_fantasy_week(week)` | Week dates, games per NHL team, light nights |
| `lineup_capacity(week, team)` | Daily lineup simulation: usable starts, games wasted on the bench, open slots |
| `league_lineup_capacity(week)` | The same for every team at once, plus goalie games played so far |
| `lineup_plan(week)` | Who fills which slot each day, for every team |
| `evaluate_add_drop(add, drop)` | Net usable starts from a swap, with both players' stats |
| `goalie_check(week, team)` | Projected goalie starts against the weekly minimum |
| `session_health` | Is the Fantrax login still valid, when does the cookie expire |
| `fantrax_raw(method, data)` | Debug passthrough to an allow-listed set of read-only Fantrax calls |

Anywhere a tool takes `team`, you can pass `me`, a team name (or part of it), a Fantrax team
id, or a short team code from `TEAM_CODES` (see below). Timeframes are `PROJ_SEASON`, `YTD` and `LAST_SEASON`. Every
response includes `fetched_at` (UTC) and `nhl_date` (the US-Eastern game date). Bad input,
such as an unknown team, comes back as `{"error": "..."}` with a readable message.

---

## Is it a fit for my league?

It was built for, and tested on, one league. Before you set it up, check these:

| Works out of the box | Needs a small code edit today |
|---|---|
| Fantrax **NHL** leagues with **head-to-head categories** scoring | Other sports or points-based scoring are not supported |
| Any number of teams, any league id | **Scoring categories** are pinned in `fantrax_mcp/categories.py` (skaters G, A, PIM, SOG, PPG, PPA, Hit, Blk, Tk, FOW, TOI; goalies W, GAA, SV, SV%). With a different set, `session_health` reports a category mismatch until you edit that file |
| Roster size and IR slots (`ROSTER_SIZE`, `IR_SLOTS`) | **Daily lineup slots** (3 C, 3 LW, 3 RW, 6 D, 2 G) and the 3-game **goalie minimum** live in `fantrax_mcp/config.py` |
| Season calendar read from Fantrax | **Team codes**: `TEAM_CODES` in `categories.py` maps the author's league. For your league, fill in your own team ids and codes (`league_info` lists the ids), or empty it: outputs then show Fantrax's own short names, and you look teams up by name or id |

Making those settings configurable is on the to-do list; pull requests are welcome.

---

## Requirements

- A Linux machine that stays on (a small VM, LXC container or Raspberry Pi is plenty)
- Python 3.11 or newer
- Your Fantrax account, logged in through a browser
- For Claude Desktop: Node.js (it runs the `mcp-remote` bridge via `npx`)

---

## Quick start

### 1. Install

```bash
sudo apt install -y python3-venv git
sudo useradd -r -s /usr/sbin/nologin fantrax
sudo git clone https://github.com/RakkenRoli/fantrax-mcp.git /opt/fantrax-mcp
cd /opt/fantrax-mcp
sudo python3 -m venv .venv
sudo .venv/bin/pip install .
```

### 2. Configure

```bash
sudo mkdir -p /etc/fantrax-mcp
sudo cp .env.example /etc/fantrax-mcp/env
sudo nano /etc/fantrax-mcp/env
```

At minimum set:

- `FANTRAX_LEAGUE_ID`: the id in your league's URL, `fantrax.com/fantasy/league/<this part>/...`
- `FANTRAX_TEAM_NAME`: your team's name exactly as Fantrax shows it. Put it in quotes if it
  contains spaces. Or set `FANTRAX_TEAM_ID` instead (the `teamId=` value on your roster page).
- `MCP_AUTH_TOKEN`: a long random secret. Generate one with `openssl rand -hex 32`.

All settings are listed under [Configuration](#configuration) below.

### 3. Give it your Fantrax login

Fantrax has no public API, so the server reuses your browser session:

1. Log in at fantrax.com with "Remember me" ticked.
2. Install the **Cookie-Editor** browser extension, open it on fantrax.com, and export the
   cookies as **JSON**.
3. Save the export as `/etc/fantrax-mcp/fantrax_cookies.json`.

```bash
sudo chown -R root:fantrax /etc/fantrax-mcp
sudo chmod 750 /etc/fantrax-mcp && sudo chmod 640 /etc/fantrax-mcp/*
```

Treat that file like a password: anyone who has it is logged in to your Fantrax account.
Never commit it or paste it anywhere.

### 4. Run it as a service

```bash
sudo cp deploy/fantrax-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fantrax-mcp
sudo journalctl -u fantrax-mcp -n 20 --no-pager
```

Check it from the same machine:

```bash
curl -s -H "Authorization: Bearer <your token>" http://127.0.0.1:8765/healthz
```

`200` with `"ok": true` means it is logged in and the scoring categories match.

### 5. Put HTTPS in front of it

MCP clients should reach the server over HTTPS. Any reverse proxy works; Caddy is the
simplest. `deploy/Caddyfile.snippet` is a starting point; keep `flush_interval -1`, because
MCP streams responses. If you use Caddy's `tls internal` certificates, the machine running
Claude must trust Caddy's root certificate (see [Troubleshooting](#troubleshooting)).

Add the hostname you use to `MCP_ALLOWED_HOSTS`, then restart the service.

### 6. Connect Claude Desktop

Merge `deploy/claude_desktop_config.snippet.json` into your Claude Desktop config and
replace the URL and token:

```json
{
  "mcpServers": {
    "fantrax": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://fantrax.example.lan/mcp",
               "--header", "Authorization:${FANTRAX_MCP_AUTH}"],
      "env": {
        "FANTRAX_MCP_AUTH": "Bearer <your token>",
        "NODE_EXTRA_CA_CERTS": "/path/to/caddy-root.crt"
      }
    }
  }
}
```

The token is passed through an environment variable because `mcp-remote` arguments must
not contain spaces. Drop `NODE_EXTRA_CA_CERTS` if your certificate comes from a public CA.

Restart Claude Desktop and ask: *"Use the fantrax tools: how is my matchup this week?"*

Other MCP clients work the same way: point them at `https://<your-host>/mcp` and send
`Authorization: Bearer <token>`.

---

## Configuration

Settings are environment variables, read from `/etc/fantrax-mcp/env` by the service.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `FANTRAX_LEAGUE_ID` | yes | | League id from the Fantrax URL |
| `FANTRAX_TEAM_NAME` | one of these two | | Your team's name (quote it if it has spaces) |
| `FANTRAX_TEAM_ID` | | | Your team id; skips the name lookup |
| `FANTRAX_COOKIE_FILE` | | `fantrax_cookies.json` | Cookie-Editor JSON export, a `{"name": "value"}` dict, or a raw `Cookie:` header |
| `MCP_AUTH_TOKEN` | strongly recommended | | Shared secret every request must send. Without it the server is open to anyone who can reach it |
| `MCP_HOST` / `MCP_PORT` | | `0.0.0.0` / `8765` | Where the server listens |
| `MCP_ALLOWED_HOSTS` | | `127.0.0.1:*,localhost:*` | Host names the server accepts (DNS-rebinding protection) |
| `ROSTER_SIZE` / `IR_SLOTS` | | `23` / `6` | Roster cap (IR not counted) and IR slots |
| `SEASON_FIRST_DAY` / `SEASON_LAST_DAY` / `N_WEEKS` | | 2026-27 season | Fallback calendar, used only if Fantrax's own calendar can't be read |
| `NHL_SEASON` | | `20262027` | NHL season for schedule lookups |

---

## Keeping it running

- **The Fantrax cookie expires** after a few months, or when you log out of Fantrax in that
  browser. `session_health` shows the expiry date and warns three days ahead. To renew,
  export the cookies again and overwrite the file. The server picks up the new file on
  the next call, so no restart is needed.
- **Updating:**
  ```bash
  cd /opt/fantrax-mcp && sudo git pull --ff-only && sudo .venv/bin/pip install -q . \
    && sudo systemctl restart fantrax-mcp
  ```
- **Using it from scripts:** `/healthz` returns 200 when the login works and the category
  map matches, 503 when not, 401 without the token. A good gate before any scheduled job:
  ```bash
  curl -fsS -H "Authorization: Bearer $MCP_AUTH_TOKEN" https://<host>/healthz >/dev/null \
    || { echo "Fantrax session unhealthy" >&2; exit 1; }
  ```

---

## How the numbers are produced

- **Matchup totals** come from Fantrax's standings table, the same numbers Fantrax scores
  on. Live scoring is used only for Fantrax's own projection.
- **Lineup simulations** solve each day's lineup exactly (players with multiple positions
  included) against the real NHL schedule, so "usable starts" means games that fit in an
  active slot, not just games played by the NHL team.
- **Goalie rates** (GAA, SV%) are recomputed from goals against, shots against and minutes
  when goalies are combined. Averaging per-game rates gives the wrong answer.
- **Fantrax does not project takeaways (Tk) or time on ice (TOI).** In season projections
  these are reported as missing (`null`), not as zero.

---

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e . pytest
.venv/bin/python -m pytest -q tests
```

Tests never touch your real login: `tests/conftest.py` forces a dummy cookie file, even if
your service environment is loaded in the shell. Several fixtures are real Fantrax
responses from the author's league; if you change the category or team mappings for your
league, a few of those tests will need their expectations updated.

`fantrax_raw` is the quickest way to inspect a response shape when Fantrax changes
something. `scripts/capture_standings_fixtures.py` saves fresh standings fixtures.

Changes to tool output are recorded in [CHANGELOG.md](CHANGELOG.md).

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `401 unauthorized` | The token in the client config doesn't match `MCP_AUTH_TOKEN` |
| `421 Invalid Host header` | Add the hostname your proxy forwards to `MCP_ALLOWED_HOSTS` |
| `mcp-remote` TLS / certificate error | `NODE_EXTRA_CA_CERTS` is missing or points at the wrong root certificate |
| Login errors, `session_health` says not logged in | Export the Fantrax cookies again (step 3) |
| `session_health` reports a category mismatch | Your league scores different categories; see [Is it a fit for my league?](#is-it-a-fit-for-my-league) |
| Claude Desktop keeps an old connection | `rm -rf ~/.mcp-auth`, then restart Claude Desktop |
| Service won't start | `journalctl -u fantrax-mcp -n 50`; a missing `FANTRAX_LEAGUE_ID` is the usual cause |

---

## License

MIT, see [LICENSE](LICENSE). Not affiliated with or endorsed by Fantrax or the NHL.
