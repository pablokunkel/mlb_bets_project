#!/usr/bin/env python3
"""
etl_outcomes.py — Outcome tracking for Daily HR Bet.

Runs at ~1:00 AM (day after games). Fetches box scores for yesterday's
picks and records which batters actually hit home runs.

Also supports backfilling outcomes for historical picks.

Usage:
    # Yesterday's outcomes
    python -m etl.etl_outcomes

    # Specific date
    python -m etl.etl_outcomes --date 2026-04-07

    # Backfill all dates with picks but no outcomes
    python -m etl.etl_outcomes --backfill

    # Performance report
    python -m etl.etl_outcomes --report
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from etl.db import (
    get_db, create_tables,
    log_etl_start, log_etl_complete, log_etl_fail,
)

MLB_API = "https://statsapi.mlb.com/api/v1"


# ---------------------------------------------------------------------------
# Fetch outcomes from box scores
# ---------------------------------------------------------------------------

def fetch_outcomes_for_date(conn, date_str: str) -> int:
    """
    Fetch box scores for all games on a date and record HR outcomes
    for every batter in our daily_picks.
    """
    # Get games from our slate
    games = conn.execute(
        "SELECT game_pk FROM daily_slate WHERE date = ?", (date_str,)
    ).fetchall()

    if not games:
        # Try fetching schedule directly if no slate exists
        url = f"{MLB_API}/schedule"
        params = {"sportId": 1, "date": date_str}
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            game_pks = []
            for d in resp.json().get("dates", []):
                for g in d.get("games", []):
                    if g["status"]["detailedState"] in ("Final", "Game Over"):
                        game_pks.append(g["gamePk"])
            games = [{"game_pk": gpk} for gpk in game_pks]
        except Exception as e:
            print(f"    ERROR fetching schedule: {e}")
            return 0

    print(f"    {len(games)} games to process for {date_str}")

    # Get all batters we picked or scored that day
    picks = conn.execute(
        "SELECT DISTINCT batter_id, batter_name, game_pk FROM daily_picks WHERE date = ?",
        (date_str,)
    ).fetchall()

    # Also check JSON results directory for backward compatibility
    picked_ids = {r["batter_id"] for r in picks}
    results_dir = Path(__file__).parent.parent.parent / "results"
    json_file = results_dir / f"picks_{date_str}.json"
    if json_file.exists() and not picked_ids:
        try:
            with open(json_file) as f:
                data = json.load(f)
            for p in data.get("picks", []):
                pid = p.get("player_id", 0)
                if pid:
                    picked_ids.add(pid)
        except Exception:
            pass

    inserted = 0
    for game_row in games:
        gpk = game_row["game_pk"] if isinstance(game_row, dict) else game_row[0]

        try:
            url = f"{MLB_API}/game/{gpk}/boxscore"
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            box = resp.json()
        except Exception:
            continue

        # Extract batting stats for every player
        for side in ["home", "away"]:
            team_data = box.get("teams", {}).get(side, {})
            players = team_data.get("players", {})

            for player_key, player_data in players.items():
                person = player_data.get("person", {})
                pid = person.get("id", 0)
                stats = player_data.get("stats", {})
                batting = stats.get("batting", {})

                # Only record outcomes for batters who had at-bats
                ab = batting.get("atBats", 0)
                if ab == 0:
                    continue

                hr_count = batting.get("homeRuns", 0)
                hits = batting.get("hits", 0)
                rbi = batting.get("rbi", 0)
                doubles = batting.get("doubles", 0)
                triples = batting.get("triples", 0)
                # Total bases: derive from singles + 2*2B + 3*3B + 4*HR.
                # MLB Stats API also returns totalBases directly; prefer it
                # when present, otherwise derive.
                total_bases = batting.get("totalBases")
                if total_bases is None:
                    singles = max(0, hits - doubles - triples - hr_count)
                    total_bases = singles + 2 * doubles + 3 * triples + 4 * hr_count

                try:
                    conn.execute("""
                        INSERT OR REPLACE INTO outcomes
                        (date, batter_id, batter_name, game_pk,
                         ab, hits, hr_count, rbi,
                         doubles, triples, total_bases)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        date_str, pid, person.get("fullName", ""),
                        gpk, ab, hits, hr_count, rbi,
                        doubles, triples, total_bases,
                    ))
                    inserted += 1
                except Exception:
                    pass

    conn.commit()
    return inserted


# ---------------------------------------------------------------------------
# Backfill from JSON results
# ---------------------------------------------------------------------------

