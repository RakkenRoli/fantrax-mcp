"""Capture real getStandings fixtures for tests/test_league_standings.py.

Run where a valid Fantrax login exists (e.g. on the LXC with the service env loaded).
Writes ONLY into --out (never next to the cookie file, never into /etc):

    set -a; . /etc/fantrax-mcp/env; set +a
    .venv/bin/python scripts/capture_standings_fixtures.py --out /tmp/fixtures
    # then copy /tmp/fixtures/*.json into tests/fixtures/ on the dev PC and commit

Responses are trimmed to the parts the parser reads (tableList, displayedLists,
fantasyTeamInfo) so fixtures stay small and contain no session data.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from pathlib import Path

from fantrax_mcp.config import Settings
from fantrax_mcp.fantrax_client import FantraxClient

KEEP = ("tableList", "displayedLists", "fantasyTeamInfo")
NON_DIVISION = {"ALL", "COMBINED", "SCHEDULE", "SEASON_STATS", "PLAYOFFS"}


def _trim(resp: dict) -> dict:
    return {k: resp[k] for k in KEEP if k in resp}


async def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    fx = FantraxClient(Settings.load())
    stamp = date.today().isoformat()
    combined = await fx.call("getStandings", view="COMBINED")
    (out / f"standings_combined_{stamp}.json").write_text(
        json.dumps(_trim(combined), ensure_ascii=False, indent=1), encoding="utf-8")
    tabs = [t for t in combined.get("displayedLists", {}).get("tabs", [])
            if t.get("id") not in NON_DIVISION]
    for tab in tabs:
        resp = await fx.call("getStandings", view=tab["id"])
        slug = tab["name"].lower().replace(" ", "_")
        (out / f"standings_division_{slug}_{stamp}.json").write_text(
            json.dumps(_trim(resp), ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {1 + len(tabs)} files to {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    asyncio.run(main(ap.parse_args().out))
