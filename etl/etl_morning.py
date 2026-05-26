#!/usr/bin/env python3
"""
etl_morning.py — Morning data pipeline for Daily HR Bet.

Runs at ~10:00 AM. Fetches today's fresh data:
  1. Schedule + probable pitchers from MLB Stats API
  2. Confirmed lineups from bdfed matchup endpoint
  2.5. Roster status (IL / Paternity / Bereavement / Suspended) for any
       Tier 2/3 fallback lineup rows — B7 IL/scratch filter
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
import time
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
    print("\n  [1/4] Fetching schedule + probable pitchers...")

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
    print(f"  [1/4] Done. {len(games)} games written to daily_slate.")
    return games


# ---------------------------------------------------------------------------
# Step 2: Confirmed lineups
# ---------------------------------------------------------------------------

def fetch_lineups(conn, games: list[dict], date_str: str):
    """Fetch confirmed lineups for *date_str* via the MLB Stats API
    schedule endpoint with hydrate=lineups.

    2026-05-04 critical fix: the previous version called bdfed/matchup
    per game and treated `array index → batting order`. But the bdfed
    endpoint returns the alphabetized 26-man active roster, not the
    batting lineup. So Aaron Judge (last name "J" sorts at position 8)
    was getting batting_order=8 in daily_lineup every single day even
    when he was actually batting #2. The whole batting_order column on
    daily_lineup was alphabetical noise; the lineup_score factor in the
    composite (weight=0.150) was actively degrading rank quality.
    Replaced with the schedule-with-lineups call from fetch_daily_data,
    which returns the real, ordered `lineups.homePlayers[]` /
    `lineups.awayPlayers[]` arrays with each player's fullName.

    When the lineup hasn't been posted yet (typical for evening games
    before ~3pm ET), the API returns no lineups block. Those games
    fall through to a bdfed-roster fallback with batting_order=NULL,
    so downstream consumers don't pretend an alphabetical roster is a
    starting lineup.
    """
    print("\n  [2/4] Fetching confirmed lineups...")

    # Import here (not at module top) to avoid a circular import: this
    # module is loaded by run_daily.bat as `etl.etl_morning`, but
    # fetch_daily_data is in the project root.
    from fetch_daily_data import (
        fetch_lineups_for_date,
        fetch_recent_lineup_for_team,
        _bdfed_roster_fallback,
    )

    by_game = fetch_lineups_for_date(date_str)

    games_with_lineups = 0
    total_players = 0
    posted_sides = 0
    recent_sides = 0
    fallback_sides = 0

    for g in games:
        gpk = g["game_pk"]
        entry = by_game.get(gpk) or {}
        home = entry.get("home") or []
        away = entry.get("away") or []
        home_team_id = entry.get("home_team_id")
        away_team_id = entry.get("away_team_id")

        # Tier 1: statsapi posted lineups (already in `home`/`away`
        # if they were posted).
        if home: posted_sides += 1
        if away: posted_sides += 1

        # Tier 2: recent-lineup for missing sides. Better than
        # alphabetical roster — real batting order from the team's
        # last posted lineup. Stamped lineup_source = "recent:DATE"
        # so downstream consumers can flag fallback rows.
        if not home and home_team_id:
            recent = fetch_recent_lineup_for_team(home_team_id, date_str)
            if recent:
                home = recent["players"]
                recent_sides += 1
        if not away and away_team_id:
            recent = fetch_recent_lineup_for_team(away_team_id, date_str)
            if recent:
                away = recent["players"]
                recent_sides += 1

        # Tier 3: bdfed roster as last resort. Only fires for sides
        # that have neither posted today nor a recent prior lineup
        # (e.g., team that just played their first game of the season,
        # or recent-lookup API hiccup).
        if not home or not away:
            fb = _bdfed_roster_fallback(gpk)
            if not home:
                home = fb.get("home") or []
                fallback_sides += 1
            if not away:
                away = fb.get("away") or []
                fallback_sides += 1

        sides = {"home": home, "away": away}

        has_lineup = False
        for side in ["home", "away"]:
            for p in sides.get(side, []):
                pid = p.get("player_id")
                if not pid:
                    continue
                # Position may arrive as either a dict (bdfed fallback)
                # or a string abbreviation (statsapi shaped output).
                position = p.get("position")
                if isinstance(position, dict):
                    position = position.get("abbreviation") or ""
                # B7 (2026-05-25): persist lineup_source so ETL Step 2.5
                # can identify Tier 2/3 fallback rows to override with
                # roster-status data. Tier 1 (posted) rows are trusted as-is.
                conn.execute("""
                    INSERT OR REPLACE INTO daily_lineup
                    (game_pk, date, side, batting_order, player_id,
                     player_name, position, team, lineup_source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    gpk, date_str, side,
                    p.get("batting_order"),  # 1-9 when posted, None when fallback
                    pid,
                    p.get("name", ""),       # fullName when posted, boxscoreName when fallback
                    position or "",
                    g[f"{side}_team"],
                    p.get("lineup_source"),  # 'posted' / 'recent:DATE' / 'roster_fallback'
                ))
                total_players += 1
                has_lineup = True

        if has_lineup:
            games_with_lineups += 1

    conn.commit()
    total_sides = len(games) * 2
    print(f"  [2/4] Done. {total_players} players across {games_with_lineups}/{len(games)} games "
          f"({posted_sides}/{total_sides} posted, {recent_sides}/{total_sides} recent-fallback, "
          f"{fallback_sides}/{total_sides} roster-fallback).")
    return games_with_lineups


