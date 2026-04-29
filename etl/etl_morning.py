#!/usr/bin/env python3
"""
etl_morning.py — Morning data pipeline for Daily HR Bet.

Runs at ~10:00 AM. Fetches today's fresh data:
  1. Schedule + probable pitchers from MLB Stats API
  2. Confirmed lineups from bdfed matchup endpoint
  3. Weather from Open-Meteo
  4. Pitcher profiles for today's starters (from DB, no API unless missing)

Takes ~15-30 seconds. After this runs, generate_picks.py can score
the full board with zero API calls.

Usage:
    python -m etl.etl_morning
    python -m etl.etl_morning --date 2026-04-08
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from etl.db import (
    get_db, create_tables,
    log_etl_start, log_etl_complete, log_etl_fail,
)

MLB_API = "https://statsapi.mlb.com/api/v1"
BDFED_API = "https://bdfed.stitch.mlbinfra.com/bdfed/matchup"

DOME_STADIUMS = {
    "Tropicana Field", "Chase Field", "American Family Field",
    "Minute Maid Park", "Globe Life Field", "Rogers Centre",
    "T-Mobile Park", "loanDepot park",
}

VENUE_COORDS = {
    "Angel Stadium": (33.8003, -117.8827),
    "Busch Stadium": (38.6226, -90.1928),
    "Chase Field": (33.4453, -112.0667),
    "Citi Field": (40.7571, -73.8458),
    "Citizens Bank Park": (39.9061, -75.1665),
    "Comerica Park": (42.3390, -83.0485),
    "Coors Field": (39.7559, -104.9942),
    "Dodger Stadium": (34.0739, -118.2400),
    "Fenway Park": (42.3467, -71.0972),
    "Globe Life Field": (32.7473, -97.0845),
    "Great American Ball Park": (39.0975, -84.5069),
    "Guaranteed Rate Field": (41.8299, -87.6338),
    "Kauffman Stadium": (39.0517, -94.4803),
    "loanDepot park": (25.7781, -80.2197),
    "Minute Maid Park": (29.7573, -95.3555),
    "Nationals Park": (38.8730, -77.0074),
    "Oakland Coliseum": (37.7516, -122.2005),
    "Oracle Park": (37.7786, -122.3893),
    "Oriole Park at Camden Yards": (39.2838, -76.6216),
    "Petco Park": (32.7076, -117.1570),
    "PNC Park": (40.4469, -80.0058),
    "Progressive Field": (41.4962, -81.6852),
    "Rogers Centre": (43.6414, -79.3894),
    "T-Mobile Park": (47.5914, -122.3325),
    "Target Field": (44.9818, -93.2775),
    "Tropicana Field": (27.7682, -82.6534),
    "Truist Park": (33.8908, -84.4678),
    "Wrigley Field": (41.9484, -87.6553),
    "Yankee Stadium": (40.8296, -73.9262),
    "American Family Field": (43.0280, -87.9712),
}

VENUE_TZ = {
    "Angel Stadium": "America/Los_Angeles",
    "Busch Stadium": "America/Chicago",
    "Chase Field": "America/Phoenix",
    "Citi Field": "America/New_York",
    "Citizens Bank Park": "America/New_York",
    "Comerica Park": "America/Detroit",
    "Coors Field": "America/Denver",
    "Dodger Stadium": "America/Los_Angeles",
    "Fenway Park": "America/New_York",
    "Globe Life Field": "America/Chicago",
    "Great American Ball Park": "America/New_York",
    "Guaranteed Rate Field": "America/Chicago",
    "Kauffman Stadium": "America/Chicago",
    "loanDepot park": "America/New_York",
    "Minute Maid Park": "America/Chicago",
    "Nationals Park": "America/New_York",
    "Oakland Coliseum": "America/Los_Angeles",
    "Oracle Park": "America/Los_Angeles",
    "Oriole Park at Camden Yards": "America/New_York",
    "Petco Park": "America/Los_Angeles",
    "PNC Park": "America/New_York",
    "Progressive Field": "America/New_York",
    "Rogers Centre": "America/Toronto",
    "T-Mobile Park": "America/Los_Angeles",
    "Target Field": "America/Chicago",
    "Tropicana Field": "America/New_York",
    "Truist Park": "America/New_York",
    "Wrigley Field": "America/Chicago",
    "Yankee Stadium": "America/New_York",
    "American Family Field": "America/Chicago",
}

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


# ---------------------------------------------------------------------------
# Step 1: Schedule + probable pitchers
# ---------------------------------------------------------------------------

def fetch_schedule(conn, date_str: str) -> list[dict]:
    """Fetch MLB schedule and write to daily_slate."""
    print("\n  [1/3] Fetching schedule + probable pitchers...")

    url = f"{MLB_API}/schedule"
    params = {
        "sportId": 1,
        "date": date_str,
        "hydrate": "team,venue,probablePitcher,linescore",
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()

    games = []
    for date_entry in resp.json().get("dates", []):
        for g in date_entry.get("games", []):
            venue_raw = g.get("venue", {}).get("name", "Unknown")
            venue = normalize_venue(venue_raw)

            hp = g["teams"]["home"].get("probablePitcher", {})
            ap = g["teams"]["away"].get("probablePitcher", {})

            game = {
                "game_pk": g["gamePk"],
                "date": date_str,
                "home_team": g["teams"]["home"]["team"]["name"],
                "away_team": g["teams"]["away"]["team"]["name"],
                "venue": venue,
                "home_pitcher_id": hp.get("id"),
                "home_pitcher": hp.get("fullName", "TBD"),
                "away_pitcher_id": ap.get("id"),
                "away_pitcher": ap.get("fullName", "TBD"),
                "game_time": g.get("gameDate", ""),
            }
            games.append(game)

    print(f"    {len(games)} games found")

    # Write to DB (weather will be added in step 3)
    for g in games:
        conn.execute("""
            INSERT OR REPLACE INTO daily_slate
            (game_pk, date, home_team, away_team, venue,
             home_pitcher_id, home_pitcher, away_pitcher_id, away_pitcher,
             game_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            g["game_pk"], g["date"], g["home_team"], g["away_team"], g["venue"],
            g["home_pitcher_id"], g["home_pitcher"],
            g["away_pitcher_id"], g["away_pitcher"],
            g["game_time"],
        ))

    conn.commit()
    print(f"  [1/3] Done. {len(games)} games written to daily_slate.")
    return games


