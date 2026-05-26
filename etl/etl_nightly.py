#!/usr/bin/env python3
"""
etl_nightly.py — Nightly data pipeline for Daily HR Bet.

Runs at ~2:00 AM. Incrementally syncs:
  1. Batter HR events from Statcast (only new games since last fetch)
  2. Pitcher arsenals (refresh any older than 7 days)
  3. Victim profiles (recompute for any batter with new HR events)
  4. Season batting stats (full refresh from MLB Stats API)
  5. Season pitching stats (full refresh from MLB Stats API)

First run does a full 18-month backfill. Subsequent runs are incremental
(~30 seconds for a normal day).

Usage:
    # Normal nightly run (incremental)
    python -m etl.etl_nightly

    # Full backfill (first time or to rebuild)
    python -m etl.etl_nightly --backfill

    # Specific date context
    python -m etl.etl_nightly --date 2026-04-08
"""

import argparse
import sys
import time
from datetime import datetime, timedelta
from math import sqrt
from pathlib import Path

import requests

# Add parent to path so we can import from the main project
sys.path.insert(0, str(Path(__file__).parent.parent))

from etl.db import (
    get_db, create_tables, get_latest_hr_date, get_arsenal_age_days,
    get_park_factors_age_days,
    log_etl_start, log_etl_complete, log_etl_fail,
)
from etl.park_factors_seed import get_seed_dataframe as get_park_seed_df

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MLB_API = "https://statsapi.mlb.com/api/v1"

FASTBALL_TYPES = {"FF", "SI", "FC", "FA"}
BREAKING_TYPES = {"SL", "CU", "KC", "SV", "CS", "KN", "SC", "EP"}
OFFSPEED_TYPES = {"CH", "FS", "FO"}

ARSENAL_REFRESH_DAYS = 7  # re-fetch pitcher arsenals older than this
PARK_FACTORS_REFRESH_DAYS = 7  # re-sync park factors at most weekly

TEAM_NAME_TO_ABBREV = {
    "Arizona Diamondbacks": "ARI", "Atlanta Braves": "ATL",
    "Baltimore Orioles": "BAL", "Boston Red Sox": "BOS",
    "Chicago Cubs": "CHC", "Chicago White Sox": "CWS",
    "Cincinnati Reds": "CIN", "Cleveland Guardians": "CLE",
    "Colorado Rockies": "COL", "Detroit Tigers": "DET",
    "Houston Astros": "HOU", "Kansas City Royals": "KC",
    "Los Angeles Angels": "LAA", "Los Angeles Dodgers": "LAD",
    "Miami Marlins": "MIA", "Milwaukee Brewers": "MIL",
    "Minnesota Twins": "MIN", "New York Mets": "NYM",
    "New York Yankees": "NYY", "Oakland Athletics": "OAK",
    "Philadelphia Phillies": "PHI", "Pittsburgh Pirates": "PIT",
    "San Diego Padres": "SD", "San Francisco Giants": "SF",
    "Seattle Mariners": "SEA", "St. Louis Cardinals": "STL",
    "Tampa Bay Rays": "TB", "Texas Rangers": "TEX",
    "Toronto Blue Jays": "TOR", "Washington Nationals": "WSH",
}


# ---------------------------------------------------------------------------
# Step 1: Batter HR events from Statcast
# ---------------------------------------------------------------------------

