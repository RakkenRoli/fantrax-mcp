"""Pinned Fantrax scoring-category ids and stable team codes.

Category ids are taken from Fantrax's own getPlayerStats tableHeader (scipId + shortName),
captured 2026-10-03. Group 2010 = skaters, 2020 = goalies. Note G is 2130 and A is 2090.
`session_health` compares this table against the live header and reports any drift.
"""
from __future__ import annotations

from typing import Any

SKATER_SCIP: dict[str, str] = {
    "2010#2130#-1": "G",
    "2010#2090#-1": "A",
    "2010#2170#-1": "PIM",
    "2010#2270#-1": "SOG",
    "2010#2210#-1": "PPG",
    "2010#2200#-1": "PPA",
    "2010#2147#-1": "Hit",
    "2010#2092#-1": "Blk",
    "2010#2295#-1": "Tk",
    "2010#2096#-1": "FOW",
    "2010#2300#-1": "TOI",
}
GOALIE_SCIP: dict[str, str] = {
    "2020#231b#-1": "W",
    "2020#2320#-1": "GAA",
    "2020#2230#-1": "SV",
    "2020#2330#-1": "SV%",
}
# Not scored, but needed to rebuild GAA / SV% across games. Only returned by
# getPlayerStats with scoringCategoryType=1 ("Standard").
GOALIE_COMPONENT_SCIP: dict[str, str] = {
    "2020#2140#-1": "GA",
    "2020#2280#-1": "SA",      # Fantrax shortName "SOGA"
    "2020#2298#-1": "MIN",     # Fantrax shortName "Min", "mm:ss"
    "2020#2100#-1": "GP",
}
SCORED_SCIP: dict[str, str] = {**SKATER_SCIP, **GOALIE_SCIP}
ALL_SCIP: dict[str, str] = {**SCORED_SCIP, **GOALIE_COMPONENT_SCIP}

# Bare stat id ("2130") -> name, as used inside getLiveScoringStats maps.
# Only the scored categories: GP (2100) exists in both groups and is left out.
BARE_ID: dict[str, str] = {k.split("#")[1]: v for k, v in SCORED_SCIP.items()}

# Categories where lower wins.
LOWER_IS_BETTER = {"GAA"}

# Fixed Fantrax teamId -> code. Fantrax shortNames drift (True North = "WPG",
# RS Devils = "NT 9", Acélvárosi Pingvinek = "NT 13"), so never use them as keys.
TEAM_CODES: dict[str, str] = {
    "r27c7dt2mtr5w0js": "KWC",   # Kistarcsa Wildcocks
    "02ud8yktmtr5w0js": "HOL",   # Hamilton Outlaws
    "le98eijtmtr5w0js": "AVR",   # Avas Raiders
    "6ayw03ncmtr5w0js": "GTX",   # Gazdagréti Taxisok
    "mc5s8r57mtr5w0js": "UV",    # Utah Vultures
    "pcn509g7mtr5w0js": "DTG",   # Dropping the Gloves
    "20lpzud7mtsa2wxf": "ACP",   # Acélvárosi Pingvinek
    "wge2dwlrmtr5w0js": "OC",    # Onga Capitals
    "2qhvckkvmtr5w0jr": "TN",    # True North
    "089wx6w1mtr5w0js": "BVB",   # BVB Eishockeyverein
    "hzvy7u89mtr5w0js": "RSD",   # RS Devils
    "y5y8jp62mtr5w0js": "KJD",   # Kilian Judges
    "85j9ouszmts9qdsy": "GBH",   # Görömböly Blackhawks
}


def team_code(team_id: str | None, fallback: str | None = None) -> str | None:
    """Stable code for a Fantrax teamId. Unknown ids fall back to the Fantrax shortName,
    so a new or renamed team still shows up; add it to TEAM_CODES when that happens."""
    if team_id is None:
        return None
    return TEAM_CODES.get(team_id, fallback or team_id)


def label(key: str) -> str:
    """'2010#2130#-1' or '2130' -> 'G'; unknown keys pass through."""
    return ALL_SCIP.get(key) or BARE_ID.get(key) or key


def label_keys(obj: Any) -> Any:
    """Recursively rename dict keys that are category ids. Leaves values untouched."""
    if isinstance(obj, dict):
        return {(label(k) if isinstance(k, str) else k): label_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [label_keys(v) for v in obj]
    return obj


def check_against_live(live: dict[str, str]) -> dict[str, Any]:
    """Compare the pinned map with Fantrax's live scipId -> shortName header map."""
    short = {"SOGA": "SA", "Min": "MIN"}
    mismatched = {k: {"pinned": v, "live": live[k]} for k, v in ALL_SCIP.items()
                  if k in live and short.get(live[k], live[k]) != v}
    missing = sorted(k for k in SCORED_SCIP if k not in live)
    return {"ok": not mismatched and not missing, "mismatched": mismatched, "missing_from_live": missing}
