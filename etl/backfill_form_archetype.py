#!/usr/bin/env python3
"""
backfill_form_archetype.py — Phase 2: populate batter_form_archetype for 2025.

For each as-of date in [start, end] and each window in {7, 14, 21} days,
walk every batter with at least 1 HR through that date, build their
pre-HR state-of-play centroid via features_v2.compute_batter_form_archetype,
and INSERT OR REPLACE one row into batter_form_archetype keyed by
(player_id, date_through, window_days).

Modeled on etl/backfill_statcast_windows.py:
  - ONE bulk pybaseball.statcast() pull at the start of the backfill,
    shared across ALL (date, window) iterations. No per-batter, per-HR
    API roundtrips. See features_v2.compute_batter_form_archetype.
  - Resume-safe: skip (date, window) pairs already densely populated.
  - --max-dates / --max-runtime for chunked runs (PR 4 pattern).
  - --reset to wipe partial centroids from a previous bad run.

Phase 2 ships the orchestrator; nightly hook follows in Phase 3 once
the backtest validates the signal. With USE_FORM_ARCHETYPE=False
(score_batters default), running this fills the table but does not change
production scoring — strictly additive.

Usage:
    python etl/backfill_form_archetype.py
    python etl/backfill_form_archetype.py --start 2025-06-01 --end 2025-06-05
    python etl/backfill_form_archetype.py --window-days 7   # one window only
    python etl/backfill_form_archetype.py --max-dates 5 --max-runtime 1h
    python etl/backfill_form_archetype.py --reset            # wipe before run
    python etl/backfill_form_archetype.py --dry-run

Runtime: with the bulk-pull architecture, per-(date, window) work is
in-memory groupby + slicing — seconds, not hours. The dominant cost is
ONE Statcast pull for the full lookback span (~1 hour on a cold cache,
free thereafter via the 24h disk cache).
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


def _reset_centroids(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    windows: tuple[int, ...] | None = None,
) -> int:
    """Wipe batter_form_archetype rows in [start, end] (date_through inclusive).

    Use after the 2026-05-26 per-batter-API-spam bug to clear the partial
    bad data before the bulk-pull backfill writes fresh rows. Returns the
    row count deleted.
    """
    if windows is None:
        cur = conn.execute(
            """
            DELETE FROM batter_form_archetype
            WHERE date_through >= ? AND date_through <= ?
            """,
            (start, end),
        )
    else:
        placeholders = ",".join("?" * len(windows))
        cur = conn.execute(
            f"""
            DELETE FROM batter_form_archetype
            WHERE date_through >= ? AND date_through <= ?
              AND window_days IN ({placeholders})
            """,
            (start, end, *windows),
        )
    n = cur.rowcount
    conn.commit()
    return n


# ---------------------------------------------------------------------------
# Per-(date, window) worker — now with shared prefetched frame
# ---------------------------------------------------------------------------

def backfill_one_date_window(
    conn: sqlite3.Connection,
    date_through: str,
    window_days: int,
    *,
    dry_run: bool = False,
    force: bool = False,
    prefetched_df=None,
) -> dict:
    """Compute + write centroids for one as-of date, one window. Returns counts.

    INSERT OR REPLACE keyed on (player_id, date_through, window_days) — safe
    to re-run. Idempotent: a re-run with the same Statcast cache yields the
    same centroid bytes.

    *prefetched_df* — when set, passed through to the builder so the
    bulk Statcast pull is amortized across many (date, window) iterations.
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
        _prefetched_df=prefetched_df,
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
# Bulk-pull amortization across the entire backfill window
# ---------------------------------------------------------------------------