def sync_batter_hr_events(conn, season: int, backfill: bool = False):
    """
    Incrementally sync HR events from Statcast.
    - backfill=True: pull full 18-month history
    - backfill=False: only fetch since last known HR date per batter
    """
    from pybaseball import statcast

    print("\n  [1/6] Syncing batter HR events from Statcast...")

    if backfill:
        # Full backfill: current season + prior seasons
        # Covers 2024-2026 (3 seasons) for rich historical data
        today = datetime.now().strftime("%Y-%m-%d")
        ranges = [
            (f"{season}-03-20", today),
        ]
        # Add prior seasons (go back up to 2 years)
        for prior in range(1, 3):
            yr = season - prior
            if yr >= 2024:
                ranges.append((f"{yr}-03-20", f"{yr}-10-05"))
    else:
        # Incremental: just yesterday and today
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        today = datetime.now().strftime("%Y-%m-%d")
        ranges = [(yesterday, today)]

    total_new = 0

    # Chunk large date ranges into ~7-day windows to avoid Savant throttling
    chunked_ranges = []
    for start_date, end_date in ranges:
        sd = datetime.strptime(start_date, "%Y-%m-%d")
        ed = datetime.strptime(end_date, "%Y-%m-%d")
        while sd < ed:
            chunk_end = min(sd + timedelta(days=6), ed)
            chunked_ranges.append((sd.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")))
            sd = chunk_end + timedelta(days=1)

    total_chunks = len(chunked_ranges)
    print(f"    {total_chunks} date chunks to fetch across {len(ranges)} season range(s)")

    for ci, (start_date, end_date) in enumerate(chunked_ranges, 1):
        print(f"    [{ci}/{total_chunks}] Fetching Statcast: {start_date} → {end_date}...")

        try:
            df = statcast(start_dt=start_date, end_dt=end_date)
        except Exception as e:
            print(f"    ERROR fetching Statcast: {e}")
            time.sleep(5)  # back off on error
            continue

        if df is None or df.empty:
            print(f"      No data returned")
            time.sleep(1)
            continue

        # Filter to HR events only
        hrs = df[df["events"] == "home_run"].copy()
        print(f"      {len(hrs)} HR events in chunk")

        if hrs.empty:
            time.sleep(1)
            continue

        # Insert into database
        inserted = 0
        for _, row in hrs.iterrows():
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO batter_hr_events
                    (batter_id, batter_name, pitcher_id, pitcher_name,
                     game_date, game_pk, p_throws, pitch_type,
                     release_speed, release_spin, release_ext,
                     launch_speed, launch_angle, hit_distance)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    int(row.get("batter", 0)),
                    str(row.get("player_name", "")),
                    int(row.get("pitcher", 0)),
                    str(row.get("pitcher_name", "")),
                    str(row.get("game_date", ""))[:10],
                    int(row.get("game_pk", 0)) if row.get("game_pk") else None,
                    str(row.get("p_throws", "")),
                    str(row.get("pitch_type", "")),
                    float(row["release_speed"]) if row.get("release_speed") else None,
                    float(row["release_spin_rate"]) if row.get("release_spin_rate") else None,
                    float(row["release_extension"]) if row.get("release_extension") else None,
                    float(row["launch_speed"]) if row.get("launch_speed") else None,
                    float(row["launch_angle"]) if row.get("launch_angle") else None,
                    float(row["hit_distance_sc"]) if row.get("hit_distance_sc") else None,
                ))
                inserted += 1
            except Exception:
                pass  # UNIQUE constraint = already have this event

        conn.commit()
        total_new += inserted
        print(f"      Inserted {inserted} new HR events")

        # Rate-limit courtesy for Savant (~2s between chunks)
        time.sleep(2)

    print(f"  [1/6] Done. {total_new} new HR events total.")
    return total_new


# ---------------------------------------------------------------------------
# Step 2: Pitcher arsenals from Statcast
# ---------------------------------------------------------------------------