# ---------------------------------------------------------------------------
# Step 2: Confirmed lineups
# ---------------------------------------------------------------------------

def fetch_lineups(conn, games: list[dict], date_str: str):
    """Fetch confirmed lineups from bdfed endpoint."""
    print("\n  [2/3] Fetching confirmed lineups...")

    games_with_lineups = 0
    total_players = 0

    for g in games:
        gpk = g["game_pk"]
        url = f"{BDFED_API}/{gpk}"
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            continue

        has_lineup = False
        for side in ["home", "away"]:
            side_data = data.get(side, {})
            if isinstance(side_data, list):
                players = side_data
            elif isinstance(side_data, dict):
                players = [side_data[k] for k in sorted(side_data.keys(), key=lambda x: int(x)) if side_data[k]]
            else:
                players = []

            for i, player in enumerate(players):
                if not isinstance(player, dict):
                    continue

                pid = player.get("id")
                if not pid:
                    continue

                conn.execute("""
                    INSERT OR REPLACE INTO daily_lineup
                    (game_pk, date, side, batting_order, player_id,
                     player_name, position, team)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    gpk, date_str, side, i + 1,
                    pid,
                    player.get("boxscoreName", ""),
                    player.get("primaryPosition", ""),
                    g[f"{side}_team"],
                ))
                total_players += 1
                has_lineup = True

        if has_lineup:
            games_with_lineups += 1

    conn.commit()
    print(f"  [2/3] Done. {total_players} players across {games_with_lineups}/{len(games)} games.")
    return games_with_lineups


# ---------------------------------------------------------------------------
# Step 3: Weather
# ---------------------------------------------------------------------------

def fetch_weather(conn, games: list[dict], date_str: str):
    """Fetch weather for each venue and update daily_slate."""
    print("\n  [3/3] Fetching weather...")

    ok = 0
    for g in games:
        venue = g["venue"]

        if venue in DOME_STADIUMS:
            conn.execute("""
                UPDATE daily_slate
                SET temperature_f=72, wind_mph=0, wind_dir_deg=0, dome=1
                WHERE game_pk=? AND date=?
            """, (g["game_pk"], date_str))
            ok += 1
            continue

        coords = VENUE_COORDS.get(venue)
        if not coords:
            continue

        lat, lon = coords
        tz_name = VENUE_TZ.get(venue, "America/New_York")
        game_time = g.get("game_time", "")

        try:
            # Convert UTC game time → venue-local time so the date and hour
            # we query Open-Meteo with match the local broadcast day.
            dt_utc = datetime.fromisoformat(game_time.replace("Z", "+00:00"))
            dt_local = dt_utc.astimezone(ZoneInfo(tz_name))
            game_date = dt_local.strftime("%Y-%m-%d")
            game_hour = dt_local.hour

            resp = requests.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": lat, "longitude": lon,
                "hourly": "temperature_2m,windspeed_10m,winddirection_10m,relativehumidity_2m",
                "start_date": game_date, "end_date": game_date,
                "temperature_unit": "fahrenheit", "windspeed_unit": "mph",
                "timezone": tz_name,
            }, timeout=10)
            resp.raise_for_status()
            hourly = resp.json().get("hourly", {})

            temps = hourly.get("temperature_2m", []) or [68]
            winds = hourly.get("windspeed_10m", []) or [5]
            wind_dirs = hourly.get("winddirection_10m", []) or [0]
            humidities = hourly.get("relativehumidity_2m", []) or [50]

            idx = max(0, min(game_hour, len(temps) - 1))
            temp = temps[idx]
            wind = winds[idx]
            wind_dir = wind_dirs[idx] if idx < len(wind_dirs) else 0
            humidity = humidities[idx] if idx < len(humidities) else 50

            conn.execute("""
                UPDATE daily_slate
                SET temperature_f=?, wind_mph=?, wind_dir_deg=?, humidity_pct=?, dome=0
                WHERE game_pk=? AND date=?
            """, (temp, wind, wind_dir, humidity, g["game_pk"], date_str))
            ok += 1

        except Exception as e:
            print(f"    [WEATHER] {venue}: {e}")
            # Default weather (neutral)
            conn.execute("""
                UPDATE daily_slate
                SET temperature_f=68, wind_mph=5, wind_dir_deg=0, dome=0
                WHERE game_pk=? AND date=?
            """, (g["game_pk"], date_str))

    conn.commit()
    print(f"  [3/3] Done. Weather fetched for {ok}/{len(games)} games.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_morning(date_str: str):
    """Run the full morning ETL pipeline."""
    print("=" * 60)
    print(f"  MORNING ETL — {date_str}")
    print("=" * 60)

    conn = get_db()
    create_tables(conn)
    log_id = log_etl_start(conn, "morning", date_str)

    try:
        games = fetch_schedule(conn, date_str)
        if not games:
            print("  No games today. Nothing to do.")
            log_etl_complete(conn, log_id, detail="No games")
            return

        lineup_count = fetch_lineups(conn, games, date_str)
        fetch_weather(conn, games, date_str)

        # Summary
        total_lineup = conn.execute(
            "SELECT COUNT(*) FROM daily_lineup WHERE date=?", (date_str,)
        ).fetchone()[0]

        detail = f"{len(games)} games, {lineup_count} with lineups, {total_lineup} players"
        log_etl_complete(conn, log_id, rows=total_lineup, detail=detail)

        print(f"\n{'=' * 60}")
        print(f"  MORNING ETL COMPLETE")
        print(f"  {detail}")
        print(f"{'=' * 60}\n")

    except Exception as e:
        log_etl_fail(conn, log_id, str(e))
        print(f"\n  ETL FAILED: {e}")
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Morning ETL for HR Bets")
    parser.add_argument("--date", default="today", help="Date (YYYY-MM-DD)")
    args = parser.parse_args()

    if args.date == "today":
        date_str = datetime.now().strftime("%Y-%m-%d")
    else:
        date_str = args.date

    run_morning(date_str)


if __name__ == "__main__":
    main()
