#!/usr/bin/env python3
"""
backfill_pitch_type_splits.py - Phase 2 (2026-05-25): one-shot backfill
of batter_pitch_type_splits across the 2025 regular season.

For each date in [--start, --end]:
  1. Find every batter who appeared in daily_lineup on-or-before the date
     (so the snapshot only contains batters who have actually played).
  2. Bulk-pull season Statcast through (date - 1) via
     features_v2.fetch_batter_pitch_type_splits.
  3. INSERT OR REPLACE one row per batter into batter_pitch_type_splits
     keyed on (player_id, date_through=that_date).

Resume-safe: dates where >70% of lineup batters already have a row are
skipped (mirrors etl/backfill_statcast_windows.py's coverage gate).

Usage:
    python -m etl.backfill_pitch_type_splits
    python -m etl.backfill_pitch_type_splits --start 2025-06-01 --end 2025-06-05
    python -m etl.backfill_pitch_type_splits --max-dates 30
    python -m etl.backfill_pitch_type_splits --max-runtime 3h

Estimated runtime cold cache: ~30-60s per date * 188 dates ~= 90-180
minutes. The pybaseball.statcast cache means dates 2-N hit a much faster
path than date 1 (one big season-up-to-date pull per call, cached HTTP
under the hood).

The full 188-date backfill writes ~25,000-40,000 rows (one row per
(active batter, date) snapshot). Total table size: ~5-10 MB.

This script is independent of the daily-picks flow — it doesn't touch
pick_inputs / daily_picks. The persisted batter_pitch_type_splits rows
are later read by generate_picks via load_pitch_type_splits_lookup and
fed onto each batter dict; load_picks_to_db persists the splits to
pick_inputs.fb_slg / br_slg / os_slg / *_pa so backtest_arsenal_inputs.py
can iterate weights without re-pulling Statcast.

See docs/pitch_type_archetype_design.md for the full design.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# Make project root importable from etl/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from etl.db import DB_PATH, create_tables, get_db
from features_v2 import fetch_batter_pitch_type_splits


DEFAULT_START = "2025-03-27"
DEFAULT_END = "2025-09-30"

# Skip a date when more than this share of the date's lineup batters
# already have a batter_pitch_type_splits row at date_through == date.
# Mirrors backfill_statcast_windows._already_done (70%).
COVERAGE_SKIP_THRESHOLD = 0.7


def parse_duration(s: str | None) -> float | None:
    """Parse a duration string '3h' / '90m' / '1h30m' / '7200' into seconds.

    None / empty -> None. Same shape as etl.backfill_2025.parse_duration so
    --max-runtime semantics are uniform across backfill scripts.
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


def _date_range(start: str, end: str):
    sd = datetime.strptime(start, "%Y-%m-%d").date()
    ed = datetime.strptime(end, "%Y-%m-%d").date()
    cur = sd
    while cur <= ed:
        yield cur.strftime("%Y-%m-%d")
        cur += timedelta(days=1)