def sync_pitcher_arsenals(conn, season: int, force: bool = False):
    """
    Refresh pitcher arsenals that are stale (> 7 days old) or missing.
    Fetches from Statcast pitch-level data.
    """
    from pybaseball import statcast_pitcher

    print("\n  [2/6] Syncing pitcher arsenals...")

    # Find all unique pitcher IDs we care about:
    # 1. Pitchers who appear in batter_hr_events (victim pitchers)
    # 2. Pitchers in season_pitching (today's starters will be here after morning ETL)
    pitcher_ids = set()

    rows = conn.execute(
        "SELECT DISTINCT pitcher_id FROM batter_hr_events WHERE pitcher_id > 0"
    ).fetchall()
    pitcher_ids.update(r[0] for r in rows)

    rows = conn.execute(
        "SELECT DISTINCT pitcher_id FROM season_pitching WHERE season = ?",
        (season,)
    ).fetchall()
    pitcher_ids.update(r[0] for r in rows)

    print(f"    {len(pitcher_ids)} unique pitchers to check")

    # Filter to those needing refresh
    need_refresh = []
    for pid in pitcher_ids:
        age = get_arsenal_age_days(conn, pid, season)
        if force or age is None or age > ARSENAL_REFRESH_DAYS:
            need_refresh.append(pid)

    print(f"    {len(need_refresh)} need refresh (>{ARSENAL_REFRESH_DAYS}d stale or missing)")

    if not need_refresh:
        print("  [2/6] All arsenals fresh. Skipping.")
        return 0

    updated = 0
    failed = 0
    start = f"{season}-03-20"
    end = datetime.now().strftime("%Y-%m-%d")

    for i, pid in enumerate(need_refresh):
        if (i + 1) % 25 == 0:
            print(f"    Progress: {i+1}/{len(need_refresh)} pitchers...")

        try:
            df = statcast_pitcher(start, end, pid)

            if df is None or df.empty:
                # Try prior season
                df = statcast_pitcher(f"{season-1}-03-20", f"{season-1}-10-05", pid)

            if df is None or df.empty:
                failed += 1
                continue

            # Aggregate arsenal
            pitch_types = df["pitch_type"].dropna()
            if pitch_types.empty:
                failed += 1
                continue

            total_pitches = len(pitch_types)
            arsenal = {}
            for pt in pitch_types.unique():
                arsenal[pt] = len(pitch_types[pitch_types == pt]) / total_pitches

            fb_pct = sum(v for k, v in arsenal.items() if k in FASTBALL_TYPES)
            brk_pct = sum(v for k, v in arsenal.items() if k in BREAKING_TYPES)
            off_pct = sum(v for k, v in arsenal.items() if k in OFFSPEED_TYPES)
            total = fb_pct + brk_pct + off_pct
            if total > 0:
                fb_pct /= total
                brk_pct /= total
                off_pct /= total

            fb_mask = df["pitch_type"].isin(FASTBALL_TYPES)
            fb_data = df[fb_mask]

            avg_velo = float(fb_data["release_speed"].mean()) if not fb_data.empty and fb_data["release_speed"].notna().any() else None
            avg_spin = float(fb_data["release_spin_rate"].mean()) if not fb_data.empty and fb_data["release_spin_rate"].notna().any() else None
            avg_ext = float(df["release_extension"].mean()) if df["release_extension"].notna().any() else None
            p_throws = df["p_throws"].mode().iloc[0] if not df["p_throws"].mode().empty else "R"

            conn.execute("""
                INSERT OR REPLACE INTO pitcher_arsenals
                (pitcher_id, season, avg_fb_velo, fb_usage_pct, breaking_pct,
                 offspeed_pct, avg_fb_spin, avg_extension, p_throws,
                 total_pitches, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'statcast')
            """, (
                pid, season,
                round(avg_velo, 1) if avg_velo else None,
                round(fb_pct, 3),
                round(brk_pct, 3),
                round(off_pct, 3),
                round(avg_spin, 0) if avg_spin else None,
                round(avg_ext, 1) if avg_ext else None,
                p_throws,
                total_pitches,
            ))
            updated += 1

        except Exception as e:
            failed += 1

        # Rate limit: ~3 req/sec for Savant
        time.sleep(0.35)

    conn.commit()
    print(f"  [2/6] Done. {updated} updated, {failed} failed.")
    return updated


# ---------------------------------------------------------------------------
# Step 3: Victim profiles (computed from local data, no API calls)
# ---------------------------------------------------------------------------