# ---------------------------------------------------------------------------
# Step 2.5: Roster status (IL / Paternity / Bereavement / Suspended)
# B7 (2026-05-25): only overrides Tier 2/3 fallback lineups — never a
# posted (Tier 1) lineup. If the team posted today's lineup with player X,
# they're starting; the IL/scratch filter must not second-guess that.
# ---------------------------------------------------------------------------

def fetch_roster_status(conn, games: list[dict], date_str: str):
    """For every Tier 2/3 fallback-sourced daily_lineup row, look up the
    player's roster status and persist to daily_player_status.

    Idempotent: deletes existing rows for *date_str* before inserting,
    so a re-run produces the same result without duplicates.

    Only writes rows for players currently on a fallback lineup. Posted-
    lineup rows are skipped entirely (we trust the posted lineup).
    """
    print("\n  [2.5/4] Fetching roster status for fallback lineups...")

    # Import here to avoid a circular import (this module is loaded as
    # `etl.etl_morning` but fetch_daily_data is in the project root).
    from fetch_daily_data import (
        fetch_lineups_for_date,
        fetch_team_roster_status,
    )

    # Idempotent re-run: clear the date's existing rows.
    conn.execute("DELETE FROM daily_player_status WHERE date = ?", (date_str,))
    conn.commit()

    # Find all players on a fallback lineup for the date. Need the team_id
    # so we can hit /teams/{team_id}/roster?date=DATE. fetch_lineups_for_date
    # is cached per process, so this is a free lookup of the team_id we
    # already used in Step 2.
    by_game = fetch_lineups_for_date(date_str)

    # Pull (game_pk, player_id, lineup_source) for every fallback row.
    # We pre-filter to lineup_source LIKE 'recent:%' OR = 'roster_fallback'
    # in SQL so we don't waste a roster fetch on posted-lineup rows.
    fallback_rows = conn.execute("""
        SELECT game_pk, player_id, lineup_source
        FROM daily_lineup
        WHERE date = ?
          AND lineup_source IS NOT NULL
          AND lineup_source != 'posted'
    """, (date_str,)).fetchall()

    if not fallback_rows:
        print("  [2.5/4] Done. No fallback-sourced lineup rows; nothing to override.")
        return

    # Group player_ids by team_id so we make ~30 calls max (one per team)
    # instead of one per player. The team_id comes from by_game[gpk] —
    # which side of the game each player_id is on is detectable from
    # daily_lineup.side, but the cleaner approach is to fetch BOTH sides'
    # roster status for any game with at least one fallback player (~30
    # team-roster calls in the worst case; typical day is far fewer).
    needed_team_ids: set[int] = set()
    for row in fallback_rows:
        gpk = row[0]
        entry = by_game.get(gpk) or {}
        for k in ("home_team_id", "away_team_id"):
            tid = entry.get(k)
            if tid:
                needed_team_ids.add(tid)

    if not needed_team_ids:
        print("  [2.5/4] Done. No team_ids resolvable for fallback rows.")
        return

    # Fetch roster status per team. ~30 HTTP calls max.
    status_by_pid: dict[int, dict] = {}
    n_teams_ok = 0
    for tid in needed_team_ids:
        statuses = fetch_team_roster_status(tid, date_str)
        if statuses:
            status_by_pid.update(statuses)
            n_teams_ok += 1

    # Persist rows. Only write the players actually in a fallback lineup
    # for this date — keeps daily_player_status compact and on-topic
    # (the table is the IL filter's input, not a full roster archive).
    n_written = 0
    n_likely_out = 0
    for row in fallback_rows:
        pid = row[1]
        st = status_by_pid.get(pid)
        if not st:
            # Player not in the team roster snapshot — could be a new
            # call-up the API hasn't reflected, or a stale fallback row.
            # Skip (no override means the row stays eligible).
            continue
        code = st.get("status_code")
        desc = st.get("status_description") or ""
        is_likely_out = 1 if (code and code != "A") else 0
        if is_likely_out:
            n_likely_out += 1
        conn.execute("""
            INSERT OR REPLACE INTO daily_player_status
                (date, player_id, status_code, status_description,
                 is_likely_out, source)
            VALUES (?, ?, ?, ?, ?, 'mlb_roster_api')
        """, (date_str, pid, code, desc, is_likely_out))
        n_written += 1

    conn.commit()
    print(f"  [2.5/4] Done. {n_teams_ok}/{len(needed_team_ids)} team rosters OK; "
          f"{n_written} status rows written ({n_likely_out} flagged is_likely_out=1).")