def backfill_outcomes(conn):
    """Find all dates with picks but no outcomes and fetch them."""
    results_dir = Path(__file__).parent.parent.parent / "results"
    if not results_dir.exists():
        print("  No results directory found.")
        return

    # Find all pick dates
    pick_dates = set()
    for fp in results_dir.glob("picks_*.json"):
        date_part = fp.stem.replace("picks_", "")
        if len(date_part) == 10:  # YYYY-MM-DD
            pick_dates.add(date_part)

    # Also check DB for pick dates
    db_dates = conn.execute(
        "SELECT DISTINCT date FROM daily_picks"
    ).fetchall()
    pick_dates.update(r[0] for r in db_dates)

    # Find dates missing outcomes
    existing = conn.execute(
        "SELECT DISTINCT date FROM outcomes"
    ).fetchall()
    existing_dates = {r[0] for r in existing}

    missing = sorted(pick_dates - existing_dates)
    print(f"  Found {len(missing)} dates needing outcomes: {missing}")

    for date_str in missing:
        print(f"\n  Processing {date_str}...")
        n = fetch_outcomes_for_date(conn, date_str)
        print(f"    Recorded {n} player outcomes")


# ---------------------------------------------------------------------------
# Performance report
# ---------------------------------------------------------------------------

def print_performance_report(conn):
    """Print overall model performance stats."""
    print()
    print("=" * 70)
    print("  MODEL PERFORMANCE REPORT")
    print("=" * 70)

    # Overall hit rate (selected picks only)
    row = conn.execute("""
        SELECT
            COUNT(*) as total_picks,
            SUM(CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END) as hits,
            ROUND(100.0 * SUM(CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) as hit_rate
        FROM daily_picks p
        JOIN outcomes o ON p.date = o.date AND p.batter_id = o.batter_id
        WHERE p.selected = 1
    """).fetchone()

    if not row or row["total_picks"] == 0:
        # Try joining with non-selected picks too (backward compat)
        row = conn.execute("""
            SELECT COUNT(DISTINCT p.date || '-' || p.batter_id) as total_picks,
                   SUM(CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END) as hits
            FROM daily_picks p
            JOIN outcomes o ON p.date = o.date AND p.batter_id = o.batter_id
        """).fetchone()

    if row and row["total_picks"] and row["total_picks"] > 0:
        print(f"\n  Overall:  {row['hits']}/{row['total_picks']} picks hit "
              f"({100*row['hits']/row['total_picks']:.1f}%)")
    else:
        print("\n  No matched pick/outcome data yet.")
        print("  Run with --backfill first to populate outcomes.\n")
        return

    # Hit rate by date
    print(f"\n  {'Date':<14} {'Picks':>6} {'Hits':>6} {'Rate':>7}")
    print(f"  {'-' * 36}")

    rows = conn.execute("""
        SELECT p.date,
               COUNT(*) as picks,
               SUM(CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END) as hits
        FROM daily_picks p
        JOIN outcomes o ON p.date = o.date AND p.batter_id = o.batter_id
        WHERE p.selected = 1
        GROUP BY p.date
        ORDER BY p.date
    """).fetchall()

    for r in rows:
        rate = 100 * r["hits"] / r["picks"] if r["picks"] > 0 else 0
        bar = "*" * r["hits"]
        print(f"  {r['date']:<14} {r['picks']:>6} {r['hits']:>6} {rate:>6.1f}%  {bar}")

    # Hit rate by tier
    print(f"\n  {'Tier':<16} {'Picks':>6} {'Hits':>6} {'Rate':>7}")
    print(f"  {'-' * 38}")

    rows = conn.execute("""
        SELECT p.tier_label,
               COUNT(*) as picks,
               SUM(CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END) as hits
        FROM daily_picks p
        JOIN outcomes o ON p.date = o.date AND p.batter_id = o.batter_id
        WHERE p.selected = 1 AND p.tier_label IS NOT NULL
        GROUP BY p.tier_label
        ORDER BY p.tier_label
    """).fetchall()

    for r in rows:
        rate = 100 * r["hits"] / r["picks"] if r["picks"] > 0 else 0
        print(f"  {r['tier_label']:<16} {r['picks']:>6} {r['hits']:>6} {rate:>6.1f}%")

    # Hit rate by matchup version (v1 vs v2)
    rows = conn.execute("""
        SELECT COALESCE(p.matchup_version, 'unknown') as version,
               COUNT(*) as picks,
               SUM(CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END) as hits
        FROM daily_picks p
        JOIN outcomes o ON p.date = o.date AND p.batter_id = o.batter_id
        WHERE p.selected = 1
        GROUP BY version
    """).fetchall()

    if rows:
        print(f"\n  {'Matchup Version':<16} {'Picks':>6} {'Hits':>6} {'Rate':>7}")
        print(f"  {'-' * 38}")
        for r in rows:
            rate = 100 * r["hits"] / r["picks"] if r["picks"] > 0 else 0
            print(f"  {r['version']:<16} {r['picks']:>6} {r['hits']:>6} {rate:>6.1f}%")

    # Avg composite for hits vs misses
    row = conn.execute("""
        SELECT
            ROUND(AVG(CASE WHEN o.hr_count > 0 THEN p.composite END), 1) as avg_hit_composite,
            ROUND(AVG(CASE WHEN o.hr_count = 0 THEN p.composite END), 1) as avg_miss_composite
        FROM daily_picks p
        JOIN outcomes o ON p.date = o.date AND p.batter_id = o.batter_id
        WHERE p.selected = 1
    """).fetchone()

    if row and row["avg_hit_composite"]:
        print(f"\n  Avg composite (hits):   {row['avg_hit_composite']}")
        print(f"  Avg composite (misses): {row['avg_miss_composite']}")

    print(f"\n{'=' * 70}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_outcomes(date_str: str, backfill: bool = False, report: bool = False):
    conn = get_db()
    create_tables(conn)

    if backfill:
        print("=" * 60)
        print("  OUTCOME BACKFILL")
        print("=" * 60)
        backfill_outcomes(conn)

    elif date_str:
        print("=" * 60)
        print(f"  OUTCOME ETL — {date_str}")
        print("=" * 60)

        log_id = log_etl_start(conn, "outcomes", date_str)
        try:
            n = fetch_outcomes_for_date(conn, date_str)
            log_etl_complete(conn, log_id, rows=n, detail=f"{n} player outcomes")
            print(f"\n  Recorded {n} player outcomes for {date_str}")
        except Exception as e:
            log_etl_fail(conn, log_id, str(e))
            print(f"  FAILED: {e}")

        print(f"  FAILED: {e}")

    if report:
        print_performance_report(conn)

    conn.close()


def run_outcomes_range(from_date, to_date, force=False):
    """
    Loop fetch_outcomes_for_date over an inclusive date range. Idempotent
    via INSERT OR REPLACE. Use to backfill league-wide outcomes (the
    historical raw_data backfill only loaded board batters, not full slate).
    """
    conn = get_db()
    create_tables(conn)

    s = datetime.strptime(from_date, "%Y-%m-%d")
    e = datetime.strptime(to_date, "%Y-%m-%d")
    if e < s:
        print(f"  ERROR: to-date {to_date} is before from-date {from_date}")
        conn.close()
        return

    print("=" * 60)
    print(f"  OUTCOME RANGE ETL: {from_date} -> {to_date}")
    print("=" * 60)

    cur = s
    grand_total = 0
    while cur <= e:
        d = cur.strftime("%Y-%m-%d")
        print(f"\n  [{d}]")
        log_id = log_etl_start(conn, "outcomes", d)
        try:
            n = fetch_outcomes_for_date(conn, d)
            log_etl_complete(conn, log_id, rows=n, detail=f"{n} player outcomes")
            grand_total += n
            print(f"    {n} player outcomes recorded")
        except Exception as ex:
            log_etl_fail(conn, log_id, str(ex))
            print(f"    FAILED: {ex}")
        cur += timedelta(days=1)

    print(f"\n  Range complete: {grand_total} total player-game outcome rows")
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Outcome tracking for HR Bets")
    parser.add_argument("--date", default=None, help="Single date (YYYY-MM-DD)")
    parser.add_argument("--from-date", dest="from_date", default=None,
                        help="Start of date range (YYYY-MM-DD); requires --to-date")
    parser.add_argument("--to-date", dest="to_date", default=None,
                        help="End of date range (YYYY-MM-DD, inclusive)")
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch even if outcomes already exist (idempotent via INSERT OR REPLACE)")
    parser.add_argument("--backfill", action="store_true", help="Backfill all missing outcome dates")
    parser.add_argument("--report", action="store_true", help="Print performance report")
    args = parser.parse_args()

    if args.from_date and args.to_date:
        run_outcomes_range(args.from_date, args.to_date, force=args.force)
        if args.report:
            conn = get_db()
            print_performance_report(conn)
            conn.close()
        return

    if not args.date and not args.backfill and not args.report:
        args.date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    run_outcomes(args.date, backfill=args.backfill, report=args.report)


if __name__ == "__main__":
    main()
