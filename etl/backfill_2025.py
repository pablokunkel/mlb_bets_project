#!/usr/bin/env python3
"""
backfill_2025.py — Reconstruct historical pick_inputs rows for the 2025 season.

What this does
--------------
Walks every date in the 2025 MLB regular season. For each date D:

  1. Builds the slate (games + lineups + weather + pitchers) AS-OF D — i.e.
     using only data that existed before D's morning. Threads as_of_date=D
     through every Statcast / profile fetcher (PR 3 + PR 4 infrastructure).
  2. Runs the standard compute_composite pipeline on every batter.
  3. Persists card + full board + raw inputs to `daily_picks` + `pick_inputs`,
     tagged `mode='backfill_2025'` so the A1 refit (PR 5) can include /
     exclude them explicitly.

Why
---
We need ~25,000 historical training rows for the weight refit + the
backtest harnesses, generated using TODAY's score_* functions. Backfilling
in-place (with strict as-of-date filtering) is the honest way to do it
without look-ahead bias.

Phase 0 — outcomes prereq
-------------------------
load_season_hr_lookup reads from the `outcomes` table. If `outcomes` has no
2025 rows, this script first runs historical_calibration's outcome backfill
into `historical_batter_games`, then bridges those rows into `outcomes` so
the existing B8 helper works unchanged.

Usage
-----
    # Default: walk full 2025 regular season
    python -m etl.backfill_2025

    # Sub-window for testing
    python -m etl.backfill_2025 --start 2025-04-01 --end 2025-04-15

    # Skip dates that already have pick_inputs rows (default behavior)
    python -m etl.backfill_2025 --resume

    # Force re-run of dates that already exist
    python -m etl.backfill_2025 --force

    # Just verify outcomes are populated, don't run the slate loop
    python -m etl.backfill_2025 --outcomes-only

Runtime
-------
Cold cache: ~5-10 minutes per date due to per-pitcher / per-batter Statcast
roundtrips. The PR 4 perf fix makes pybaseball's HTTP cache hit across
backfill dates (one pull per player for the whole season), so dates 2-N
are much faster than date 1. Estimated full-season runtime: 6-12 hours.

Idempotence
-----------
Re-running a date deletes-and-replaces its daily_picks + pick_inputs rows.
Safe to interrupt + resume.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

# Make project root importable from etl/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from etl.db import get_db, create_tables, log_etl_start, log_etl_complete, log_etl_fail
from load_picks_to_db import load_picks
from generate_picks import generate_card, format_card


# 2025 regular season window. Spring training games + post-season are
# intentionally excluded (different game cadence + roster shuffling
# screw up the season-to-date aggregates).
DEFAULT_START = "2025-03-27"
DEFAULT_END   = "2025-09-30"

# Where generate_card writes its picks_<DATE>.json (matches the production
# convention in load_picks_to_db.resolve_json_path).
RESULTS_DIR = Path(__file__).resolve().parent.parent.parent / "results"


# ---------------------------------------------------------------------------
# Phase 0: outcomes prereq
# ---------------------------------------------------------------------------

def _count_2025_outcomes(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM outcomes WHERE date LIKE '2025-%'"
    ).fetchone()[0]


def _count_2025_historical(conn: sqlite3.Connection) -> int:
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM historical_batter_games WHERE season = 2025"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def bridge_historical_to_outcomes(conn: sqlite3.Connection) -> int:
    """Copy historical_batter_games 2025 rows into outcomes so the B8
    helper (load_season_hr_lookup) works. INSERT OR IGNORE so re-running
    is safe; we keep any production-written outcomes rows if they exist.

    historical_batter_games has fewer columns than outcomes (no ab/hits/
    rbi/etc). For the backfill, only date+batter_id+game_pk+hr_count are
    used by score_power's floor lookup, so we fill the rest with 0 / NULL.
    """
    n_before = _count_2025_outcomes(conn)
    conn.execute("""
        INSERT OR IGNORE INTO outcomes
            (date, batter_id, batter_name, game_pk,
             ab, hits, hr_count, rbi, doubles, triples, total_bases)
        SELECT
            hbg.date,
            hbg.batter_id,
            NULL                  AS batter_name,
            hbg.game_pk,
            NULL                  AS ab,
            NULL                  AS hits,
            hbg.hr_count,
            NULL                  AS rbi,
            0                     AS doubles,
            0                     AS triples,
            0                     AS total_bases
        FROM historical_batter_games hbg
        WHERE hbg.season = 2025
    """)
    conn.commit()
    n_after = _count_2025_outcomes(conn)
    return n_after - n_before


def ensure_2025_outcomes(conn: sqlite3.Connection) -> None:
    """Phase 0: make sure outcomes has 2025 rows so load_season_hr_lookup
    returns real season_hr values during the backfill.

    Flow:
      1. If outcomes already has 2025 rows: done.
      2. If historical_batter_games has 2025 rows: bridge them.
      3. Else: run historical_calibration's outcome backfill, then bridge.
    """
    n = _count_2025_outcomes(conn)
    if n > 0:
        print(f"  [PHASE 0] outcomes already has {n} 2025 rows — skipping backfill")
        return

    hist_n = _count_2025_historical(conn)
    if hist_n == 0:
        print(f"  [PHASE 0] historical_batter_games has no 2025 rows; "
              "running historical_calibration.backfill_outcomes_for_season(2025) "
              "(~30-60 minutes)...")
        from etl.historical_calibration import backfill_outcomes_for_season
        backfill_outcomes_for_season(2025)
        hist_n = _count_2025_historical(conn)
        if hist_n == 0:
            raise RuntimeError(
                "historical_calibration ran but historical_batter_games "
                "is still empty for 2025 — check Savant rate limits / errors"
            )

    print(f"  [PHASE 0] bridging {hist_n} historical_batter_games rows -> outcomes...")
    written = bridge_historical_to_outcomes(conn)
    print(f"  [PHASE 0] outcomes gained {written} 2025 rows "
          f"(total: {_count_2025_outcomes(conn)})")


# ---------------------------------------------------------------------------
# Phase 1: walk dates
# ---------------------------------------------------------------------------

def _date_range(start: str, end: str):
    sd = datetime.strptime(start, "%Y-%m-%d").date()
    ed = datetime.strptime(end, "%Y-%m-%d").date()
    cur = sd
    while cur <= ed:
        yield cur.strftime("%Y-%m-%d")
        cur += timedelta(days=1)


def _already_done(conn: sqlite3.Connection, date_str: str) -> bool:
    n = conn.execute(
        "SELECT COUNT(*) FROM pick_inputs WHERE date = ?", (date_str,)
    ).fetchone()[0]
    return n > 0


def _purge_date(conn: sqlite3.Connection, date_str: str) -> None:
    """Idempotent re-run: delete this date's daily_picks + pick_inputs."""
    conn.execute("DELETE FROM pick_inputs WHERE date = ?", (date_str,))
    conn.execute("DELETE FROM daily_picks WHERE date = ?", (date_str,))
    conn.commit()


