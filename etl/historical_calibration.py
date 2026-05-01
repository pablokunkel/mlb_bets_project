"""
historical_calibration.py — Backfill environmental-factor calibration data
from prior MLB seasons.

Why this exists:
    Our daily pick_inputs only covers 2026 picks (~150 outdoor pick-rows in
    the temp×humidity heatmap). Tiny sample for environmental diagnostics.
    Solution: pull weather + per-batter-game HR outcomes for prior seasons
    where we don't have to wait for games. 2 seasons backfilled = ~170k
    additional rows for the heatmap, sufficient to fill the HOT × HUMID
    cell that's currently empty in 2026 (it's only late April).

What this is and isn't for:
    USE for environmental factors (temp, humidity, wind, dome, park) — the
    physics of HR-vs-weather doesn't change between seasons. Park orientations
    are stable. Dome status is stable.

    DO NOT USE for player-specific factors (form, archetype, pitcher matchup)
    — those are time-dependent and player-specific, so historical seasons
    don't validate this season's scoring.

Caveat: 2021 ball-deadening shifted HR rates ~10%. Stick to 2023-2026 to
    avoid that step change. We default to seasons=[2024, 2025].

Two-stage pull:
    1) Open-Meteo archive API for weather (~2,400 unique (venue, date) tuples
       per season; ~3-4 hours rate-limited but fully automated)
    2) pybaseball.statcast for HR events + PA counts per batter-game
       (~30 min per season, season-wide pull)

Then a JOIN materializes a `historical_calibration` table with one row per
(date, game_pk, batter_id) including weather and HR outcome.

Usage:
    python etl/historical_calibration.py --seasons 2024 2025
    python etl/historical_calibration.py --weather-only --seasons 2024
    python etl/historical_calibration.py --outcomes-only --seasons 2025
    python etl/historical_calibration.py --build-table   # join + write
"""

import argparse
import os
import sys
import time
from datetime import datetime, date as date_cls, timedelta
from pathlib import Path

# Make sure we can import from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

import requests

from etl.db import get_db, create_tables
from etl.etl_morning import VENUE_COORDS, VENUE_TZ, normalize_venue


# Dome / retractable-roof venues. These get dome=1 unconditionally —
# no Open-Meteo call. Even retractable roofs are typically closed when
# weather is unfavorable, so treating as dome is the conservative choice.
DOME_VENUES = {
    "Tropicana Field",
    "Rogers Centre",
    "Chase Field",
    "Minute Maid Park",
    "loanDepot park",
    "Globe Life Field",
    "American Family Field",
    "T-Mobile Park",
}


# Team abbreviation → home venue. Used to resolve weather location
# from the statcast home_team column, which doesn't include venue.
# This mapping covers 2024-2026. The Oakland → Sutter Health Park
# move in 2025 is handled by season-aware lookup.
TEAM_VENUE = {
    "ARI": "Chase Field",
    "ATL": "Truist Park",
    "BAL": "Oriole Park at Camden Yards",
    "BOS": "Fenway Park",
    "CHC": "Wrigley Field",
    "CHW": "Guaranteed Rate Field",
    "CIN": "Great American Ball Park",
    "CLE": "Progressive Field",
    "COL": "Coors Field",
    "DET": "Comerica Park",
    "HOU": "Minute Maid Park",
    "KC":  "Kauffman Stadium",
    "LAA": "Angel Stadium",
    "LAD": "Dodger Stadium",
    "MIA": "loanDepot park",
    "MIL": "American Family Field",
    "MIN": "Target Field",
    "NYM": "Citi Field",
    "NYY": "Yankee Stadium",
    "PHI": "Citizens Bank Park",
    "PIT": "PNC Park",
    "SD":  "Petco Park",
    "SEA": "T-Mobile Park",
    "SF":  "Oracle Park",
    "STL": "Busch Stadium",
    "TB":  "Tropicana Field",
    "TEX": "Globe Life Field",
    "TOR": "Rogers Centre",
    "WSH": "Nationals Park",
    # Oakland: Oakland Coliseum 2024, then Sutter Health Park starting 2025
    "OAK": "Oakland Coliseum",
    "ATH": "Sutter Health Park",  # post-rename
}


def team_to_venue(team_abbrev: str, season: int) -> str | None:
    """Resolve home venue for a team in a given season. Handles the
    Oakland → Sutter Health move (2025+)."""
    if team_abbrev in ("OAK", "ATH") and season >= 2025:
        return "Sutter Health Park"
    if team_abbrev in ("OAK", "ATH") and season == 2024:
        return "Oakland Coliseum"
    return TEAM_VENUE.get(team_abbrev)


# ---------------------------------------------------------------------------
# Weather backfill (Open-Meteo archive API)
# ---------------------------------------------------------------------------

OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_RATE_LIMIT_SLEEP = 0.6  # seconds between calls (free tier ~600/hr)


