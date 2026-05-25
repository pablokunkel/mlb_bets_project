#!/usr/bin/env python3
"""
backfill_statcast_windows.py - one-off: populate recent_*_21d / _28d
columns in pick_inputs for the existing 2025 backfill window.

Backtest-only -- the nightly ETL still writes only recent_*_14d. If B12
(wider-window backtest) shows 21d or 28d beats 14d in
diagnostics/backtest_power_inputs.py, the nightly fetcher gets wired
to populate these too.

For each date with pick_inputs rows in [start, end]:
  1. Pull Statcast for [date - 28d, date - 1d] (the WIDEST window) -- one call.
  2. Slice that DataFrame to the 21d and 28d sub-windows by `game_date`.
  3. Aggregate per batter via features_v2._aggregate_recent_statcast
     (same exact math as the production _14d fetcher) and re-label the
     output keys with the right suffix.
  4. UPDATE pick_inputs.recent_{barrel_real,xwoba_contact,iso}_{21,28}d
     for each batter on that date.

Skips dates where the 21d + 28d columns are already populated for >70%
of rows -- so the job is resume-safe and a re-run after a crash only
re-pulls the unfinished tail.

Bypasses the features_v2 _14d cache (different (start, end) tuple in
pybaseball cache; fresh pulls expected).

Usage:
    python etl/backfill_statcast_windows.py
    python etl/backfill_statcast_windows.py --start 2025-03-27 --end 2025-09-30
    python etl/backfill_statcast_windows.py --dry-run

Estimated runtime on a cold pybaseball cache: ~30s per date * ~188
dates ~= 90-100 min. Re-run is fast (resume-safe, skips done dates).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
sys.path.insert(0, str(_REPO_ROOT))

from etl.db import DB_PATH, create_tables
from features_v2 import _aggregate_recent_statcast


WINDOWS_TO_BACKFILL = (21, 28)   # _14d already populated by nightly ETL
WIDEST = max(WINDOWS_TO_BACKFILL)


def _statcast_pull(start_str: str, end_str: str):
    """Bulk Statcast over [start, end] inclusive. Returns DataFrame or None."""
    try:
        from pybaseball import statcast
        return statcast(start_dt=start_str, end_dt=end_str, verbose=False)
    except Exception as e:
        print(f"  [statcast] pull {start_str}..{end_str} failed: {e}")
        return None


def _slice_to_window(df, window_start_str: str):
    """Subset DataFrame to rows with game_date >= window_start. ISO date
    string comparison works because Statcast returns YYYY-MM-DD."""
    if df is None or df.empty or "game_date" not in df.columns:
        return df
    return df[df["game_date"].astype(str) >= window_start_str]


def _aggregate_for_suffix(df, suffix: str) -> dict[int, dict]:
    """Run the production _14d aggregator on a sliced DataFrame, then
    relabel the output keys with the right window suffix."""
    raw = _aggregate_recent_statcast(df, min_batted_balls=10)
    out: dict[int, dict] = {}
    for bid, entry in raw.items():
        out[bid] = {k.replace("_14d", f"_{suffix}"): v for k, v in entry.items()}
    return out


def _already_done(conn: sqlite3.Connection, date_str: str) -> bool:
    """True iff the 21d + 28d columns are populated for >70% of rows on
    this date. The threshold is intentionally loose: real coverage will
    be 80-90% (the rest are batters with <10 batted balls in the window,
    legitimately dropped) and a hard 100% gate would never skip anything."""
    r = conn.execute(
        "SELECT COUNT(*) tot, "
        " SUM(CASE WHEN recent_barrel_real_21d IS NOT NULL THEN 1 ELSE 0 END) n21, "
        " SUM(CASE WHEN recent_barrel_real_28d IS NOT NULL THEN 1 ELSE 0 END) n28 "
        "FROM pick_inputs WHERE date = ?",
        (date_str,),
    ).fetchone()
    tot, n21, n28 = r["tot"], r["n21"] or 0, r["n28"] or 0
    return tot > 0 and (n21 / tot) > 0.7 and (n28 / tot) > 0.7


def backfill_one_date(conn: sqlite3.Connection, date_str: str,
                      dry_run: bool = False) -> int:
    """Pull widest window, slice, aggregate, write. Returns # of cell
    updates (1 cell = 1 batter * 1 window-set of columns)."""
    date_dt = datetime.strptime(date_str, "%Y-%m-%d")
    # Window is [date - widest, date - 1d], inclusive on both ends --
    # matches the _14d ETL semantics (games ON `date` are excluded so
    # the rolling stat doesn't leak today's in-progress games).
    last_completed = (date_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    widest_start = (date_dt - timedelta(days=WIDEST)).strftime("%Y-%m-%d")

    df = _statcast_pull(widest_start, last_completed)
    if df is None or df.empty:
        print(f"  {date_str}: empty Statcast pull, skip")
        return 0

    # Slice + aggregate each backfill window.
    suffix_for_window = {w: f"{w}d" for w in WINDOWS_TO_BACKFILL}
    by_batter_by_suffix: dict[str, dict[int, dict]] = {}
    for window, suffix in suffix_for_window.items():
        window_start = (date_dt - timedelta(days=window)).strftime("%Y-%m-%d")
        sliced = _slice_to_window(df, window_start)
        by_batter_by_suffix[suffix] = _aggregate_for_suffix(sliced, suffix)

    n_updated = 0
    if dry_run:
        for suffix, by_b in by_batter_by_suffix.items():
            print(f"  {date_str}: [dry-run] {suffix} -> {len(by_b)} batter rows")
        return sum(len(d) for d in by_batter_by_suffix.values())

    # Merge per-batter so each batter gets ONE UPDATE statement covering
    # both windows -- fewer round-trips, cleaner transaction.
    merged: dict[int, dict] = {}
    for by_b in by_batter_by_suffix.values():
        for bid, entry in by_b.items():
            merged.setdefault(bid, {}).update(entry)

    for bid, entry in merged.items():
        if not entry:
            continue
        cols = list(entry.keys())
        sets = ", ".join(f"{c} = ?" for c in cols)
        vals = [entry[c] for c in cols] + [date_str, bid]
        cur = conn.execute(
            f"UPDATE pick_inputs SET {sets} WHERE date = ? AND batter_id = ?",
            vals,
        )
        n_updated += cur.rowcount
    conn.commit()
    print(f"  {date_str}: pulled {len(df)} pitches, updated {n_updated} batter rows")
    return n_updated


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0].strip()
    )
    ap.add_argument("--start",
                    help="start date YYYY-MM-DD (default: earliest 2025 date in pick_inputs)")
    ap.add_argument("--end",
                    help="end date YYYY-MM-DD (default: latest 2025 date in pick_inputs)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be pulled / updated without touching the DB")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # Idempotent schema migration -- adds the 6 new columns if missing.
    create_tables(conn)

    # Find candidate dates.
    if args.start and args.end:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM pick_inputs WHERE date >= ? AND date <= ? "
            "ORDER BY date", (args.start, args.end)).fetchall()]
    else:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM pick_inputs WHERE date LIKE '2025-%' "
            "ORDER BY date").fetchall()]

    if not dates:
        print("No dates to process.", file=sys.stderr)
        return

    # Resume-safe skip.
    to_process = [d for d in dates if not _already_done(conn, d)]
    skipped = len(dates) - len(to_process)
    print(f"Dates in window: {len(dates)}  to process: {len(to_process)}  "
          f"already populated (skipped): {skipped}")
    if not to_process:
        conn.close()
        return

    t_start = datetime.now()
    for i, d in enumerate(to_process):
        elapsed_min = (datetime.now() - t_start).total_seconds() / 60
        if i > 0:
            avg = elapsed_min / i
            eta_min = avg * (len(to_process) - i)
            tag = f"elapsed {elapsed_min:.0f}m, ETA {eta_min:.0f}m"
        else:
            tag = "starting"
        print(f"[{i + 1}/{len(to_process)}] {d}  ({tag})")
        backfill_one_date(conn, d, dry_run=args.dry_run)

    elapsed_min = (datetime.now() - t_start).total_seconds() / 60
    print(f"\nDone in {elapsed_min:.1f} min.")
    conn.close()


if __name__ == "__main__":
    main()