def _hms(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def _active_batters_for_date(conn: sqlite3.Connection, date_str: str) -> list[int]:
    """Every distinct player_id in daily_lineup on-or-before *date_str*
    within the 2025 season window. The "on-or-before" semantics means
    batters who appeared earlier in the season but not on the target date
    still get a snapshot — they may show up in tomorrow's lineup and we
    want the season-to-date data ready.

    Conservatively restricts to lineup_source != 'roster_fallback' so
    batters who never actually played (bdfed alphabetical placeholders)
    don't bloat the backfill. If lineup_source is missing (older DBs),
    the row is kept (back-compat).
    """
    season_year = int(date_str[:4])
    season_start = f"{season_year}-03-01"
    rows = conn.execute(
        """
        SELECT DISTINCT player_id
        FROM daily_lineup
        WHERE date >= ? AND date <= ?
          AND player_id IS NOT NULL
          AND (lineup_source IS NULL OR lineup_source != 'roster_fallback')
        """,
        (season_start, date_str),
    ).fetchall()
    return [int(r[0]) for r in rows if r[0]]


def _coverage_for_date(
    conn: sqlite3.Connection, date_str: str, player_ids: list[int]
) -> float:
    """Fraction of player_ids that already have a row in
    batter_pitch_type_splits at date_through == date_str.

    1.0 means every batter is done; 0.0 means none. Used to skip
    well-populated dates on a resumed run.
    """
    if not player_ids:
        return 1.0
    placeholders = ",".join("?" for _ in player_ids)
    n = conn.execute(
        f"SELECT COUNT(*) FROM batter_pitch_type_splits "
        f"WHERE date_through = ? AND player_id IN ({placeholders})",
        (date_str, *player_ids),
    ).fetchone()[0]
    return n / len(player_ids)


def backfill_one_date(
    conn: sqlite3.Connection, date_str: str, *, force: bool = False
) -> dict:
    """Resolve the date's active batter set, pull splits, write rows.

    Returns ``{"date": str, "n_batters": int, "n_written": int,
    "skipped": bool, "elapsed_s": float}``.
    """
    t0 = time.time()
    batters = _active_batters_for_date(conn, date_str)
    if not batters:
        return {
            "date": date_str, "n_batters": 0, "n_written": 0,
            "skipped": True, "reason": "no batters in daily_lineup",
            "elapsed_s": time.time() - t0,
        }

    if not force:
        cov = _coverage_for_date(conn, date_str, batters)
        if cov >= COVERAGE_SKIP_THRESHOLD:
            return {
                "date": date_str, "n_batters": len(batters), "n_written": 0,
                "skipped": True,
                "reason": f"already {cov*100:.0f}% populated",
                "elapsed_s": time.time() - t0,
            }

    print(f"  {date_str}: pulling splits for {len(batters)} batters...")
    splits = fetch_batter_pitch_type_splits(
        batters, as_of_date=date_str, season=int(date_str[:4]),
    )
    if not splits:
        return {
            "date": date_str, "n_batters": len(batters), "n_written": 0,
            "skipped": True, "reason": "empty Statcast pull",
            "elapsed_s": time.time() - t0,
        }

    insert_sql = """
        INSERT OR REPLACE INTO batter_pitch_type_splits (
            player_id, date_through,
            fb_slg, fb_pa, br_slg, br_pa, os_slg, os_pa, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
    """
    n_written = 0
    for pid, entry in splits.items():
        try:
            conn.execute(insert_sql, (
                int(pid), date_str,
                entry.get("fb_slg"), entry.get("fb_pa") or 0,
                entry.get("br_slg"), entry.get("br_pa") or 0,
                entry.get("os_slg"), entry.get("os_pa") or 0,
            ))
            n_written += 1
        except Exception as e:
            print(f"    [WRITE-ERROR] {date_str} pid={pid}: {e}")
    conn.commit()
    elapsed = time.time() - t0
    print(f"  {date_str}: wrote {n_written} rows in {elapsed:.1f}s")
    return {
        "date": date_str, "n_batters": len(batters), "n_written": n_written,
        "skipped": False, "elapsed_s": elapsed,
    }


def backfill_window(
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
    *,
    db_path: Path | None = None,
    force: bool = False,
    max_dates: int | None = None,
    max_runtime_s: float | None = None,
) -> dict:
    """Walk [start, end] and call backfill_one_date per date.

    *force* — re-pull every date, ignoring coverage gate.
    *max_dates* — stop after N dates RAN (skipped ones don't count).
    *max_runtime_s* — stop after wall-clock budget elapses (between dates).
    """
    conn = get_db(db_path)
    create_tables(conn)

    summary = {
        "start": start, "end": end,
        "dates_run": 0, "dates_skipped": 0, "dates_failed": 0,
        "total_rows": 0,
        "stopped_reason": "completed",
        "last_completed": None,
    }
    t_start = time.time()

    for date_str in _date_range(start, end):
        # Budget gates run before any work for the date.
        if max_dates is not None and summary["dates_run"] >= max_dates:
            summary["stopped_reason"] = f"max_dates={max_dates} reached"
            print(f"\n  [STOP] {summary['stopped_reason']}")
            break
        if max_runtime_s is not None and (time.time() - t_start) >= max_runtime_s:
            summary["stopped_reason"] = (
                f"max_runtime={_hms(max_runtime_s)} elapsed"
            )
            print(f"\n  [STOP] {summary['stopped_reason']}")
            break

        try:
            r = backfill_one_date(conn, date_str, force=force)
            if r.get("skipped"):
                summary["dates_skipped"] += 1
                if r.get("reason"):
                    print(f"  [SKIP] {date_str}: {r['reason']}")
            else:
                summary["dates_run"] += 1
                summary["total_rows"] += r.get("n_written", 0)
                summary["last_completed"] = date_str
        except KeyboardInterrupt:
            summary["stopped_reason"] = "user interrupt"
            print(f"\n  [INTERRUPT] stopped at {date_str}.")
            raise
        except Exception as e:
            print(f"  [ERROR] {date_str}: {type(e).__name__}: {e}")
            summary["dates_failed"] += 1
            continue

    conn.close()
    summary["elapsed_s"] = time.time() - t_start
    return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    ap.add_argument("--start", default=DEFAULT_START,
                    help=f"YYYY-MM-DD (default {DEFAULT_START})")
    ap.add_argument("--end", default=DEFAULT_END,
                    help=f"YYYY-MM-DD (default {DEFAULT_END})")
    ap.add_argument("--force", action="store_true",
                    help="Re-pull and overwrite even when coverage > 70%% "
                         "(literal %% escaped for Python 3.14 argparse strictness)")
    ap.add_argument("--max-dates", type=int, default=None, metavar="N",
                    help="Stop after N dates RUN (skipped ones don't count)")
    ap.add_argument("--max-runtime", type=str, default=None, metavar="DURATION",
                    help="Stop after a wall-clock budget elapses "
                         "('3h', '90m', '1h30m', or seconds-as-int)")
    ap.add_argument("--db", default=None, help="Optional alternate DB path")
    args = ap.parse_args()

    db_path = Path(args.db) if args.db else None
    max_runtime_s = parse_duration(args.max_runtime)

    summary = backfill_window(
        start=args.start, end=args.end,
        db_path=db_path, force=args.force,
        max_dates=args.max_dates, max_runtime_s=max_runtime_s,
    )

    print()
    print("=" * 70)
    print("  PITCH-TYPE SPLITS BACKFILL SUMMARY")
    print("=" * 70)
    for k, v in summary.items():
        print(f"  {k:<18}  {v}")
    print()


if __name__ == "__main__":
    main()