def _compute_full_backfill_span(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    windows: tuple[int, ...],
) -> tuple[str | None, str | None]:
    """Return (span_start, span_end) covering EVERY HR that will be sliced
    by any (date_through, window) iteration in this backfill.

    The Statcast frame must cover [min(hr_date) - max_window, max(hr_date) - 1]
    so every window slice for every iteration is in-memory.

    *hr_date* range comes from batter_hr_events filtered by the lookback
    season range relevant to the backfill window — same as the builder.

    Returns (None, None) if the DB has no relevant HR events.
    """
    # Honest as-of-date: we need HRs strictly before `end`. The lookback
    # range starts FORM_ARCHETYPE_LOOKBACK_SEASONS-1 seasons before `start`.
    from features_v2 import FORM_ARCHETYPE_LOOKBACK_SEASONS
    start_year = int(start[:4])
    lookback_floor = f"{start_year - FORM_ARCHETYPE_LOOKBACK_SEASONS + 1}-03-01"

    row = conn.execute(
        """
        SELECT MIN(game_date) AS lo, MAX(game_date) AS hi
        FROM batter_hr_events
        WHERE game_date >= ? AND game_date < ?
        """,
        (lookback_floor, end),
    ).fetchone()
    if row is None or row[0] is None:
        return (None, None)

    lo_str, hi_str = str(row[0]), str(row[1])
    try:
        lo_dt = datetime.strptime(lo_str, "%Y-%m-%d")
        hi_dt = datetime.strptime(hi_str, "%Y-%m-%d")
    except ValueError:
        return (None, None)

    # Pad the start by the widest window (always 21 in the 3x3 sweep) so
    # every per-HR slice [hr_date - window, hr_date - 1] is in-memory.
    pad_days = max(max(windows), 21)
    span_start = (lo_dt - timedelta(days=pad_days)).strftime("%Y-%m-%d")
    span_end = (hi_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    return (span_start, span_end)


def _prefetch_bulk_statcast(span_start: str, span_end: str):
    """Wrapper around features_v2._fetch_form_archetype_bulk_statcast with
    a more visible progress line — this is the dominant cost of the backfill.
    """
    from features_v2 import _fetch_form_archetype_bulk_statcast
    t0 = time.time()
    print()
    print(f"  [bulk] pulling Statcast span {span_start}..{span_end} "
          f"(ONE call, shared across ALL date×window iterations)...")
    df = _fetch_form_archetype_bulk_statcast(span_start, span_end)
    elapsed = time.time() - t0
    n_rows = 0 if df is None else len(df)
    print(f"  [bulk] pulled {span_start}..{span_end} — {n_rows} rows, took {_hms(elapsed)}")
    return df


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
    reset: bool = False,
) -> dict:
    """Walk every date * window combo in [start, end]. Returns summary.

    *max_dates* counts DATES (not date×window pairs) processed before stop.
    *max_runtime_s* is wall-clock; checked between dates, not mid-date.
    *reset* — if True, DELETE all batter_form_archetype rows in [start, end]
              before backfilling. Use this after a botched run.

    Architecture (post-2026-05-26): ONE bulk pybaseball.statcast() pull
    at the start, shared in-memory across ALL (date, window) iterations.
    No per-batter API calls.
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
        "reset_rows_deleted": 0,
        "bulk_pull_rows": 0,
        "bulk_pull_seconds": 0.0,
        "stopped_reason": "completed",
        "last_completed": None,
    }
    t_start = time.time()

    try:
        # Optional reset pass — wipe partial data from a previous bad run.
        if reset and not dry_run:
            n_deleted = _reset_centroids(conn, start, end, windows)
            summary["reset_rows_deleted"] = n_deleted
            print(f"  [reset] deleted {n_deleted} batter_form_archetype rows "
                  f"in [{start}..{end}] for windows={list(windows)}")

        # Bulk Statcast pull — ONCE, shared across all iterations.
        prefetched_df = None
        if not dry_run:
            span_start, span_end = _compute_full_backfill_span(
                conn, start, end, windows
            )
            if span_start and span_end:
                t_bulk = time.time()
                prefetched_df = _prefetch_bulk_statcast(span_start, span_end)
                summary["bulk_pull_seconds"] = round(time.time() - t_bulk, 1)
                if prefetched_df is not None:
                    summary["bulk_pull_rows"] = len(prefetched_df)
            else:
                print("  [bulk] no HR events in lookback span — nothing to pull")

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
                        prefetched_df=prefetched_df,
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
    ap.add_argument("--reset", action="store_true",
                    help="DELETE batter_form_archetype rows in [start, end] "
                         "before backfilling. Use after the 2026-05-26 "
                         "per-batter-API-spam bug to clear partial bad data.")
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
        reset=args.reset,
    )

    print()
    print("=" * 70)
    print("  FORM-ARCHETYPE BACKFILL SUMMARY")
    print("=" * 70)
    for k, v in summary.items():
        print(f"  {k:<20}  {v}")
    if summary.get("last_completed"):
        print(
            f"\n  Resume with: python -m etl.backfill_form_archetype "
            f"--start {args.start} --end {args.end} --window-days {args.window_days}"
        )


if __name__ == "__main__":
    main()