def recompute_victim_profiles(conn, season: int):
    """
    Recompute victim profiles for batters who have new HR events.
    Uses data already in the database — no API calls needed.
    """
    print("\n  [3/6] Recomputing victim profiles...")

    # Get all batters with HR events
    batters = conn.execute("""
        SELECT batter_id, batter_name, COUNT(*) as hr_count
        FROM batter_hr_events
        GROUP BY batter_id
        HAVING hr_count >= 1
        ORDER BY hr_count DESC
    """).fetchall()

    print(f"    {len(batters)} batters with HR events")

    updated = 0
    for batter_id, batter_name, hr_count in batters:
        # Get all unique victim pitchers and HR counts against each
        victims = conn.execute("""
            SELECT pitcher_id, COUNT(*) as hrs_against
            FROM batter_hr_events
            WHERE batter_id = ?
            GROUP BY pitcher_id
        """, (batter_id,)).fetchall()

        # Get arsenals for victim pitchers (from our local DB)
        arsenals = []
        for victim_pid, hrs_against in victims:
            row = conn.execute("""
                SELECT avg_fb_velo, fb_usage_pct, breaking_pct, offspeed_pct,
                       avg_fb_spin, avg_extension, p_throws
                FROM pitcher_arsenals
                WHERE pitcher_id = ?
                ORDER BY season DESC LIMIT 1
            """, (victim_pid,)).fetchone()

            # Audit MED fix: was using `or LEAGUE_MEAN` for every column —
            # silently filled None with league mean and weighted that fill
            # into the victim profile, dragging archetype similarity toward
            # generic-pitcher for batters whose victim history happens to
            # include sparse-arsenal pitchers. Now: SKIP victim-pitchers
            # with missing avg_fb_velo (the most discriminative dimension)
            # so the weighted average reflects only measured arsenals.
            #
            # Edge case: if ALL arsenals are missing velo, the existing
            # per-event fallback path (lines 386-397) takes over below.
            if row and row["avg_fb_velo"] is not None and row["avg_fb_velo"] > 0:
                arsenals.append({
                    "avg_fb_velo": row["avg_fb_velo"],
                    "fb_usage_pct": row["fb_usage_pct"] if row["fb_usage_pct"] is not None else 0.53,
                    "breaking_pct": row["breaking_pct"] if row["breaking_pct"] is not None else 0.28,
                    "offspeed_pct": row["offspeed_pct"] if row["offspeed_pct"] is not None else 0.15,
                    "avg_fb_spin": row["avg_fb_spin"] if row["avg_fb_spin"] is not None else 2250,
                    "avg_extension": row["avg_extension"] if row["avg_extension"] is not None else 6.2,
                    "p_throws": row["p_throws"] or "R",  # str — `or` is fine here
                    "weight": hrs_against,
                })

        # Also get per-event pitch data for handedness
        events = conn.execute("""
            SELECT p_throws FROM batter_hr_events
            WHERE batter_id = ? AND p_throws IS NOT NULL AND p_throws != ''
        """, (batter_id,)).fetchall()

        hands = [e["p_throws"] for e in events]
        hand_r_pct = sum(1 for h in hands if h == "R") / max(len(hands), 1) if hands else 0.65

        if arsenals:
            total_w = sum(a["weight"] for a in arsenals)
            avg_velo = sum(a["avg_fb_velo"] * a["weight"] for a in arsenals) / total_w
            fb_pct = sum(a["fb_usage_pct"] * a["weight"] for a in arsenals) / total_w
            brk_pct = sum(a["breaking_pct"] * a["weight"] for a in arsenals) / total_w
            off_pct = sum(a["offspeed_pct"] * a["weight"] for a in arsenals) / total_w
            avg_spin = sum(a["avg_fb_spin"] * a["weight"] for a in arsenals) / total_w
            avg_ext = sum(a["avg_extension"] * a["weight"] for a in arsenals) / total_w
            n_pitchers = len(arsenals)
        else:
            # Fallback: use per-event pitch data
            velos = conn.execute(
                "SELECT AVG(release_speed) FROM batter_hr_events WHERE batter_id = ? AND release_speed > 0",
                (batter_id,)
            ).fetchone()
            spins = conn.execute(
                "SELECT AVG(release_spin) FROM batter_hr_events WHERE batter_id = ? AND release_spin > 0",
                (batter_id,)
            ).fetchone()
            avg_velo = velos[0] if velos[0] else 93.5
            avg_spin = spins[0] if spins[0] else 2250
            avg_ext = 6.2
            fb_pct, brk_pct, off_pct = 0.53, 0.28, 0.15
            n_pitchers = 0

        # Confidence
        if hr_count >= 15 and n_pitchers >= 8:
            confidence = 1.0
        elif hr_count >= 8 and n_pitchers >= 4:
            confidence = 0.8
        elif hr_count >= 3:
            confidence = 0.6
        else:
            confidence = 0.3

        conn.execute("""
            INSERT OR REPLACE INTO victim_profiles
            (batter_id, season, batter_name, avg_fb_velo, fb_usage_pct,
             breaking_pct, offspeed_pct, hand_r_pct, avg_fb_spin,
             avg_extension, hr_count, n_victim_pitchers, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            batter_id, season, batter_name,
            round(avg_velo, 1), round(fb_pct, 3), round(brk_pct, 3),
            round(off_pct, 3), round(hand_r_pct, 2), round(avg_spin, 0),
            round(avg_ext, 1), hr_count, n_pitchers, confidence,
        ))
        updated += 1

    conn.commit()
    print(f"  [3/6] Done. {updated} victim profiles computed.")
    return updated


# ---------------------------------------------------------------------------
# Step 4: Season batting stats from MLB Stats API
# ---------------------------------------------------------------------------

def sync_season_batting(conn, season: int, date_str: str):
    """Pull season batting stats from MLB Stats API."""
    print("\n  [4/6] Syncing season batting stats...")

    url = f"{MLB_API}/stats"
    params = {
        "stats": "season",
        "season": season,
        "group": "hitting",
        "gameType": "R",
        "sportId": 1,
        "sortStat": "homeRuns",
        "order": "desc",
        "limit": 500,
    }

    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        stats_list = resp.json().get("stats", [])
        splits = stats_list[0].get("splits", []) if stats_list else []
    except Exception as e:
        print(f"    ERROR: {e}")
        return 0

    print(f"    Got {len(splits)} batters from MLB Stats API")

    inserted = 0
    for s in splits:
        stat = s.get("stat", {})
        player = s.get("player", {})
        team = s.get("team", {})

        hr = stat.get("homeRuns", 0)
        ab = stat.get("atBats", 0)
        pa = stat.get("plateAppearances", ab)
        games = stat.get("gamesPlayed", 0)

        avg = float(stat.get("avg", "0") or "0")
        slg = float(stat.get("slg", "0") or "0")
        obp = float(stat.get("obp", "0") or "0")
        iso = round(slg - avg, 3)
        woba = round(obp * 0.7 + slg * 0.3, 3)
        hr_per_pa = hr / max(pa, 1)

        team_name = team.get("name", "")
        team_abbrev = team.get("abbreviation") or TEAM_NAME_TO_ABBREV.get(team_name, "???")
        bat_side = player.get("batSide", {}).get("code", "R")

        conn.execute("""
            INSERT OR REPLACE INTO season_batting
            (player_id, season, player_name, team, bats, games, pa, ab, hr,
             hr_per_pa, avg, slg, obp, iso, woba,
             barrel_pct, exit_velo, hr_fb_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            player.get("id", 0), season,
            player.get("fullName", "Unknown"),
            team_abbrev, bat_side,
            games, pa, ab, hr,
            round(hr_per_pa, 4), avg, slg, obp, iso, woba,
            round(min(25, hr_per_pa * 200), 1),
            round(82 + slg * 15, 1),
            round(hr_per_pa * 100 * 1.8, 1),
        ))
        inserted += 1

    conn.commit()
    print(f"  [4/6] Done. {inserted} batters synced.")
    return inserted


