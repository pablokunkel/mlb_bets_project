#!/usr/bin/env python3
"""
backfill_recent_days.py — Fill the dashboard gap between raw_data.csv (ends
2026-04-15) and today.

For each date in the requested range, this script:
  1. Looks for an existing results/picks_<DATE>.json (the daily flow was
     writing these even when DB load was broken — see HANDOFF doc).
  2. If found, ingests it via load_picks_to_db.py.
  3. If missing AND --regen is passed, calls generate_picks.py to
     regenerate. WARNING: regenerated picks have look-ahead bias —
     pitcher season stats + Statcast reflect today's data, not what was
     known on the historical date.
  4. After all picks are loaded, fetches outcomes for the full range via
     etl/etl_outcomes.py.

After this runs, the user still needs to run:
    python export_site_data.py
    netlify deploy --prod --dir=mlb_hr_bet_site --site=0fade6bd-ae06-43a8-aaef-22ee692ecbba

Usage:
    python backfill_recent_days.py --start 2026-04-16 --end 2026-04-28
    python backfill_recent_days.py --start 2026-04-16 --end 2026-04-28 --regen
    python backfill_recent_days.py --dry-run
"""

import argparse
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path


PROJECT_DIR = Path(__file__).parent
RESULTS_DIR = PROJECT_DIR.parent / "results"
PYTHON = sys.executable


def daterange(start: str, end: str):
    """Yield each YYYY-MM-DD from start to end inclusive."""
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d")
    cur = s
    while cur <= e:
        yield cur.strftime("%Y-%m-%d")
        cur += timedelta(days=1)


def run(cmd: list[str], dry: bool = False) -> int:
    """Run a subprocess; return exit code. Echoes the command."""
    print(f"  $ {' '.join(cmd)}")
    if dry:
        return 0
    try:
        return subprocess.call(cmd)
    except FileNotFoundError as e:
        print(f"    ERROR: {e}")
        return 127


def has_picks_json(date_str: str) -> Path | None:
    """Return path to picks_<date>.json if it exists, else None.
    Checks <project>/../results/, <project>/results/, and Desktop fallback.
    """
    candidates = [
        RESULTS_DIR / f"picks_{date_str}.json",
        PROJECT_DIR / "results" / f"picks_{date_str}.json",
        Path.home() / "Desktop" / "HR-Picks" / f"picks_{date_str}.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def main():
    ap = argparse.ArgumentParser(description="Backfill missing days into hr_bets.db")
    ap.add_argument("--start", default="2026-04-16", help="First date (YYYY-MM-DD)")
    ap.add_argument("--end", default=None, help="Last date (default: yesterday)")
    ap.add_argument("--regen", action="store_true",
                    help="If picks_<date>.json missing, regenerate via generate_picks.py "
                         "(look-ahead bias warning — see module docstring)")
    ap.add_argument("--skip-outcomes", action="store_true",
                    help="Don't run the outcomes ETL after loading picks")
    ap.add_argument("--dry-run", action="store_true", help="Print commands without executing")
    args = ap.parse_args()

    if args.end is None:
        args.end = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    print(f"Backfilling {args.start} -> {args.end}")
    print(f"  Project dir: {PROJECT_DIR}")
    print(f"  Results dir: {RESULTS_DIR}\n")

    # Phase 1: load picks per date
    found_count = 0
    regen_count = 0
    missing_count = 0
    load_failed = []
    dates = list(daterange(args.start, args.end))

    for d in dates:
        json_path = has_picks_json(d)
        if json_path:
            print(f"[{d}] FOUND existing JSON: {json_path}")
            rc = run([
                PYTHON, str(PROJECT_DIR / "load_picks_to_db.py"),
                "--json", str(json_path),
            ], dry=args.dry_run)
            if rc == 0:
                found_count += 1
            else:
                load_failed.append(d)
        elif args.regen:
            print(f"[{d}] MISSING JSON — regenerating (look-ahead bias warning)")
            # Regenerate JSON via generate_picks.py
            rc1 = run([
                PYTHON, str(PROJECT_DIR / "generate_picks.py"),
                "--date", d,
            ], dry=args.dry_run)
            if rc1 != 0:
                print(f"    generate_picks.py failed for {d}")
                load_failed.append(d)
                continue
            # Now load it
            new_path = RESULTS_DIR / f"picks_{d}.json"
            rc2 = run([
                PYTHON, str(PROJECT_DIR / "load_picks_to_db.py"),
                "--json", str(new_path),
            ], dry=args.dry_run)
            if rc2 == 0:
                regen_count += 1
            else:
                load_failed.append(d)
        else:
            print(f"[{d}] MISSING JSON — skipped (use --regen to regenerate)")
            missing_count += 1

    print()
    print("=" * 60)
    print(f"Picks loaded:  {found_count} from existing JSON, {regen_count} regenerated")
    print(f"Missing/skip:  {missing_count}")
    if load_failed:
        print(f"FAILED dates:  {load_failed}")
    print("=" * 60)

    # Phase 2: outcomes ETL — etl/etl_outcomes.py only supports --date (one
    # day at a time) or --backfill (find dates with picks but no outcomes).
    # We loop date-by-date so the user sees per-date progress.
    if not args.skip_outcomes:
        print(f"\nFetching outcomes for each date in {args.start} -> {args.end}...")
        any_failed = False
        for d in dates:
            rc = run([
                PYTHON, "-m", "etl.etl_outcomes",
                "--date", d,
            ], dry=args.dry_run)
            if rc != 0:
                any_failed = True
                print(f"    WARN: outcomes ETL exit code {rc} on {d}")
        if any_failed:
            print("  Some dates failed — re-run individually if needed:")
            print(f"    python -m etl.etl_outcomes --date YYYY-MM-DD")

    # Reminder
    print("\nNext steps (run from project dir):")
    print("  python export_site_data.py")
    site_id = "0fade6bd-ae06-43a8-aaef-22ee692ecbba"
    print(f"  netlify deploy --prod --dir=mlb_hr_bet_site --site={site_id}")


if __name__ == "__main__":
    main()
