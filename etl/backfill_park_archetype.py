#!/usr/bin/env python3
"""
backfill_park_archetype.py - populate batter_park_archetype across the
2025 season for the Phase 2 backtest harness.

For each as-of date in [--start, --end]:

  1. Find every batter with at least one HR strictly before that date
     (joined against daily_slate so we only count HRs with a resolvable
     venue -- the unresolvable ones can't enter the centroid anyway).
  2. Call features_v2.compute_batter_park_archetype with as_of_date set
     to that date. The builder honors PARK_ARCHETYPE_MIN_HRS and returns
     None for batters below the threshold -- those rows ARE persisted
     (centroid_json = NULL, n_hrs_used populated) so the harness's
     threshold sweep can read them back.
  3. INSERT OR REPLACE rows into batter_park_archetype keyed on
     (player_id, date_through). Idempotent -- safe to re-run.

This is FAST. No fresh Statcast pulls. The builder only reads existing
batter_hr_events + daily_slate tables and centroids the 6-element park
feature vector. Expected runtime: ~seconds-per-date for 100s-of-batter
slates; the 188-date full 2025 season should complete in well under
30 minutes single-machine.

Where this fits relative to backfill_2025.py: that orchestrator drives
the full score-and-persist pipeline for daily_picks + pick_inputs.
This script is narrower -- it only writes batter_park_archetype rows.
The pick_inputs.park_archetype_centroid_json column gets populated by
load_picks_to_db when daily_picks reruns through generate_card, so a
typical flow is:

    python -m etl.backfill_park_archetype       # populate batter_park_archetype
    python -m etl.backfill_2025 --force        # rerun pick_inputs with the centroids

But the harness works against batter_park_archetype directly too -- it
reads centroids via the JOIN in fetch_rows, so the pick_inputs column
is only a perf optimization (avoid a JOIN on each query).

Usage:
    # Default: full 2025 regular season
    python -m etl.backfill_park_archetype

    # Sub-window for testing
    python -m etl.backfill_park_archetype --start 2025-06-01 --end 2025-06-05

    # Chunk by date count
    python -m etl.backfill_park_archetype --max-dates 20

    # Chunk by wall-clock time
    python -m etl.backfill_park_archetype --max-runtime 30m

    # Custom DB
    python -m etl.backfill_park_archetype --db /path/to/custom.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# Make project root importable from etl/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from etl.db import DB_PATH, create_tables, get_db
from features_v2 import compute_batter_park_archetype


# 2025 regular season window. Matches backfill_2025.py.
DEFAULT_START = "2025-03-27"
DEFAULT_END = "2025-09-30"


def _date_range(start: str, end: str):
    sd = datetime.strptime(start, "%Y-%m-%d").date()
    ed = datetime.strptime(end, "%Y-%m-%d").date()
    cur = sd
    while cur <= ed:
        yield cur.strftime("%Y-%m-%d")
        cur += timedelta(days=1)


def parse_duration(s: str | None) -> float | None:
    """Parse a duration string like '3h', '90m', '1h30m', '7200' into seconds.

    Mirrors backfill_2025.parse_duration so the two scripts have the same
    chunking flag semantics.
    """
    if s is None:
        return None
    s = s.strip().lower()
    if not s:
        return None
    if s.isdigit():
        return float(s)
    total = 0.0
    rest = s
    units = {"h": 3600, "m": 60, "s": 1}
    while rest:
        idx = next((i for i, c in enumerate(rest) if c in units), None)
        if idx is None or idx == 0:
            raise ValueError(
                f"bad duration {s!r}; expected forms like '3h', '90m', '1h30m'"
            )
        num_str, unit_char, rest = rest[:idx], rest[idx], rest[idx + 1:]
        try:
            total += float(num_str) * units[unit_char]
        except ValueError:
            raise ValueError(
                f"bad duration {s!r}; '{num_str}' is not a number"
            ) from None
    return total


def _hms(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def _eligible_batters_through(conn: sqlite3.Connection, cutoff: str) -> list[int]:
    """Batter IDs with at least one HR strictly before *cutoff*.

    We pre-filter on (HRs > 0) so the builder isn't called on millions of
    no-HR batters. The builder still enforces PARK_ARCHETYPE_MIN_HRS for
    the centroid; we just skip the all-zero rows.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT batter_id
        FROM batter_hr_events
        WHERE game_date < ?
          AND batter_id IS NOT NULL
          AND batter_id > 0
        """,
        (cutoff,),
    ).fetchall()
    return [int(r[0]) for r in rows]


def backfill_one_date(
    conn: sqlite3.Connection,
    date_str: str,
    db_path: str,
) -> dict:
    """Compute + persist centroids for every eligible batter as-of date_str.

    Returns counts: {batters, with_centroid, below_threshold, elapsed_s}.
    """
    t0 = time.time()
    batters = _eligible_batters_through(conn, date_str)
    if not batters:
        return {
            "batters": 0,
            "with_centroid": 0,
            "below_threshold": 0,
            "elapsed_s": time.time() - t0,
        }

    result = compute_batter_park_archetype(
        player_ids=batters,
        as_of_date=date_str,
        db_path=db_path,
    )

    # INSERT OR REPLACE -- idempotent on (player_id, date_through).
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
    conn.commit()
    return {
        "batters": len(batters),
        "with_centroid": n_with,
        "below_threshold": n_below,
        "elapsed_s": time.time() - t0,
    }


def backfill_window(
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
    db_path: Path | None = None,
    *,
    max_dates: int | None = None,
    max_runtime_s: float | None = None,
) -> dict:
    """Walk every date in [start, end] and persist centroids.

    *max_dates* / *max_runtime_s* mirror backfill_2025's chunking. Re-running
    with the same window is safe -- INSERT OR REPLACE is idempotent.
    """
    conn = get_db(db_path)
    create_tables(conn)

    actual_db_path = str(db_path) if db_path else str(DB_PATH)

    summary = {
        "start": start, "end": end,
        "dates_run": 0,
        "total_batters": 0,
        "total_with_centroid": 0,
        "total_below_threshold": 0,
        "stopped_reason": "completed",
        "last_completed": None,
    }
    t_window_start = time.time()

    try:
        all_dates = list(_date_range(start, end))
        n_total = len(all_dates)
        for i, date_str in enumerate(all_dates):
            # Budget check -- BEFORE processing each date.
            if max_dates is not None and summary["dates_run"] >= max_dates:
                summary["stopped_reason"] = f"max_dates={max_dates} reached"
                print(f"\n  [STOP] {summary['stopped_reason']}")
                break
            if (max_runtime_s is not None
                    and (time.time() - t_window_start) >= max_runtime_s):
                summary["stopped_reason"] = (
                    f"max_runtime={_hms(max_runtime_s)} elapsed"
                )
                print(f"\n  [STOP] {summary['stopped_reason']}")
                break

            elapsed_min = (time.time() - t_window_start) / 60
            if i > 0 and summary["dates_run"] > 0:
                avg = elapsed_min / summary["dates_run"]
                eta_min = avg * (n_total - i)
                tag = f"elapsed {elapsed_min:.1f}m, ETA {eta_min:.1f}m"
            else:
                tag = "starting"
            print(f"[{i + 1}/{n_total}] {date_str}  ({tag})")

            try:
                r = backfill_one_date(conn, date_str, actual_db_path)
            except KeyboardInterrupt:
                summary["stopped_reason"] = "user interrupt"
                print(f"\n  [INTERRUPT] stopped at {date_str}.")
                raise
            except Exception as e:
                print(f"  [ERROR] {date_str}: {type(e).__name__}: {e}")
                continue

            summary["dates_run"] += 1
            summary["total_batters"] += r["batters"]
            summary["total_with_centroid"] += r["with_centroid"]
            summary["total_below_threshold"] += r["below_threshold"]
            summary["last_completed"] = date_str
            print(
                f"  {r['batters']} batters | "
                f"{r['with_centroid']} centroids | "
                f"{r['below_threshold']} below-threshold "
                f"({r['elapsed_s']:.1f}s)"
            )
    finally:
        conn.close()

    return summary


def main():
    ap = argparse.ArgumentParser(
        description=(
            "Backfill batter_park_archetype for the 2025 season. "
            "Idempotent INSERT OR REPLACE -- safe to re-run."
        ),
    )
    ap.add_argument("--start", default=DEFAULT_START,
                    help=f"start date YYYY-MM-DD (default: {DEFAULT_START})")
    ap.add_argument("--end", default=DEFAULT_END,
                    help=f"end date YYYY-MM-DD (default: {DEFAULT_END})")
    ap.add_argument("--max-dates", type=int, default=None, metavar="N",
                    help="Stop after N dates have been processed.")
    ap.add_argument("--max-runtime", type=str, default=None, metavar="DURATION",
                    help="Stop after a wall-clock budget elapses ('3h', '30m', "
                         "'1h30m', or seconds as int).")
    ap.add_argument("--db", default=None, help="Optional alternate DB path")
    args = ap.parse_args()

    db_path = Path(args.db) if args.db else None
    max_runtime_s = parse_duration(args.max_runtime)

    print(f"=== Park-archetype backfill {args.start} -> {args.end} ===\n")

    summary = backfill_window(
        start=args.start, end=args.end,
        db_path=db_path,
        max_dates=args.max_dates,
        max_runtime_s=max_runtime_s,
    )

    print()
    print("=" * 70)
    print("  PARK-ARCHETYPE BACKFILL SUMMARY")
    print("=" * 70)
    for k, v in summary.items():
        print(f"  {k:<24}  {v}")


if __name__ == "__main__":
    main()
