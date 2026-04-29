#!/usr/bin/env python3
"""
export_site_data.py — Export DB data to static JSON for the Netlify dashboard.

Reads from the SQLite database and writes JSON files that the frontend
can fetch() at runtime. Run this after generate_picks.py each day,
then git push to deploy.

Usage:
    python export_site_data.py                     # export to mlb_hr_bet_site/data/
    python export_site_data.py --out ./docs/data   # custom output dir
    python export_site_data.py --days 30           # last 30 days of history

Output files:
    picks_latest.json      — today's card + full board
    picks_history.json     — daily pick results with outcomes
    performance.json       — aggregate stats (hit rates, streaks, factor analysis)
    factor_trends.json     — per-factor score averages over time (for charts)
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from etl.db import get_db, create_tables


def export_latest_picks(conn, out_dir: Path):
    """Export the most recent day's picks and full board."""

    # Find the latest date with picks
    row = conn.execute(
        "SELECT MAX(date) FROM daily_picks WHERE selected = 1"
    ).fetchone()
    latest_date = row[0] if row and row[0] else None

    if not latest_date:
        # Fall back to JSON results directory
        results_dir = Path(__file__).parent.parent / "results"
        json_files = sorted(results_dir.glob("picks_*.json"), reverse=True)
        if json_files:
            with open(json_files[0]) as f:
                data = json.load(f)
            with open(out_dir / "picks_latest.json", "w") as f:
                json.dump(data, f, indent=2)
            print(f"  Exported picks_latest.json from {json_files[0].name}")
            return
        print("  WARNING: No picks found in DB or results/")
        return

    # Selected picks (the card)
    picks = conn.execute("""
        SELECT batter_name, team, tier, tier_label, opp_pitcher,
               composite, power_score, matchup_score, park_score,
               form_score, weather_score, lineup_score, batting_order,
               matchup_version, game_pk, selected, rank_in_board
        FROM daily_picks
        WHERE date = ? AND selected = 1
        ORDER BY composite DESC
    """, (latest_date,)).fetchall()

    # Full board
    board = conn.execute("""
        SELECT batter_name, team, tier, tier_label, opp_pitcher,
               composite, power_score, matchup_score, park_score,
               form_score, weather_score, lineup_score, batting_order,
               selected
        FROM daily_picks
        WHERE date = ?
        ORDER BY composite DESC
    """, (latest_date,)).fetchall()

    data = {
        "date": latest_date,
        "generated_at": datetime.now().isoformat(),
        "picks": [dict(r) for r in picks],
        "full_board": [dict(r) for r in board],
    }

    with open(out_dir / "picks_latest.json", "w") as f:
        json.dump(data, f, indent=2)
    print(f"  Exported picks_latest.json ({len(picks)} picks, {len(board)} board)")


def export_history(conn, out_dir: Path, days: int = 60):
    """Export daily pick history with outcomes."""

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    rows = conn.execute("""
        SELECT
            p.date,
            p.batter_name AS name,
            p.team,
            p.tier,
            p.tier_label,
            p.opp_pitcher,
            p.composite,
            p.power_score,
            p.matchup_score,
            p.park_score,
            p.form_score,
            p.weather_score,
            p.lineup_score,
            p.batting_order,
            COALESCE(o.hr_count, -1) AS hr_count,
            COALESCE(o.ab, 0) AS ab,
            CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END AS hit
        FROM daily_picks p
        LEFT JOIN outcomes o ON p.date = o.date AND p.batter_id = o.batter_id
        WHERE p.selected = 1 AND p.date >= ?
        ORDER BY p.date DESC, p.composite DESC
    """, (cutoff,)).fetchall()

    # Group by date
    by_date = {}
    for r in rows:
        d = r["date"]
        if d not in by_date:
            by_date[d] = {"date": d, "picks": [], "total": 0, "hits": 0}
        entry = dict(r)
        del entry["date"]
        by_date[d]["picks"].append(entry)
        by_date[d]["total"] += 1
        if r["hr_count"] > 0:
            by_date[d]["hits"] += 1

    # Add hit_rate per day
    history = []
    for d in sorted(by_date.keys(), reverse=True):
        day = by_date[d]
        day["hit_rate"] = round(day["hits"] / max(day["total"], 1) * 100, 1)
        # Check if outcomes are available (-1 means no outcome data)
        day["outcomes_available"] = any(
            p["hr_count"] >= 0 for p in day["picks"]
        )
        history.append(day)

    data = {
        "days": days,
        "total_days": len(history),
        "history": history,
        "exported_at": datetime.now().isoformat(),
    }

    with open(out_dir / "picks_history.json", "w") as f:
        json.dump(data, f, indent=2)
    print(f"  Exported picks_history.json ({len(history)} days)")


