#!/usr/bin/env python3
"""
backfill_form_archetype.py — Phase 2: populate batter_form_archetype for 2025.

For each as-of date in [start, end] and each window in {7, 14, 21} days,
walk every batter with at least 1 HR through that date, build their
pre-HR state-of-play centroid via features_v2.compute_batter_form_archetype,
and INSERT OR REPLACE one row into batter_form_archetype keyed by
(player_id, date_through, window_days).

Modeled on etl/backfill_statcast_windows.py:
  - Resume-safe: skip (date, window) pairs already densely populated.
  - --max-dates / --max-runtime for chunked runs (PR 4 pattern).
  - One bulk Statcast pull per (batter, HR) inside the builder; this
    script just orchestrates the (date, window, batter-list) walks.

Phase 2 ships the orchestrator; nightly hook follows in Phase 3 once
the backtest validates the signal. With USE_FORM_ARCHETYPE=False
(score_batters default), running this fills the table but does not change
production scoring — strictly additive.

Usage:
    python etl/backfill_form_archetype.py
    python etl/backfill_form_archetype.py --start 2025-06-01 --end 2025-06-05
    python etl/backfill_form_archetype.py --window-days 7   # one window only
    python etl/backfill_form_archetype.py --max-dates 5 --max-runtime 1h
    python etl/backfill_form_archetype.py --dry-run

Runtime: per (date, window), the builder issues one Statcast pull per HR
per qualifying batter — slow. Full 188-date x 3-window run is estimated
~6 hours on a warm pybaseball cache. Use --max-dates / --max-runtime to
chunk into shorter sessions.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
sys.path.insert(0, str(_REPO_ROOT))

from etl.db import DB_PATH, create_tables, log_etl_start, log_etl_complete, log_etl_fail


# Default window list — mirrors the diagnostics/backtest_form_archetype.py
# 3x3 sweep (3 windows × 3 min-HR thresholds). The min-HR dimension is
# applied at re-score time (read from batter_form_archetype.n_hrs_used),
# so the backfill only sweeps the window dimension.
ALL_WINDOWS = (7, 14, 21)

# 2025 regular-season default window (matches etl/backfill_2025.py).
DEFAULT_START = "2025-03-27"
DEFAULT_END   = "2025-09-30"


# ---------------------------------------------------------------------------
# Helpers (mirror etl/backfill_2025 idioms so the CLI surface matches)
# ---------------------------------------------------------------------------

def _date_range(start: str, end: str):
    sd = datetime.strptime(start, "%Y-%m-%d").date()
    ed = datetime.strptime(end, "%Y-%m-%d").date()
    cur = sd
    while cur <= ed:
        yield cur.strftime("%Y-%m-%d")
        cur += timedelta(days=1)


def parse_duration(s: str | None) -> float | None:
    """Same shape as etl/backfill_2025.parse_duration — '3h', '90m', '1h30m'."""
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


def _resolve_windows(arg: str | None) -> tuple[int, ...]:
    """Parse --window-days. None / 'ALL' / 'all' -> (7, 14, 21). Int -> single."""
    if arg is None or str(arg).strip().lower() in ("", "all"):
        return ALL_WINDOWS
    try:
        w = int(arg)
    except ValueError:
        raise ValueError(
            f"bad --window-days {arg!r}; expected integer or 'ALL'"
        ) from None
    if w not in ALL_WINDOWS:
        # Allow any integer for forward-compat, but warn for off-grid values.
        print(
            f"  [warn] --window-days {w} is outside the 3x3 sweep grid "
            f"{ALL_WINDOWS}; row will be written but not picked up by the "
            "default backtest harness."
        )
    return (w,)


def _batters_with_hr_through(
    conn: sqlite3.Connection, date_through: str
) -> list[int]:
    """Return batter_ids with at least 1 HR in `batter_hr_events` strictly
    before *date_through*. The builder's MIN_HRS gate applies inside it —
    this is the outer filter for "did this batter homer at all in the
    lookback window".

    `batter_hr_events` is the canonical Statcast HR-event table the
    features_v2 builder reads. We include `outcomes` as a fallback so
    historical backfills that bridged from historical_batter_games (no
    pitch-level data) still see batters.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT batter_id
        FROM batter_hr_events
        WHERE game_date < ?
        UNION
        SELECT DISTINCT batter_id
        FROM outcomes
        WHERE date < ? AND COALESCE(hr_count, 0) > 0
        """,
        (date_through, date_through),
    ).fetchall()
    return sorted({int(r[0]) for r in rows if r[0] is not None})


def _existing_centroids(
    conn: sqlite3.Connection, date_through: str, window_days: int
) -> set[int]:
    """Player IDs that already have a centroid row for this (date, window)."""
    rows = conn.execute(
        """
        SELECT player_id
        FROM batter_form_archetype
        WHERE date_through = ? AND window_days = ?
        """,
        (date_through, window_days),
    ).fetchall()
    return {int(r[0]) for r in rows}


def _already_done(
    conn: sqlite3.Connection,
    date_through: str,
    window_days: int,
    n_batters: int,
    coverage_threshold: float = 0.7,
) -> bool:
    """True iff >coverage_threshold of expected batters have a row for
    (date, window). Conservative threshold — most batters have <MIN_HRS
    HRs in lookback and legitimately get a None row written elsewhere or
    just skipped; full 100% coverage is unrealistic."""
    if n_batters <= 0:
        return True
    n_existing = len(_existing_centroids(conn, date_through, window_days))
    return n_existing / n_batters > coverage_threshold


# ---------------------------------------------------------------------------
# Per-(date, window) worker
# ---------------------------------------------------------------------------

def backfill_one_date_window(
    conn: sqlite3.Connection,
    date_through: str,
    window_days: int,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    """Compute + write centroids for one as-of date, one window. Returns counts.

    INSERT OR REPLACE keyed on (player_id, date_through, window_days) — safe
    to re-run. Idempotent: a re-run with the same Statcast cache yields the
    same centroid bytes.
    """
    t0 = time.time()
    batters = _batters_with_hr_through(conn, date_through)
    if not batters:
        print(f"    {date_through} w{window_days}: no batters with prior HRs — skip")
        return {"n_batters": 0, "n_centroids": 0, "elapsed_s": 0.0, "skipped": True}

    n_batters = len(batters)
    if not force and _already_done(conn, date_through, window_days, n_batters):
        n_existing = len(_existing_centroids(conn, date_through, window_days))
        print(
            f"    {date_through} w{window_days}: "
            f"{n_existing}/{n_batters} centroids present (>70%) — skip"
        )
        return {
            "n_batters": n_batters, "n_centroids": n_existing,
            "elapsed_s": 0.0, "skipped": True,
        }

    if dry_run:
        print(
            f"    {date_through} w{window_days}: [dry-run] would call "
            f"compute_batter_form_archetype for {n_batters} batters"
        )
        return {
            "n_batters": n_batters, "n_centroids": 0,
            "elapsed_s": 0.0, "dry_run": True,
        }

    # Import lazily so the script imports + parses CLI without dragging
    # pybaseball in.
    from features_v2 import compute_batter_form_archetype

    centroids = compute_batter_form_archetype(
        player_ids=batters,
        as_of_date=date_through,
        window_days=window_days,
    )

    n_written = 0
    insert_sql = """
        INSERT OR REPLACE INTO batter_form_archetype
            (player_id, date_through, window_days,
             feature_centroid_json, n_hrs_used, fetched_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
    """
    for pid, entry in centroids.items():
        if entry is None:
            # None+skip per design — no row written, no league-avg fallback.
            continue
        centroid = entry.get("feature_centroid")
        n_hrs = entry.get("n_hrs_used", 0)
        if not centroid:
            continue
        conn.execute(
            insert_sql,
            (int(pid), date_through, int(window_days),
             json.dumps(centroid), int(n_hrs)),
        )
        n_written += 1
    conn.commit()

    elapsed = time.time() - t0
    print(
        f"    {date_through} w{window_days}: {n_written}/{n_batters} centroids "
        f"written in {_hms(elapsed)}"
    )
    return {
        "n_batters": n_batters,
        "n_centroids": n_written,
        "elapsed_s": elapsed,
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def backfill_window(
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
    windows: tuple[int, ...] = ALL_WINDOWS,
    db_path: Path | None = None,
    *,
    dry_run: bool = False,
    force: bool = False,
    max_dates: int | None = None,
    max_runtime_s: float | None = None,
) -> dict:
    """Walk every date * window combo in [start, end]. Returns summary.

    *max_dates* counts DATES (not date×window pairs) processed before stop.
    *max_runtime_s* is wall-clock; checked between dates, not mid-date.
    """
    conn_path = str(db_path) if db_path else str(DB_PATH)
    conn = sqlite3.connect(conn_path)
    conn.row_factory = sqlite3.Row
    create_tables(conn)

    log_id = log_etl_start(conn, "backfill_form_archetype",
                           f"{start}..{end} windows={windows}")
    summary = {
        "start": start, "end": end,
        "windows": list(windows),
        "dates_run": 0, "dates_skipped": 0, "dates_failed": 0,
        "total_centroids": 0,
        "stopped_reason": "completed",
        "last_completed": None,
    }
    t_start = time.time()

    try:
        for date_str in _date_range(start, end):
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

            print(f"\n[{date_str}] windows={list(windows)}")
            date_ran = False
            date_failed = False
            for w in windows:
                try:
                    r = backfill_one_date_window(
                        conn, date_str, w,
                        dry_run=dry_run, force=force,
                    )
                    if not r.get("skipped") and not r.get("dry_run"):
                        date_ran = True
                        summary["total_centroids"] += r.get("n_centroids", 0)
                except KeyboardInterrupt:
                    summary["stopped_reason"] = "user interrupt"
                    raise
                except Exception as e:
                    print(f"    {date_str} w{w}: ERROR {type(e).__name__}: {e}")
                    date_failed = True

            if date_failed:
                summary["dates_failed"] += 1
            elif date_ran:
                summary["dates_run"] += 1
                summary["last_completed"] = date_str
            else:
                summary["dates_skipped"] += 1

        log_etl_complete(
            conn, log_id,
            rows=summary["total_centroids"],
            detail=json.dumps(summary),
        )
    except KeyboardInterrupt:
        log_etl_fail(conn, log_id, "user interrupt")
        raise
    except Exception as e:
        log_etl_fail(conn, log_id, str(e))
        raise
    finally:
        conn.close()

    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0].strip(),
    )
    ap.add_argument("--start", default=DEFAULT_START,
                    help=f"start date YYYY-MM-DD (default: {DEFAULT_START})")
    ap.add_argument("--end", default=DEFAULT_END,
                    help=f"end date YYYY-MM-DD (default: {DEFAULT_END})")
    ap.add_argument("--window-days", default="ALL",
                    help="window in days (7, 14, 21) or 'ALL' for the full "
                         "3x3 backtest sweep. Default: ALL.")
    ap.add_argument("--dry-run", action="store_true",
                    help="report counts without calling the Statcast pull")
    ap.add_argument("--force", action="store_true",
                    help="re-run dates even if centroids already populated")
    ap.add_argument("--max-dates", type=int, default=None, metavar="N",
                    help="stop after N dates have been processed. Chunked "
                         "run support — same idiom as etl/backfill_2025.py.")
    ap.add_argument("--max-runtime", type=str, default=None, metavar="DURATION",
                    help="stop after wall-clock budget elapses. "
                         "Formats: '3h', '90m', '1h30m', or seconds as int.")
    ap.add_argument("--db", default=None,
                    help="optional alternate DB path")
    args = ap.parse_args()

    db_path = Path(args.db) if args.db else None
    windows = _resolve_windows(args.window_days)
    max_runtime_s = parse_duration(args.max_runtime)

    summary = backfill_window(
        start=args.start, end=args.end,
        windows=windows,
        db_path=db_path,
        dry_run=args.dry_run,
        force=args.force,
        max_dates=args.max_dates,
        max_runtime_s=max_runtime_s,
    )

    print()
    print("=" * 70)
    print("  FORM-ARCHETYPE BACKFILL SUMMARY")
    print("=" * 70)
    for k, v in summary.items():
        print(f"  {k:<18}  {v}")
    if summary.get("last_completed"):
        print(
            f"\n  Resume with: python -m etl.backfill_form_archetype "
            f"--start {args.start} --end {args.end} --window-days {args.window_days}"
        )


if __name__ == "__main__":
    main()