def fetch_historical_weather(
    venue: str,
    game_date: str,
    game_hour: int = 19,  # default 7pm local
) -> dict | None:
    """
    Pull historical hourly weather for a venue on a specific date.
    Picks the hour closest to first-pitch local time.

    Returns {temperature_f, wind_mph, wind_dir_deg, humidity_pct} or None.
    Domes return a fixed neutral payload without an API call.

    Open-Meteo archive supports dates from 1940 to 5 days ago (real-time
    data is in the forecast endpoint, not archive).
    """
    if venue in DOME_VENUES:
        return {
            "temperature_f": 72, "wind_mph": 0, "wind_dir_deg": 0,
            "humidity_pct": 50, "dome": 1,
        }

    coords = VENUE_COORDS.get(venue)
    if not coords:
        return None
    lat, lon = coords
    tz_name = VENUE_TZ.get(venue, "America/New_York")

    try:
        resp = requests.get(OPEN_METEO_ARCHIVE, params={
            "latitude": lat, "longitude": lon,
            "hourly": "temperature_2m,windspeed_10m,winddirection_10m,relativehumidity_2m",
            "start_date": game_date, "end_date": game_date,
            "temperature_unit": "fahrenheit", "windspeed_unit": "mph",
            "timezone": tz_name,
        }, timeout=15)
        resp.raise_for_status()
        hourly = resp.json().get("hourly", {})

        temps      = hourly.get("temperature_2m", []) or [68]
        winds      = hourly.get("windspeed_10m", []) or [5]
        wind_dirs  = hourly.get("winddirection_10m", []) or [0]
        humidities = hourly.get("relativehumidity_2m", []) or [50]

        idx = max(0, min(game_hour, len(temps) - 1))
        return {
            "temperature_f": temps[idx],
            "wind_mph":      winds[idx],
            "wind_dir_deg":  wind_dirs[idx] if idx < len(wind_dirs)  else 0,
            "humidity_pct":  humidities[idx] if idx < len(humidities) else 50,
            "dome":          0,
        }
    except Exception as e:
        print(f"  [WEATHER] {venue} {game_date}: {e}")
        return None


def backfill_weather_for_season(season: int, max_calls: int | None = None) -> int:
    """
    Pull historical weather for every (date, venue) tuple where the team
    played a home game in the given season. Skips tuples already cached
    in historical_game_weather.

    Returns count of new rows written. Pass max_calls to throttle for
    testing — None means run the full season.
    """
    conn = get_db()
    create_tables(conn)

    # Need (date, home_team) tuples for the season. We get these from the
    # outcomes pull (statcast game_date + home_team). If the historical
    # outcomes table has data, use it. Otherwise this function should be
    # run AFTER backfill_outcomes_for_season.
    rows = conn.execute("""
        SELECT DISTINCT date, home_team
        FROM historical_batter_games
        WHERE season = ?
    """, (season,)).fetchall()

    if not rows:
        print(f"  [WEATHER] No outcomes rows for {season}. Run --outcomes-only "
              f"first, then re-run --weather-only.")
        conn.close()
        return 0

    # Already-fetched (date, venue) tuples
    cached = set()
    for r in conn.execute("""
        SELECT date, venue FROM historical_game_weather
        WHERE strftime('%Y', date) = ?
    """, (str(season),)):
        cached.add((r["date"], r["venue"]))

    targets = []
    for r in rows:
        venue = team_to_venue(r["home_team"], season)
        if venue is None:
            continue
        if (r["date"], venue) in cached:
            continue
        targets.append((r["date"], venue, r["home_team"]))

    if max_calls is not None:
        targets = targets[:max_calls]

    print(f"  [WEATHER] {season}: {len(targets)} new (date,venue) tuples to fetch "
          f"(cached: {len(cached)})")

    n_written = 0
    for i, (gdate, venue, home_team) in enumerate(targets, start=1):
        weather = fetch_historical_weather(venue, gdate, game_hour=19)
        if weather is None:
            continue
        conn.execute("""
            INSERT OR REPLACE INTO historical_game_weather
              (date, venue, home_team, season, temperature_f, wind_mph,
               wind_dir_deg, humidity_pct, dome, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """, (
            gdate, venue, home_team, season,
            weather["temperature_f"], weather["wind_mph"],
            weather["wind_dir_deg"], weather["humidity_pct"],
            weather["dome"],
        ))
        n_written += 1
        if i % 50 == 0:
            conn.commit()
            print(f"  [WEATHER] {season}: {i}/{len(targets)} fetched...")
        time.sleep(OPEN_METEO_RATE_LIMIT_SLEEP)

    conn.commit()
    conn.close()
    print(f"  [WEATHER] {season}: {n_written} rows written.")
    return n_written


# ---------------------------------------------------------------------------
# Outcomes backfill (pybaseball statcast — HRs + PA per batter-game)
# ---------------------------------------------------------------------------