def export_performance(conn, out_dir: Path):
    """Export aggregate performance stats."""

    # Overall hit rate
    overall = conn.execute("""
        SELECT
            COUNT(*) AS total_picks,
            SUM(CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END) AS hits
        FROM daily_picks p
        JOIN outcomes o ON p.date = o.date AND p.batter_id = o.batter_id
        WHERE p.selected = 1
    """).fetchone()

    total = overall["total_picks"] or 0
    hits = overall["hits"] or 0

    # By tier
    tier_rows = conn.execute("""
        SELECT
            p.tier_label,
            COUNT(*) AS total,
            SUM(CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END) AS hits
        FROM daily_picks p
        JOIN outcomes o ON p.date = o.date AND p.batter_id = o.batter_id
        WHERE p.selected = 1 AND p.tier_label IS NOT NULL
        GROUP BY p.tier_label
    """).fetchall()

    # By matchup version
    version_rows = conn.execute("""
        SELECT
            COALESCE(p.matchup_version, 'v1') AS version,
            COUNT(*) AS total,
            SUM(CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END) AS hits
        FROM daily_picks p
        JOIN outcomes o ON p.date = o.date AND p.batter_id = o.batter_id
        WHERE p.selected = 1
        GROUP BY version
    """).fetchall()

    # Composite score for hits vs misses
    composite_row = conn.execute("""
        SELECT
            ROUND(AVG(CASE WHEN o.hr_count > 0 THEN p.composite END), 1) AS avg_hit,
            ROUND(AVG(CASE WHEN o.hr_count = 0 THEN p.composite END), 1) AS avg_miss
        FROM daily_picks p
        JOIN outcomes o ON p.date = o.date AND p.batter_id = o.batter_id
        WHERE p.selected = 1
    """).fetchone()

    # Top hitters (most HR hits from our picks)
    top_hitters = conn.execute("""
        SELECT
            p.batter_name AS name,
            p.team,
            COUNT(*) AS times_picked,
            SUM(CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END) AS times_hit,
            ROUND(AVG(p.composite), 1) AS avg_composite
        FROM daily_picks p
        JOIN outcomes o ON p.date = o.date AND p.batter_id = o.batter_id
        WHERE p.selected = 1
        GROUP BY p.batter_id
        HAVING times_picked >= 3
        ORDER BY times_hit DESC, avg_composite DESC
        LIMIT 15
    """).fetchall()

    # Current streak (consecutive days with at least 1 hit)
    daily_hits = conn.execute("""
        SELECT p.date,
               SUM(CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END) AS hits
        FROM daily_picks p
        JOIN outcomes o ON p.date = o.date AND p.batter_id = o.batter_id
        WHERE p.selected = 1
        GROUP BY p.date
        ORDER BY p.date DESC
    """).fetchall()

    streak = 0
    for r in daily_hits:
        if r["hits"] > 0:
            streak += 1
        else:
            break

    data = {
        "overall": {
            "total_picks": total,
            "hits": hits,
            "hit_rate": round(hits / max(total, 1) * 100, 1),
        },
        "by_tier": [dict(r) for r in tier_rows],
        "by_matchup_version": [dict(r) for r in version_rows],
        "composite_analysis": {
            "avg_hit_composite": composite_row["avg_hit"],
            "avg_miss_composite": composite_row["avg_miss"],
        },
        "top_hitters": [dict(r) for r in top_hitters],
        "current_hit_streak_days": streak,
        "exported_at": datetime.now().isoformat(),
    }

    with open(out_dir / "performance.json", "w") as f:
        json.dump(data, f, indent=2)
    print(f"  Exported performance.json ({total} picks, {hits} hits)")


def export_factor_trends(conn, out_dir: Path, days: int = 30):
    """Export per-factor daily averages for trend charts."""

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    rows = conn.execute("""
        SELECT
            p.date,
            ROUND(AVG(p.power_score), 1) AS avg_power,
            ROUND(AVG(p.matchup_score), 1) AS avg_matchup,
            ROUND(AVG(p.park_score), 1) AS avg_park,
            ROUND(AVG(p.form_score), 1) AS avg_form,
            ROUND(AVG(p.weather_score), 1) AS avg_weather,
            ROUND(AVG(p.lineup_score), 1) AS avg_lineup,
            ROUND(AVG(p.composite), 1) AS avg_composite,
            COUNT(*) AS n_picks,
            SUM(CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END) AS hits
        FROM daily_picks p
        LEFT JOIN outcomes o ON p.date = o.date AND p.batter_id = o.batter_id
        WHERE p.selected = 1 AND p.date >= ?
        GROUP BY p.date
        ORDER BY p.date
    """, (cutoff,)).fetchall()

    data = {
        "days": days,
        "trends": [dict(r) for r in rows],
        "exported_at": datetime.now().isoformat(),
    }

    with open(out_dir / "factor_trends.json", "w") as f:
        json.dump(data, f, indent=2)
    print(f"  Exported factor_trends.json ({len(rows)} days)")


def main():
    parser = argparse.ArgumentParser(description="Export DB data to static JSON for Netlify")
    parser.add_argument("--out", default=None, help="Output directory (default: mlb_hr_bet_site/data/)")
    parser.add_argument("--days", type=int, default=60, help="Days of history to export (default: 60)")
    parser.add_argument("--db", default=None, help="Custom DB path")
    args = parser.parse_args()

    if args.out:
        out_dir = Path(args.out)
    else:
        out_dir = Path(__file__).parent / "mlb_hr_bet_site" / "data"

    out_dir.mkdir(parents=True, exist_ok=True)

    conn = get_db(args.db)
    create_tables(conn)

    print(f"\n  EXPORT SITE DATA → {out_dir}")
    print("=" * 50)

    export_latest_picks(conn, out_dir)
    export_history(conn, out_dir, days=args.days)
    export_performance(conn, out_dir)
    export_factor_trends(conn, out_dir, days=args.days)

    print(f"\n  Done. Files ready in {out_dir}/")
    print("  Next: git add mlb_hr_bet_site/data/ && git commit && git push")

    conn.close()


if __name__ == "__main__":
    main()
