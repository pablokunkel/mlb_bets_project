#!/usr/bin/env python3
"""
top25_stats.py — Show the stats of the top N batters from today's big board.

Reads the picks JSON that generate_picks.py wrote, takes the top N by
composite, and pulls each batter's current-season stats. Prefers the
local SQLite season_batting table (fast, no API); falls back to the MLB
Stats API by name search if the DB isn't populated.

Usage:
    python top25_stats.py                     # top 25 from today's board
    python top25_stats.py --date 2026-04-10   # specific date
    python top25_stats.py --top 50            # top 50 instead of 25
    python top25_stats.py --file path.json    # custom picks file
    python top25_stats.py --csv               # CSV output instead of table
"""

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import requests

MLB_API = "https://statsapi.mlb.com/api/v1"
PROJECT_DIR = Path(__file__).parent
RESULTS_DIR = PROJECT_DIR.parent / "results"
DB_PATH = PROJECT_DIR.parent / "data" / "hr_bets.db"


# ---------------------------------------------------------------------------
# Load the big board
# ---------------------------------------------------------------------------

def load_picks_file(date_str: str, file_override: str | None) -> dict:
    """Load the picks JSON for a given date (or a custom path)."""
    if file_override:
        path = Path(file_override)
    else:
        path = RESULTS_DIR / f"picks_{date_str}.json"

    if not path.exists():
        # Also try the "data/results" variant if the user ran with a different layout
        alt = PROJECT_DIR / "results" / f"picks_{date_str}.json"
        if alt.exists():
            path = alt
        else:
            print(f"  ERROR: Couldn't find picks file at {path}")
            print(f"  Try: python generate_picks.py --date {date_str}")
            sys.exit(1)

    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Stat lookup — DB first, API fallback
# ---------------------------------------------------------------------------

def lookup_in_db(conn: sqlite3.Connection, name: str, season: int) -> dict | None:
    """Look up a batter's season stats from season_batting by name."""
    # Try exact name match first
    row = conn.execute(
        "SELECT player_name, team, bats, games, pa, ab, hr, avg, slg, obp, iso, "
        "       woba, barrel_pct, exit_velo, hr_fb_pct "
        "FROM season_batting WHERE season=? AND player_name=?",
        (season, name),
    ).fetchone()

    if row:
        return dict(row)

    # Fallback: try case-insensitive / last-name match
    row = conn.execute(
        "SELECT player_name, team, bats, games, pa, ab, hr, avg, slg, obp, iso, "
        "       woba, barrel_pct, exit_velo, hr_fb_pct "
        "FROM season_batting WHERE season=? AND LOWER(player_name)=LOWER(?)",
        (season, name),
    ).fetchone()

    return dict(row) if row else None


def lookup_via_api(name: str, season: int) -> dict | None:
    """Look up a batter's stats via MLB Stats API (by name → id → stats)."""
    try:
        # Step 1: search for the player
        search_url = f"{MLB_API}/people/search"
        r = requests.get(search_url, params={"names": name}, timeout=8)
        r.raise_for_status()
        people = r.json().get("people", [])
        if not people:
            return None

        # Prefer an active player; otherwise take the first match
        person = next((p for p in people if p.get("active")), people[0])
        player_id = person["id"]

        # Step 2: pull season stats
        stats_url = f"{MLB_API}/people/{player_id}/stats"
        r = requests.get(
            stats_url,
            params={"stats": "season", "season": season, "group": "hitting"},
            timeout=8,
        )
        r.raise_for_status()
        stats_list = r.json().get("stats", [])
        if not stats_list:
            return None

        splits = stats_list[0].get("splits", [])
        if not splits:
            return None

        s = splits[0].get("stat", {})
        avg = float(s.get("avg", "0") or 0)
        slg = float(s.get("slg", "0") or 0)
        obp = float(s.get("obp", "0") or 0)

        return {
            "player_name": person.get("fullName", name),
            "team": person.get("currentTeam", {}).get("abbreviation", ""),
            "bats": person.get("batSide", {}).get("code", ""),
            "games": s.get("gamesPlayed", 0),
            "pa": s.get("plateAppearances", 0),
            "ab": s.get("atBats", 0),
            "hr": s.get("homeRuns", 0),
            "avg": avg,
            "slg": slg,
            "obp": obp,
            "iso": round(slg - avg, 3),
            "woba": None,
            "barrel_pct": None,
            "exit_velo": None,
            "hr_fb_pct": None,
        }
    except Exception:
        return None


