#!/usr/bin/env python3
"""
hr_tracker.py — Pull every home run hit on a given MLB date.

Uses the MLB Stats API (free, no auth) to fetch box scores for all
completed games and extract HR details per player.

Optionally cross-references against your daily picks to show hits/misses.

Usage:
    # Today's HRs
    python hr_tracker.py

    # Specific date
    python hr_tracker.py --date 2026-04-07

    # Compare against your picks
    python hr_tracker.py --date 2026-04-07 --picks

    # JSON output
    python hr_tracker.py --date 2026-04-07 --json
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import requests

MLB_API = "https://statsapi.mlb.com/api/v1"


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def get_schedule(date_str: str) -> list[dict]:
    """Get all games for a date."""
    url = f"{MLB_API}/schedule"
    params = {
        "sportId": 1,
        "date": date_str,
        "hydrate": "team,venue,linescore",
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    games = []
    for date_entry in resp.json().get("dates", []):
        for g in date_entry.get("games", []):
            games.append({
                "game_pk": g["gamePk"],
                "status": g["status"]["detailedState"],
                "home_team": g["teams"]["home"]["team"]["name"],
                "away_team": g["teams"]["away"]["team"]["name"],
                "home_score": g.get("linescore", {}).get("teams", {}).get("home", {}).get("runs"),
                "away_score": g.get("linescore", {}).get("teams", {}).get("away", {}).get("runs"),
                "venue": g.get("venue", {}).get("name", ""),
                "inning": g.get("linescore", {}).get("currentInning"),
                "inning_state": g.get("linescore", {}).get("inningHalf", ""),
            })
    return games


def get_boxscore(game_pk: int) -> dict:
    """Fetch full boxscore for a game."""
    url = f"{MLB_API}/game/{game_pk}/boxscore"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()


def extract_hr_hitters(boxscore: dict, game_info: dict) -> list[dict]:
    """
    Extract every player who hit a HR from a boxscore.
    Returns list of dicts with player info and HR count.
    """
    hr_hitters = []

    for side in ["home", "away"]:
        team_data = boxscore.get("teams", {}).get(side, {})
        team_name = game_info[f"{side}_team"]
        players = team_data.get("players", {})

        for player_key, player_data in players.items():
            stats = player_data.get("stats", {})
            batting = stats.get("batting", {})
            hr_count = batting.get("homeRuns", 0)

            if hr_count > 0:
                person = player_data.get("person", {})
                hr_hitters.append({
                    "name": person.get("fullName", "Unknown"),
                    "player_id": person.get("id", 0),
                    "team": team_name,
                    "hr": hr_count,
                    "ab": batting.get("atBats", 0),
                    "hits": batting.get("hits", 0),
                    "rbi": batting.get("rbi", 0),
                    "game_pk": game_info["game_pk"],
                    "opponent": game_info["home_team"] if side == "away" else game_info["away_team"],
                    "venue": game_info["venue"],
                })

    return hr_hitters


def get_live_batting(game_pk: int, game_info: dict) -> list[dict]:
    """
    For in-progress games, pull current batting lines from the live feed.
    Returns list of dicts for players who have HR > 0.
    """
    url = f"{MLB_API}.1/game/{game_pk}/feed/live"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []

    hr_hitters = []
    boxscore = data.get("liveData", {}).get("boxscore", {})
    return extract_hr_hitters(boxscore, game_info)


# ---------------------------------------------------------------------------
# Picks cross-reference
# ---------------------------------------------------------------------------

def load_picks(date_str: str) -> list[dict]:
    """Load picks from the results directory."""
    results_dir = Path(__file__).parent.parent / "results"
    fp = results_dir / f"picks_{date_str}.json"
    if not fp.exists():
        return []
    try:
        with open(fp) as f:
            data = json.load(f)
        return data.get("picks", [])
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def format_game_status(game: dict) -> str:
    """Human-readable game status."""
    status = game["status"]
    if status == "Final":
        return f"Final: {game['away_score']}-{game['home_score']}"
    elif status in ("In Progress", "Manager challenge"):
        half = "Top" if game["inning_state"] == "Top" else "Bot"
        return f"{half} {game['inning']}: {game['away_score']}-{game['home_score']}"
    elif status in ("Pre-Game", "Scheduled", "Warmup"):
        return "Not started"
    elif status == "Game Over":
        return f"Game Over: {game['away_score']}-{game['home_score']}"
    else:
        return status


def print_report(date_str: str, games: list, all_hrs: list, picks: list = None):
    """Print the HR tracker report."""
    total_games = len(games)
    final_games = sum(1 for g in games if g["status"] in ("Final", "Game Over"))
    in_progress = sum(1 for g in games if g["status"] in ("In Progress", "Manager challenge"))
    not_started = total_games - final_games - in_progress

    print()
    print("=" * 78)
    print(f"  HOME RUN TRACKER — {date_str}")
    print(f"  Games: {final_games} final, {in_progress} in progress, {not_started} not started")
    print("=" * 78)

    if not all_hrs:
        if final_games == 0 and in_progress == 0:
            print("\n  No games completed or in progress yet.\n")
        else:
            print("\n  No home runs recorded yet.\n")
    else:
        # Sort by HR count descending, then name
        all_hrs.sort(key=lambda x: (-x["hr"], x["name"]))

        # Build pick name set for marking
        pick_names = set()
        if picks:
            pick_names = {p.get("name", "") for p in picks}

        print()
        print(f"  {'':>3} {'Player':<24} {'Team':<22} {'HR':>3} {'AB':>4} "
              f"{'H':>3} {'RBI':>4}  {'vs':>0}")
        print(f"  {'-' * 76}")

        total_hr = 0
        for i, h in enumerate(all_hrs, 1):
            total_hr += h["hr"]
            marker = " *" if h["name"] in pick_names else "  "
            multi = f" ({h['hr']})" if h["hr"] > 1 else ""
            print(f"  {i:>3} {h['name']:<24} {h['team']:<22} {h['hr']:>3} {h['ab']:>4} "
                  f"{h['hits']:>3} {h['rbi']:>4}  vs {h['opponent']}{marker}")

        print(f"  {'-' * 76}")
        print(f"  Total: {total_hr} HR by {len(all_hrs)} players")

    # Cross-reference with picks
    if picks:
        hr_names = {h["name"] for h in all_hrs}
        print()
        print("  PICK RESULTS")
        print("  " + "-" * 50)
        hits = 0
        for p in picks:
            name = p.get("name", "")
            went_yard = name in hr_names
            if went_yard:
                hits += 1
            icon = "HR" if went_yard else "--"
            hr_detail = ""
            if went_yard:
                match = [h for h in all_hrs if h["name"] == name]
                if match:
                    hr_detail = f" ({match[0]['hr']} HR, {match[0]['rbi']} RBI)"
            status_note = ""
            if not any(g["status"] in ("Final", "Game Over", "In Progress", "Manager challenge")
                       for g in games):
                status_note = " (not started)"
            print(f"    [{icon}]  {name:<24} {p.get('team', ''):<5} "
                  f"vs {p.get('opp_pitcher', 'TBD'):<18}{hr_detail}{status_note}")

        total_picks = len(picks)
        if final_games > 0 or in_progress > 0:
            print(f"\n  Hit rate: {hits}/{total_picks} ({100*hits/total_picks:.0f}%)")
            if in_progress > 0:
                print(f"  ({in_progress} game(s) still in progress)")
        print()

    # Game scoreboard
    print()
    print("  SCOREBOARD")
    print("  " + "-" * 50)
    for g in games:
        status = format_game_status(g)
        print(f"    {g['away_team']:<22} @ {g['home_team']:<22} {status}")
    print("=" * 78)
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_tracker(date_str: str, show_picks: bool = False, as_json: bool = False):
    """Main tracker logic."""
    print(f"\n  Fetching MLB schedule for {date_str}...")
    games = get_schedule(date_str)

    if not games:
        print(f"  No games found for {date_str}")
        return

    print(f"  Found {len(games)} games. Fetching box scores...")

    all_hrs = []
    for g in games:
        status = g["status"]
        if status in ("Final", "Game Over"):
            # Completed game — full boxscore available
            try:
                box = get_boxscore(g["game_pk"])
                hrs = extract_hr_hitters(box, g)
                all_hrs.extend(hrs)
            except Exception as e:
                print(f"  Warning: Could not fetch boxscore for {g['game_pk']}: {e}")

        elif status in ("In Progress", "Manager challenge"):
            # Live game — use live feed
            try:
                hrs = get_live_batting(g["game_pk"], g)
                all_hrs.extend(hrs)
            except Exception as e:
                print(f"  Warning: Could not fetch live data for {g['game_pk']}: {e}")

    # Load picks if requested
    picks = load_picks(date_str) if show_picks else None

    if as_json:
        output = {
            "date": date_str,
            "games": games,
            "home_runs": all_hrs,
            "total_hr": sum(h["hr"] for h in all_hrs),
            "total_players": len(all_hrs),
        }
        if picks:
            hr_names = {h["name"] for h in all_hrs}
            output["picks"] = [
                {**p, "hit": p.get("name", "") in hr_names}
                for p in picks
            ]
            output["pick_hits"] = sum(1 for p in picks if p.get("name", "") in hr_names)
        print(json.dumps(output, indent=2))
    else:
        print_report(date_str, games, all_hrs, picks)


def main():
    parser = argparse.ArgumentParser(description="Track MLB home runs for a date")
    parser.add_argument("--date", default="today", help="Date (YYYY-MM-DD or 'today')")
    parser.add_argument("--picks", action="store_true", help="Cross-reference against your daily picks")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    if args.date == "today":
        date_str = datetime.now().strftime("%Y-%m-%d")
    else:
        date_str = args.date

    run_tracker(date_str, show_picks=args.picks, as_json=args.json)


if __name__ == "__main__":
    main()