# ---------------------------------------------------------------------------
# Step 5: Season pitching stats from MLB Stats API
# ---------------------------------------------------------------------------

def sync_season_pitching(conn, season: int):
    """Pull season pitching stats from MLB Stats API."""
    print("\n  [5/6] Syncing season pitching stats...")

    url = f"{MLB_API}/stats"
    params = {
        "stats": "season",
        "season": season,
        "group": "pitching",
        "gameType": "R",
        "sportId": 1,
        "sortStat": "inningsPitched",
        "order": "desc",
        "limit": 400,
    }

    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        stats_list = resp.json().get("stats", [])
        splits = stats_list[0].get("splits", []) if stats_list else []
    except Exception as e:
        print(f"    ERROR: {e}")
        return 0

    print(f"    Got {len(splits)} pitchers from MLB Stats API")

    inserted = 0
    for s in splits:
        stat = s.get("stat", {})
        player = s.get("player", {})
        team = s.get("team", {})

        ip = float(stat.get("inningsPitched", "0") or "0")
        hr_allowed = stat.get("homeRuns", 0)
        era = float(stat.get("era", "4.00") or "4.00")
        k = stat.get("strikeOuts", 0)
        bb = stat.get("baseOnBalls", 0)
        hits = stat.get("hits", 0)

        hr_per_9 = round((hr_allowed / max(ip, 1)) * 9, 2) if ip > 0 else 1.2
        k_per_9 = round((k / max(ip, 1)) * 9, 2) if ip > 0 else 8.0
        whip = round((bb + hits) / max(ip, 1), 2) if ip > 0 else 1.3
        est_hard_hit = round(min(50, max(25, 25 + (whip - 1.0) * 20)), 1)

        p_throws = player.get("pitchHand", {}).get("code", "R")
        team_name = team.get("name", "")
        team_abbrev = team.get("abbreviation") or TEAM_NAME_TO_ABBREV.get(team_name, "???")

        conn.execute("""
            INSERT OR REPLACE INTO season_pitching
            (pitcher_id, season, pitcher_name, team, p_throws,
             ip, era, hr_per_9, k_per_9, whip, hard_hit_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            player.get("id", 0), season,
            player.get("fullName", "Unknown"),
            team_abbrev, p_throws,
            ip, era, hr_per_9, k_per_9, whip, est_hard_hit,
        ))
        inserted += 1

    conn.commit()
    print(f"  [5/6] Done. {inserted} pitchers synced.")
    return inserted


# ---------------------------------------------------------------------------
# Step 7: Park archetype snapshot (Phase 2, 2026-05-25)
# ---------------------------------------------------------------------------

def sync_park_archetype(conn, date_str: str) -> int:
    """Refresh batter_park_archetype with a row per (batter, date_str).

    For every batter with at least one HR strictly before *date_str*,
    compute the 6-element park-feature centroid (weighted by 1 /
    park_neutral_hr_factor) and persist it. Batters below
    PARK_ARCHETYPE_MIN_HRS get centroid=NULL but the row still lands so
    the harness's threshold sweep can read n_hrs_used.

    Returns number of rows upserted.

    The math is in features_v2.compute_batter_park_archetype -- this is
    the nightly hook that calls it on today's eligible batters and writes
    the result to the snapshot table.
    """
    print("\n  [7/7] Park archetype snapshot for today...")

    # Defer the heavy imports so the rest of the nightly works if
    # features_v2 has a transitive issue.
    import json
    try:
        from features_v2 import compute_batter_park_archetype
    except Exception as e:
        print(f"    [SKIP] features_v2 import failed: {e}")
        return 0

    batters = [r[0] for r in conn.execute(
        "SELECT DISTINCT batter_id FROM batter_hr_events "
        "WHERE game_date < ? AND batter_id IS NOT NULL AND batter_id > 0",
        (date_str,),
    ).fetchall()]
    if not batters:
        print("    no eligible batters")
        return 0

    # The builder takes db_path; we already have a connection but pass the
    # path so the builder uses its own connection-pooling logic.
    from etl.db import DB_PATH
    db_path = str(DB_PATH)
    result = compute_batter_park_archetype(
        player_ids=batters,
        as_of_date=date_str,
        db_path=db_path,
    )

    n_upserted = 0
    n_with, n_below = 0, 0
    for bid, entry in result.items():
        centroid = entry.get("centroid")
        n_hrs = int(entry.get("n_hrs_used", 0))
        centroid_json = json.dumps(centroid) if centroid is not None else None
        if centroid is not None:
            n_with += 1
        else:
            n_below += 1
        conn.execute(
            """
            INSERT OR REPLACE INTO batter_park_archetype
                (player_id, date_through, feature_centroid_json,
                 n_hrs_used, fetched_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            """,
            (bid, date_str, centroid_json, n_hrs),
        )
        n_upserted += 1
    conn.commit()
    print(f"    {n_upserted} batters | {n_with} centroids | "
          f"{n_below} below-threshold")
    return n_upserted


# ---------------------------------------------------------------------------
# Step 6: Park factors (weekly refresh)
# ---------------------------------------------------------------------------

def sync_park_factors(conn, season: int, force: bool = False) -> int:
    """
    Populate the park_factors table with L/R split HR factors.

    Currently loads from the curated seed dataset in etl.park_factors_seed.
    In the future this can try a live Baseball Savant pull first and fall
    back to the seed on failure.

    Refreshes at most once per PARK_FACTORS_REFRESH_DAYS days unless force=True.
    """
    print("\n  [6/6] Syncing park factors...")

    # Skip if we have fresh data
    if not force:
        age = get_park_factors_age_days(conn, season)
        if age is not None and age < PARK_FACTORS_REFRESH_DAYS:
            print(f"    Park factors are {age}d old (< {PARK_FACTORS_REFRESH_DAYS}d), skipping.")
            return 0

    # TODO: try a live pull from Baseball Savant or FanGraphs here.
    # For now, always use the seed.
    df = get_park_seed_df()
    source = "seed"
    print(f"    Loaded {len(df)} venues from seed ({source})")

    inserted = 0
    for _, row in df.iterrows():
        conn.execute("""
            INSERT OR REPLACE INTO park_factors
            (venue, season, hr_pf_overall, hr_pf_lhb, hr_pf_rhb, source, notes, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """, (
            row["venue"], season,
            float(row["hr_pf_overall"]),
            float(row["hr_pf_lhb"]),
            float(row["hr_pf_rhb"]),
            source,
            row.get("notes", ""),
        ))
        inserted += 1

    conn.commit()
    print(f"  [6/6] Done. {inserted} park factors written.")
    return inserted


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_nightly(date_str: str, backfill: bool = False):
    """Run the full nightly ETL pipeline."""
    season = int(date_str[:4])

    print("=" * 60)
    print(f"  NIGHTLY ETL — {date_str}")
    print(f"  Mode: {'BACKFILL' if backfill else 'INCREMENTAL'}")
    print("=" * 60)

    conn = get_db()
    create_tables(conn)
    log_id = log_etl_start(conn, "nightly", date_str)

    total_rows = 0
    # For backfill, process all seasons; otherwise just current
    seasons = list(range(2024, season + 1)) if backfill else [season]

    try:
        # Step 1: Statcast HR events (handles multi-season internally)
        n = sync_batter_hr_events(conn, season, backfill=backfill)
        total_rows += n

        # Step 2: Pitcher arsenals
        for s in seasons:
            n = sync_pitcher_arsenals(conn, s, force=backfill)
            total_rows += n

        # Step 3: Victim profiles (no API calls — computed from DB)
        n = recompute_victim_profiles(conn, season)
        total_rows += n

        # Step 4: Season batting (all seasons on backfill)
        for s in seasons:
            n = sync_season_batting(conn, s, date_str)
            total_rows += n

        # Step 5: Season pitching (all seasons on backfill)
        for s in seasons:
            n = sync_season_pitching(conn, s)
            total_rows += n

        # Step 6: Park factors (all seasons on backfill)
        for s in seasons:
            n = sync_park_factors(conn, s, force=backfill)
            total_rows += n

        # Step 7: Park archetype snapshot for today (Phase 2, 2026-05-25).
        # Refreshes batter_park_archetype with a row keyed on
        # (player_id, date_through = date_str) for every batter who has
        # at least one HR strictly before date_str. Fast -- no Statcast
        # pulls; just joins batter_hr_events + daily_slate. Idempotent
        # INSERT OR REPLACE so re-running the nightly is safe.
        n = sync_park_archetype(conn, date_str)
        total_rows += n

        log_etl_complete(conn, log_id, rows=total_rows)
        print(f"\n{'=' * 60}")
        print(f"  NIGHTLY ETL COMPLETE — {total_rows} total rows affected")
        print(f"{'=' * 60}")

        # Print summary
        print(f"\n  Database summary:")
        for table in ["batter_hr_events", "pitcher_arsenals", "victim_profiles",
                       "season_batting", "season_pitching", "park_factors",
                       "batter_park_archetype"]:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"    {table:<24} {count:>8} rows")
        print()

    except Exception as e:
        log_etl_fail(conn, log_id, str(e))
        print(f"\n  ETL FAILED: {e}")
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Nightly ETL for HR Bets")
    parser.add_argument("--date", default="today", help="Date context (YYYY-MM-DD)")
    parser.add_argument("--backfill", action="store_true", help="Full 18-month backfill")
    args = parser.parse_args()

    if args.date == "today":
        date_str = datetime.now().strftime("%Y-%m-%d")
    else:
        date_str = args.date

    run_nightly(date_str, backfill=args.backfill)


if __name__ == "__main__":
    main()