def backfill_one_date(date_str: str, db_path: Path | None = None) -> dict:
    """Score one historical date and persist to DB. Returns counts."""
    t0 = time.time()
    print(f"\n=== Backfilling {date_str} ===")

    # generate_card writes JSON under <project_parent>/results/picks_<DATE>.json
    # by default. We accept that side-effect as the per-date checkpoint
    # artifact (matches the production flow). as_of_date=D filters every
    # historical fetch to strictly before D.
    card, tier_details, mode, full_board, status = generate_card(
        date_str, as_of_date=date_str,
    )

    if not full_board:
        print(f"  [SKIP] {date_str}: empty full_board (no games / API failure)")
        return {"date": date_str, "rows": 0, "selected": 0, "skipped": True}

    # Persist a results JSON in the same shape generate_picks.main() emits,
    # then call the standard loader so the DB schema stays canonical.
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESULTS_DIR / f"picks_{date_str}.json"
    payload = {
        "date": date_str,
        "picks": card,
        "full_board": full_board,
        "tier_details": tier_details,
        "mode": mode,
        "scoring_config": "default",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "is_backfill": True,
        "as_of_date": date_str,
    }
    json_path.write_text(json.dumps(payload, default=str))

    n_inserted, n_selected = load_picks(json_path, db_path)

    elapsed = time.time() - t0
    print(f"  [OK] {date_str}: {n_inserted} board rows ({n_selected} selected) "
          f"in {elapsed:.0f}s")
    return {"date": date_str, "rows": n_inserted, "selected": n_selected,
            "elapsed_s": elapsed, "mode": mode}


def backfill_window(
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
    resume: bool = True,
    force: bool = False,
    db_path: Path | None = None,
) -> dict:
    """Walk every date in [start, end] and call backfill_one_date.

    *resume* (default True) — skip dates that already have pick_inputs rows.
    *force* — re-run every date, deleting+re-inserting (overrides resume).
    """
    conn = get_db(db_path)
    create_tables(conn)

    log_id = log_etl_start(conn, "backfill_2025", f"{start}..{end}")
    summary = {
        "start": start, "end": end,
        "dates_run": 0, "dates_skipped": 0, "dates_failed": 0,
        "total_rows": 0,
    }

    try:
        ensure_2025_outcomes(conn)

        for date_str in _date_range(start, end):
            if force:
                _purge_date(conn, date_str)
            elif resume and _already_done(conn, date_str):
                print(f"  [SKIP] {date_str}: pick_inputs already populated "
                      "(use --force to re-run)")
                summary["dates_skipped"] += 1
                continue

            try:
                r = backfill_one_date(date_str, db_path=db_path)
                if r.get("skipped"):
                    summary["dates_skipped"] += 1
                else:
                    summary["dates_run"] += 1
                    summary["total_rows"] += r.get("rows", 0)
            except KeyboardInterrupt:
                print(f"\n  [INTERRUPT] stopped at {date_str}. "
                      "Re-run with --resume to continue.")
                raise
            except Exception as e:
                print(f"  [ERROR] {date_str}: {type(e).__name__}: {e}")
                traceback.print_exc()
                summary["dates_failed"] += 1
                continue

        log_etl_complete(
            conn, log_id,
            rows=summary["total_rows"],
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

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=DEFAULT_END)
    ap.add_argument("--resume", action="store_true", default=True,
                    help="Skip dates that already have pick_inputs rows (default)")
    ap.add_argument("--force", action="store_true",
                    help="Re-run every date, deleting + re-inserting")
    ap.add_argument("--outcomes-only", action="store_true",
                    help="Run Phase 0 (outcomes prereq) and exit")
    ap.add_argument("--db", default=None,
                    help="Optional alternate DB path")
    args = ap.parse_args()

    db_path = Path(args.db) if args.db else None

    if args.outcomes_only:
        conn = get_db(db_path)
        create_tables(conn)
        ensure_2025_outcomes(conn)
        conn.close()
        return

    summary = backfill_window(
        start=args.start, end=args.end,
        resume=args.resume, force=args.force,
        db_path=db_path,
    )

    print()
    print("=" * 70)
    print("  2025 BACKFILL SUMMARY")
    print("=" * 70)
    for k, v in summary.items():
        print(f"  {k:<16}  {v}")


if __name__ == "__main__":
    main()
