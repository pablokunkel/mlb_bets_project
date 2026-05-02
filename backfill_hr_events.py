#!/usr/bin/env python3
"""
backfill_hr_events.py — One-time backfill of per-HR Statcast events.

Walks completed games' playByPlay endpoints (free MLB Stats API) and
persists each home run with full Statcast detail (coordX/Y, launch
speed/angle, total distance, trajectory, location, pitcher attribution)
into the `hr_events` SQLite table.

This is the historical counterpart to the live `dingersonly-live-hr`
Cloudflare Worker, which does the same job for today's games but writes
to KV with 36-hour TTL. The two are complementary — the worker handles
real-time HRs as they happen; this script populates the table for past
days the worker never saw.

Idempotent: UNIQUE(game_pk, at_bat_index) + INSERT OR IGNORE means
re-running for the same date is a no-op (writes zero new rows).

Usage:
    # Backfill every date that has rows in `outcomes` but no rows in
    # `hr_events`. This is the typical first run after PR #5a merges.
    python backfill_hr_events.py

    # Explicit inclusive date range (e.g., re-fetch a date because
    # MLB later updated a play's hitData).
    python backfill_hr_events.py --from-date 2026-04-01 --to-date 2026-05-01

    # Dry run — print dates that would be processed, no DB writes
    python backfill_hr_events.py --dry-run

    # Force re-fetch even if hr_events already has rows for the date.
    # The INSERT OR IGNORE still prevents duplicate (gpk, abi) rows; this
    # just re-walks playByPlay so you'd pick up newly-added Statcast
    # fields if MLB backfilled them server-side.
    python backfill_hr_events.py --force --from-date 2026-04-01 --to-date 2026-04-30

Estimated runtime: ~1 sec/game * ~15 games/day * N days. The 37-day
window currently in hr_recap.json takes ~9 minutes sequential.

Exit codes:
    0  success
    1  no dates needed backfill (still success)
    2  partial failure (some dates errored)
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from etl.db import get_db, create_tables
from etl.etl_outcomes import fetch_hr_events_for_date


def find_missing_dates(conn) -> list[str]:
    """Dates in `outcomes` that have NO rows in `hr_events`."""
    rows = conn.execute("""
        SELECT DISTINCT o.date
        FROM outcomes o
        WHERE o.date NOT IN (SELECT DISTINCT date FROM hr_events)
        ORDER BY o.date
    """).fetchall()
    return [r[0] for r in rows]


def date_range(from_date: str, to_date: str) -> list[str]:
    s = datetime.strptime(from_date, "%Y-%m-%d")
    e = datetime.strptime(to_date, "%Y-%m-%d")
    if e < s:
        raise ValueError(f"to-date {to_date} is before from-date {from_date}")
    out = []
    cur = s
    while cur <= e:
        out.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill hr_events from MLB Stats API playByPlay.",
    )
    parser.add_argument("--from-date", help="Inclusive start date YYYY-MM-DD (requires --to-date)")
    parser.add_argument("--to-date", help="Inclusive end date YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print dates that would be processed, no writes")
    parser.add_argument("--force", action="store_true",
                        help="Re-walk playByPlay even if hr_events has rows for the date "
                             "(INSERT OR IGNORE still prevents dupes)")
    args = parser.parse_args()

    if (args.from_date and not args.to_date) or (args.to_date and not args.from_date):
        parser.error("--from-date and --to-date must be used together")

    conn = get_db()
    create_tables(conn)

    if args.from_date and args.to_date:
        try:
            dates = date_range(args.from_date, args.to_date)
        except ValueError as e:
            print(f"ERROR: {e}")
            return 2
        if not args.force:
            # Filter to only those that actually need backfill
            existing = {r[0] for r in conn.execute(
                "SELECT DISTINCT date FROM hr_events"
            ).fetchall()}
            dates = [d for d in dates if d not in existing]
    else:
        dates = find_missing_dates(conn)

    if not dates:
        print("No dates need backfill — hr_events is up to date with outcomes.")
        conn.close()
        return 1

    if args.dry_run:
        print(f"Would backfill {len(dates)} dates:")
        for d in dates:
            print(f"  {d}")
        conn.close()
        return 0

    print(f"Backfilling hr_events for {len(dates)} date(s)...")
    if len(dates) > 20:
        print(f"  (estimated runtime: ~{len(dates) * 15 // 60} min sequential)")
    print()

    grand_total = 0
    failed_dates: list[str] = []
    for d in dates:
        try:
            n = fetch_hr_events_for_date(conn, d)
            print(f"  [{d}] {n} HR events")
            grand_total += n
        except Exception as e:
            failed_dates.append(d)
            print(f"  [{d}] FAILED: {e}")

    print()
    print(f"Done. {grand_total} total HR events recorded across {len(dates)} dates.")
    if failed_dates:
        print(f"FAILED: {len(failed_dates)} dates errored: {failed_dates}")
        conn.close()
        return 2

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