def enrich_batters(batters: list[dict], season: int) -> list[dict]:
    """For each batter on the big board, attach season stats."""
    conn = None
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
        except Exception:
            conn = None

    db_hits, api_hits, misses = 0, 0, 0
    enriched = []
    for b in batters:
        name = b.get("name", "")
        stats = None

        if conn is not None:
            stats = lookup_in_db(conn, name, season)
            if stats is not None:
                db_hits += 1

        if stats is None:
            stats = lookup_via_api(name, season)
            if stats is not None:
                api_hits += 1

        if stats is None:
            misses += 1

        enriched.append({**b, "stats": stats or {}})

    if conn is not None:
        conn.close()

    print(f"  Lookups: {db_hits} from DB, {api_hits} from API, {misses} missing")
    return enriched


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_table(rows: list[dict], date_str: str):
    """Render a readable table of the top N with stats."""
    print()
    print(f"  TOP {len(rows)} FROM BIG BOARD — {date_str}")
    print("  " + "=" * 116)

    header = (
        f"  {'#':>2}  {'Name':<22} {'Tm':<4} {'B':<1}  {'Tier':<3}  "
        f"{'Comp':>5}  {'G':>3} {'PA':>4} {'HR':>3}  "
        f"{'AVG':>5} {'OBP':>5} {'SLG':>5} {'ISO':>5}  "
        f"{'Brl%':>5} {'EV':>5}  {'Venue':<18}"
    )
    print(header)
    print("  " + "-" * 116)

    for i, r in enumerate(rows, 1):
        s = r.get("stats", {}) or {}
        name = (r.get("name", "") or "")[:22]
        team = (r.get("team", "") or s.get("team", "") or "")[:4]
        bats = (s.get("bats", "") or "")[:1]
        tier = r.get("tier", "")
        comp = r.get("composite", 0)
        venue = (r.get("venue", "") or "")[:18]

        games = s.get("games") or 0
        pa = s.get("pa") or 0
        hr = s.get("hr") or 0
        avg = s.get("avg")
        obp = s.get("obp")
        slg = s.get("slg")
        iso = s.get("iso")
        barrel = s.get("barrel_pct")
        ev = s.get("exit_velo")

        def fmt3(v):
            return f"{v:.3f}".lstrip("0") if v is not None else "  -- "

        def fmt1(v):
            return f"{v:>5.1f}" if v is not None else "   --"

        print(
            f"  {i:>2}  {name:<22} {team:<4} {bats:<1}  T{tier:<2}  "
            f"{comp:>5.1f}  {games:>3} {pa:>4} {hr:>3}  "
            f"{fmt3(avg):>5} {fmt3(obp):>5} {fmt3(slg):>5} {fmt3(iso):>5}  "
            f"{fmt1(barrel):>5} {fmt1(ev):>5}  {venue:<18}"
        )

    print("  " + "=" * 116)
    print()


def write_csv(rows: list[dict], path: str):
    fieldnames = [
        "rank", "name", "team", "bats", "tier", "composite", "venue", "opp_pitcher",
        "games", "pa", "ab", "hr", "avg", "obp", "slg", "iso", "woba",
        "barrel_pct", "exit_velo", "hr_fb_pct",
        "power_score", "matchup_score", "park_score", "form_score", "weather_score",
        "selected",
    ]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for i, r in enumerate(rows, 1):
            s = r.get("stats", {}) or {}
            writer.writerow({
                "rank": i,
                "name": r.get("name", ""),
                "team": r.get("team", "") or s.get("team", ""),
                "bats": s.get("bats", ""),
                "tier": r.get("tier", ""),
                "composite": r.get("composite", 0),
                "venue": r.get("venue", ""),
                "opp_pitcher": r.get("opp_pitcher", ""),
                "games": s.get("games", ""),
                "pa": s.get("pa", ""),
                "ab": s.get("ab", ""),
                "hr": s.get("hr", ""),
                "avg": s.get("avg", ""),
                "obp": s.get("obp", ""),
                "slg": s.get("slg", ""),
                "iso": s.get("iso", ""),
                "woba": s.get("woba", ""),
                "barrel_pct": s.get("barrel_pct", ""),
                "exit_velo": s.get("exit_velo", ""),
                "hr_fb_pct": s.get("hr_fb_pct", ""),
                "power_score": r.get("power_score", ""),
                "matchup_score": r.get("matchup_score", ""),
                "park_score": r.get("park_score", ""),
                "form_score": r.get("form_score", ""),
                "weather_score": r.get("weather_score", ""),
                "selected": r.get("selected", False),
            })
    print(f"  CSV written to {path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Show top N big-board batters with season stats")
    parser.add_argument("--date", default="today", help="Date (YYYY-MM-DD or 'today')")
    parser.add_argument("--top", type=int, default=25, help="How many to show (default 25)")
    parser.add_argument("--file", default=None, help="Custom picks JSON path")
    parser.add_argument("--csv", default=None, help="Write CSV to this path instead of printing")
    args = parser.parse_args()

    date_str = datetime.now().strftime("%Y-%m-%d") if args.date == "today" else args.date
    season = int(date_str[:4])

    print(f"\n  Loading picks file for {date_str}...")
    data = load_picks_file(date_str, args.file)

    board = data.get("full_board", [])
    if not board:
        print("  ERROR: full_board is empty in the picks file")
        sys.exit(1)

    # Sort defensively by composite in case upstream ordering changed
    board.sort(key=lambda b: b.get("composite", 0), reverse=True)
    top = board[: args.top]

    print(f"  Loaded {len(board)} batters, taking top {len(top)}")
    print(f"  Enriching with season stats...")
    enriched = enrich_batters(top, season)

    if args.csv:
        write_csv(enriched, args.csv)
    else:
        print_table(enriched, date_str)


if __name__ == "__main__":
    main()
