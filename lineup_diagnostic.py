#!/usr/bin/env python3
"""
Lineup diagnostic — shows exactly what the model sees for today's lineup filter.

Compares confirmed lineups (bdfed) against the tier pool to find:
  - Batters confirmed in lineup (✓ IN LINEUP)
  - Batters on active roster but no lineup posted (⚠ ROSTER-ONLY — double check!)
  - Batters blocked by the filter (✗ NOT IN LINEUP)
  - Batters whose team isn't playing (— NO GAME)

Usage:
    python lineup_diagnostic.py                # today
    python lineup_diagnostic.py --date 2026-04-16
"""

import argparse
import sys
from pathlib import Path
from datetime import datetime

import requests

# Add project root to path (works from inside the project folder)
PROJECT = Path(__file__).parent
sys.path.insert(0, str(PROJECT))

from mlb_2025_tiers import ALL_TIERS, get_all_batters_lookup
from fetch_daily_data import (
    get_schedule, get_lineup, get_roster, build_live_tiers, VENUE_COORDS,
)

MLB_API = "https://statsapi.mlb.com/api/v1"

# Venue aliases (from generate_picks.py)
VENUE_ALIASES = {
    "UNIQLO Field at Dodger Stadium": "Dodger Stadium",
    "Uniqlo Field at Dodger Stadium": "Dodger Stadium",
    "Rate Field": "Guaranteed Rate Field",
    "Daikin Park": "Minute Maid Park",
}

def normalize_venue(raw: str) -> str:
    if raw in VENUE_ALIASES:
        return VENUE_ALIASES[raw]
    for canon in VENUE_COORDS:
        if canon.lower() in raw.lower() or raw.lower() in canon.lower():
            return canon
    return raw

# Team name mappings
TEAM_ABBREV_TO_FULL = {
    "ARI": "Arizona Diamondbacks", "ATL": "Atlanta Braves",
    "BAL": "Baltimore Orioles", "BOS": "Boston Red Sox",
    "CHC": "Chicago Cubs", "CWS": "Chicago White Sox",
    "CIN": "Cincinnati Reds", "CLE": "Cleveland Guardians",
    "COL": "Colorado Rockies", "DET": "Detroit Tigers",
    "HOU": "Houston Astros", "KC": "Kansas City Royals",
    "LAA": "Los Angeles Angels", "LAD": "Los Angeles Dodgers",
    "MIA": "Miami Marlins", "MIL": "Milwaukee Brewers",
    "MIN": "Minnesota Twins", "NYM": "New York Mets",
    "NYY": "New York Yankees", "OAK": "Oakland Athletics",
    "PHI": "Philadelphia Phillies", "PIT": "Pittsburgh Pirates",
    "SD": "San Diego Padres", "SF": "San Francisco Giants",
    "SEA": "Seattle Mariners", "STL": "St. Louis Cardinals",
    "TB": "Tampa Bay Rays", "TEX": "Texas Rangers",
    "TOR": "Toronto Blue Jays", "WSH": "Washington Nationals",
}
TEAM_FULL_TO_ABBREV = {v: k for k, v in TEAM_ABBREV_TO_FULL.items()}

EXCLUDED_PLAYERS = {
    "Anthony Santander",
    "Eli White",
}


def fetch_active_roster(team_id: int, date_str: str) -> dict:
    """
    Fetch active roster from MLB Stats API.
    Returns {"ids": set of player_ids, "names": set of lowercase last names}
    """
    try:
        roster = get_roster(team_id, date_str)
        ids = {p["player_id"] for p in roster if p.get("player_id")}
        names = set()
        for p in roster:
            full = p.get("name", "")
            if full:
                names.add(full.split()[-1].lower().strip())
        return {"ids": ids, "names": names, "full": roster}
    except Exception as e:
        print(f"    [ROSTER] Failed for team {team_id}: {e}")
        return {"ids": set(), "names": set(), "full": []}