# ---------------------------------------------------------------------------
# Step 3: Weather
# ---------------------------------------------------------------------------

def fetch_weather(conn, games: list[dict], date_str: str):
    """Fetch weather for each venue and update daily_slate."""
    print("\n  [3/4] Fetching weather...")

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

            # 30s timeout + one retry on transient errors. Open-Meteo's free
            # tier appears to deprioritize GitHub Actions egress IPs; 10s was
            # causing 5-8 venue failures per run (see fetch_daily_data.get_weather).
            resp = None
            for attempt in range(2):
                try:
                    resp = requests.get("https://api.open-meteo.com/v1/forecast", params={
                        "latitude": lat, "longitude": lon,
                        "hourly": "temperature_2m,windspeed_10m,winddirection_10m,relativehumidity_2m",
                        "start_date": game_date, "end_date": game_date,
                        "temperature_unit": "fahrenheit", "windspeed_unit": "mph",
                        "timezone": tz_name,
                    }, timeout=30)
                    resp.raise_for_status()
                    break
                except (requests.Timeout, requests.ConnectionError):
                    if attempt == 0:
                        time.sleep(1.5)
                        continue
                    raise
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
    print(f"  [4/4] Done. Weather fetched for {ok}/{len(games)} games.")


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
        # B7 (2026-05-25): IL/scratch filter step. Walks Tier 2/3 fallback
        # lineup rows and writes is_likely_out flags into daily_player_status.
        # generate_picks reads this in its eligible_batters assembly.
        fetch_roster_status(conn, games, date_str)
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