def backfill_outcomes_for_season(season: int) -> int:
    """
    Pull season-wide statcast pitch-by-pitch data, aggregate to
    (game_pk, date, batter, home_team) → hr_count + pa_count.
    Writes to historical_batter_games.

    Slow — typically 30-60 min per season due to large statcast pulls.
    pybaseball caches its CSVs by date range so reruns are fast.
    """
    try:
        from pybaseball import statcast
    except ImportError:
        print("  [OUTCOMES] pybaseball not installed. pip install pybaseball")
        return 0

    start_dt = f"{season}-03-15"  # spring training tail; safe lower bound
    end_dt   = f"{season}-11-05"  # post-WS upper bound

    print(f"  [OUTCOMES] {season}: pulling statcast {start_dt} -> {end_dt}...")
    df = statcast(start_dt=start_dt, end_dt=end_dt, verbose=False)
    if df is None or df.empty:
        print(f"  [OUTCOMES] {season}: empty statcast frame")
        return 0

    # Filter to PA-ending events (each row = one pitch; PA-ending events have
    # the `events` column populated). Then aggregate to batter-game.
    if "events" not in df.columns:
        print(f"  [OUTCOMES] {season}: 'events' column missing")
        return 0

    pa_ending = df[df["events"].notna()].copy()
    pa_ending["hr"] = (pa_ending["events"] == "home_run").astype(int)

    # Group by (game_pk, game_date, batter, home_team)
    agg = pa_ending.groupby(
        ["game_pk", "game_date", "batter", "home_team"], dropna=False
    ).agg(
        hr_count=("hr", "sum"),
        pa_count=("hr", "count"),
    ).reset_index()

    # batter_name lookup — statcast has 'player_name' as a redundant column,
    # but it's pitcher-named for the pitch row, not batter. We'll skip name
    # for now and pull from MLB Stats API later if needed.

    conn = get_db()
    create_tables(conn)
    n_written = 0
    for _, row in agg.iterrows():
        try:
            conn.execute("""
                INSERT OR REPLACE INTO historical_batter_games
                  (date, game_pk, batter_id, home_team, season, hr_count, pa_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                str(row["game_date"]),
                int(row["game_pk"]),
                int(row["batter"]),
                str(row["home_team"]),
                season,
                int(row["hr_count"]),
                int(row["pa_count"]),
            ))
            n_written += 1
        except Exception:
            continue

        if n_written % 5000 == 0:
            conn.commit()
            print(f"  [OUTCOMES] {season}: {n_written} rows written...")

    conn.commit()
    conn.close()
    print(f"  [OUTCOMES] {season}: {n_written} batter-game rows.")
    return n_written


# ---------------------------------------------------------------------------
# Materialized join — historical_calibration table
# ---------------------------------------------------------------------------

def build_historical_calibration_table(seasons: list[int]) -> int:
    """
    Join historical_batter_games × historical_game_weather into a single
    denormalized historical_calibration table. Run after both backfills
    complete. Idempotent — DELETEs rows for the given seasons before
    re-inserting.
    """
    conn = get_db()
    create_tables(conn)

    # Wipe rows for the seasons being rebuilt (idempotency).
    placeholders = ",".join("?" * len(seasons))
    conn.execute(
        f"DELETE FROM historical_calibration WHERE season IN ({placeholders})",
        tuple(seasons),
    )

    n = conn.execute(f"""
        INSERT INTO historical_calibration (
            date, game_pk, batter_id, home_team, venue, season,
            temperature_f, wind_mph, wind_dir_deg, humidity_pct, dome,
            hr_count, pa_count
        )
        SELECT
            o.date, o.game_pk, o.batter_id, o.home_team,
            COALESCE(w.venue, '') AS venue,
            o.season,
            w.temperature_f, w.wind_mph, w.wind_dir_deg, w.humidity_pct, w.dome,
            o.hr_count, o.pa_count
        FROM historical_batter_games o
        LEFT JOIN historical_game_weather w
            ON w.date = o.date AND w.season = o.season
            AND w.home_team = o.home_team
        WHERE o.season IN ({placeholders})
    """, tuple(seasons))

    conn.commit()
    n_rows = conn.execute(
        f"SELECT COUNT(*) AS n FROM historical_calibration WHERE season IN ({placeholders})",
        tuple(seasons),
    ).fetchone()["n"]
    conn.close()
    print(f"  [JOIN] historical_calibration rows for {seasons}: {n_rows}")
    return n_rows


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Backfill historical environmental calibration data."
    )
    parser.add_argument("--seasons", type=int, nargs="+", default=[2024, 2025],
                        help="Seasons to backfill (default: 2024 2025)")
    parser.add_argument("--weather-only", action="store_true",
                        help="Skip outcomes pull, only fetch weather")
    parser.add_argument("--outcomes-only", action="store_true",
                        help="Skip weather pull, only fetch outcomes")
    parser.add_argument("--build-table", action="store_true",
                        help="(Re)materialize historical_calibration after pulls")
    parser.add_argument("--max-calls", type=int, default=None,
                        help="Cap weather API calls (for testing)")
    args = parser.parse_args()

    print(f"Historical calibration backfill: seasons={args.seasons}")

    if not args.weather_only:
        for season in args.seasons:
            backfill_outcomes_for_season(season)

    if not args.outcomes_only:
        for season in args.seasons:
            backfill_weather_for_season(season, max_calls=args.max_calls)

    if args.build_table or (not args.weather_only and not args.outcomes_only):
        build_historical_calibration_table(args.seasons)

    print("Done.")


if __name__ == "__main__":
    main()