def run_diagnostic(date_str: str):
    print(f"\n{'='*70}")
    print(f"  LINEUP DIAGNOSTIC — {date_str}")
    print(f"{'='*70}")

    # ── Step 1: Fetch schedule ───────────────────────────────────────
    print(f"\n  [1] Fetching schedule...")
    games = get_schedule(date_str)
    if not games:
        print("  No games found. Exiting.")
        return

    for g in games:
        g["venue"] = normalize_venue(g["venue"])

    print(f"  Found {len(games)} games\n")

    # ── Step 2: Fetch lineups + rosters ──────────────────────────────
    print(f"  [2] Fetching lineups (bdfed) and active rosters (MLB API)...")
    lineups = {}
    rosters = {}  # team_id -> roster dict
    lineup_summary = []

    for g in games:
        gpk = g["game_pk"]
        away = g["away_team"]
        home = g["home_team"]
        away_abbrev = TEAM_FULL_TO_ABBREV.get(away, "???")
        home_abbrev = TEAM_FULL_TO_ABBREV.get(home, "???")

        # Fetch lineup from bdfed
        lu = get_lineup(gpk)
        lineups[gpk] = lu

        h_count = len(lu["home"])
        a_count = len(lu["away"])
        has_lineup = h_count > 0 or a_count > 0

        # Fetch active rosters for teams we haven't seen yet
        for side, team_name in [("away", away), ("home", home)]:
            team_id = g.get(f"{side}_team_id")
            if team_id and team_id not in rosters:
                rosters[team_id] = fetch_active_roster(team_id, date_str)

        hp = g.get("home_pitcher_name", "TBD")
        ap = g.get("away_pitcher_name", "TBD")

        status = "✓ LINEUPS" if has_lineup else "⚠ NO LINEUP (roster fallback)"
        lineup_summary.append({
            "gpk": gpk, "away": away_abbrev, "home": home_abbrev,
            "a_count": a_count, "h_count": h_count,
            "has_lineup": has_lineup, "hp": hp, "ap": ap,
            "home_team_id": g.get("home_team_id"),
            "away_team_id": g.get("away_team_id"),
        })

        print(f"    {away_abbrev:>3} @ {home_abbrev:<3}  gpk={gpk}  "
              f"lineup: away={a_count} home={h_count}  {status}  "
              f"({ap} vs {hp})")

    games_with_lineups = sum(1 for s in lineup_summary if s["has_lineup"])
    games_without = sum(1 for s in lineup_summary if not s["has_lineup"])
    print(f"\n  Lineup summary: {games_with_lineups} confirmed, "
          f"{games_without} roster-only fallback")
    print(f"  Rosters fetched for {len(rosters)} teams")

    # ── Step 3: Build confirmed sets ─────────────────────────────────
    confirmed_ids = {}
    confirmed_names = {}
    for gpk, lu in lineups.items():
        ids = set()
        names = set()
        for side in ["home", "away"]:
            for p in lu.get(side, []):
                if p.get("player_id"):
                    ids.add(p["player_id"])
                if p.get("name"):
                    names.add(p["name"].lower().strip())
        confirmed_ids[gpk] = ids
        confirmed_names[gpk] = names

    # Build roster sets per game (for fallback)
    game_roster_ids = {}   # gpk -> set of player_ids on active roster
    game_roster_names = {} # gpk -> set of lowercase last names on active roster
    for s in lineup_summary:
        gpk = s["gpk"]
        ids = set()
        names = set()
        for tid in [s.get("home_team_id"), s.get("away_team_id")]:
            if tid and tid in rosters:
                ids |= rosters[tid]["ids"]
                names |= rosters[tid]["names"]
        game_roster_ids[gpk] = ids
        game_roster_names[gpk] = names

    # ── Step 4: Build team-to-game mapping ───────────────────────────
    team_to_game = {}
    for g in games:
        team_to_game[g["home_team"]] = g
        team_to_game[g["away_team"]] = g
        ha = TEAM_FULL_TO_ABBREV.get(g["home_team"])
        aa = TEAM_FULL_TO_ABBREV.get(g["away_team"])
        if ha:
            team_to_game[ha] = g
        if aa:
            team_to_game[aa] = g

    # ── Step 5: Build live tiers ─────────────────────────────────────
    print(f"\n  [3] Building live tiers...")
    live_tiers = build_live_tiers(date_str)
    if live_tiers:
        for t, batters in live_tiers.items():
            label = {1: "T1-Chalk", 2: "T2-Mid", 3: "T3-Longshot"}[t]
            print(f"    {label}: {len(batters)} batters")
    else:
        print(f"    Live tiers failed — falling back to hardcoded")

    active_tiers = live_tiers or ALL_TIERS
    tier_source = "LIVE" if live_tiers else "HARDCODED"

    # ── Step 6: Run the filter ───────────────────────────────────────
    print(f"\n  [4] Running lineup filter against {tier_source} tiers...")
    print(f"{'='*70}")

    total_confirmed = 0
    total_roster_only = 0
    total_blocked = 0
    total_no_game = 0
    total_excluded = 0

    for tier in [1, 2, 3]:
        tier_label = {1: "T1-Chalk", 2: "T2-Mid", 3: "T3-Longshot"}[tier]
        tier_batters = active_tiers.get(tier, [])

        confirmed = []     # in bdfed lineup
        roster_only = []   # not in lineup but on active roster
        blocked = []       # not in lineup AND not on roster
        no_game = []
        excluded = []

        for b in tier_batters:
            name = b["name"]
            team = b.get("team", "")

            if name in EXCLUDED_PLAYERS:
                excluded.append(b)
                continue

            # Find game
            game = team_to_game.get(team)
            if not game:
                full = TEAM_ABBREV_TO_FULL.get(team)
                if full:
                    game = team_to_game.get(full)
            if not game:
                no_game.append(b)
                continue

            gpk = game["game_pk"]
            player_id = b.get("player_id", hash(name) % 1_000_000)
            last_name = name.split()[-1].lower().strip()

            # Check 1: Is batter in confirmed lineup?
            if confirmed_ids.get(gpk):
                in_lineup = (player_id in confirmed_ids[gpk] or
                             last_name in confirmed_names.get(gpk, set()))
                if in_lineup:
                    confirmed.append((b, "lineup"))
                    continue

            # Check 2: No lineup data — fall back to active roster
            # This is the ROSTER-ONLY path — flag it clearly
            if game_roster_ids.get(gpk):
                on_roster = (player_id in game_roster_ids[gpk] or
                             last_name in game_roster_names.get(gpk, set()))
                if on_roster:
                    # Determine if this game had a lineup at all
                    had_lineup = bool(confirmed_ids.get(gpk))
                    reason = "roster (no lineup posted)" if not had_lineup else "roster (not in starting lineup)"
                    roster_only.append((b, reason))
                    continue

            # Neither in lineup nor on active roster
            blocked.append(b)

        # ── Print results ────────────────────────────────────────────
        print(f"\n  {tier_label} ({len(tier_batters)} total)")
        print(f"  {'─'*66}")

        if confirmed:
            print(f"    ✓ IN LINEUP ({len(confirmed)}):")
            for b, reason in confirmed[:20]:
                print(f"      {b['name']:<24} {b.get('team',''):<5} "
                      f"id={b.get('player_id','?')}")
            if len(confirmed) > 20:
                print(f"      ... and {len(confirmed)-20} more")

        if roster_only:
            print(f"    ⚠ ROSTER-ONLY — double check these ({len(roster_only)}):")
            for b, reason in roster_only[:15]:
                print(f"      {b['name']:<24} {b.get('team',''):<5} "
                      f"id={b.get('player_id','?'):<8} [{reason}]")
            if len(roster_only) > 15:
                print(f"      ... and {len(roster_only)-15} more")

        if blocked:
            print(f"    ✗ BLOCKED — not on roster or lineup ({len(blocked)}):")
            for b in blocked[:10]:
                print(f"      {b['name']:<24} {b.get('team',''):<5} "
                      f"id={b.get('player_id','?')}")
            if len(blocked) > 10:
                print(f"      ... and {len(blocked)-10} more")

        if no_game:
            print(f"    — NO GAME TODAY ({len(no_game)}):")
            for b in no_game[:5]:
                print(f"      {b['name']:<24} {b.get('team',''):<5}")
            if len(no_game) > 5:
                print(f"      ... and {len(no_game)-5} more")

        if excluded:
            print(f"    ✗ EXCLUDED ({len(excluded)}):")
            for b in excluded:
                print(f"      {b['name']:<24} {b.get('team',''):<5}")

        total_confirmed += len(confirmed)
        total_roster_only += len(roster_only)
        total_blocked += len(blocked)
        total_no_game += len(no_game)
        total_excluded += len(excluded)

    # ── Summary ──────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  TOTALS:")
    print(f"    ✓ In confirmed lineup:          {total_confirmed}")
    print(f"    ⚠ Roster-only (DOUBLE CHECK):   {total_roster_only}")
    print(f"    ✗ Blocked (not roster/lineup):   {total_blocked}")
    print(f"    — No game today:                 {total_no_game}")
    print(f"    ✗ Excluded:                      {total_excluded}")
    print(f"{'='*70}")

    would_score = total_confirmed + total_roster_only
    print(f"\n  {would_score} batters would be scored total")
    if total_roster_only > 0:
        pct = total_roster_only / would_score * 100
        print(f"  ⚠ {total_roster_only} of those ({pct:.0f}%) are ROSTER-ONLY — "
              f"no confirmed lineup data.")
        print(f"  These are the candidates for inactive-player picks.")
        print(f"  The roster check catches IL/DFA'd players, but bench bats")
        print(f"  who are on the active roster will still slip through.")

    # ── Sample lineup data ───────────────────────────────────────────
    print(f"\n  [5] Sample bdfed lineup (first game with data)...")
    for s in lineup_summary:
        if s["has_lineup"]:
            gpk = s["gpk"]
            lu = lineups[gpk]
            print(f"\n  Game {gpk}: {s['away']} @ {s['home']}")
            for side in ["away", "home"]:
                print(f"    {side.upper()} lineup:")
                for i, p in enumerate(lu[side][:9], 1):
                    print(f"      {i}. {p.get('name','?'):<20} "
                          f"id={p.get('player_id','?'):<8} "
                          f"pos={p.get('position','?')}")
            break

    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Lineup filter diagnostic")
    parser.add_argument("--date", default="today", help="Date (YYYY-MM-DD or 'today')")
    args = parser.parse_args()

    if args.date == "today":
        date_str = datetime.now().strftime("%Y-%m-%d")
    else:
        date_str = args.date

    run_diagnostic(date_str)
