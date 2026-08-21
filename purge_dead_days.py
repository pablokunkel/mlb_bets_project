#!/usr/bin/env python3
"""
purge_dead_days.py — B33: remove picks/outcomes rows for dates that had no
real MLB games.

Why this exists
---------------
Over the 2026 All-Star break the pipeline published full 8-pick cards on
dates with zero scheduled games:

    2026-07-13   0 games   -> 8 picks, all invented by the offline simulator
    2026-07-14   1 event   -> the All-Star Game; 2 exhibition picks + 6 simulated
    2026-07-15   0 games   -> 8 picks, all invented by the offline simulator

Those rows are not "picks that lost". They are picks against games that
never existed, and they poison every surface that reads `daily_picks`:

  - the hit streak resets on them,
  - DB-side hit-rate queries count all three days as misses,
  - 2026-07-14 shows in picks_history as an honest-looking 0-for-8.

The generator-side guard (also B33) stops NEW dead days from being
written. This script cleans up the ones already in the DB.

Purge at the source, not per surface
------------------------------------
The exported surfaces already disagree about these days — `picks_history`
silently drops 2026-07-13 and 2026-07-15 (no outcomes -> dropped in
export_site_data.export_history) but shows 2026-07-14 as 0-for-8, while
DB-side queries count all three as misses. Adding read-time filters would
mean fixing each surface separately and forever. Deleting the rows fixes
every surface at once, including the ones nobody has written yet.

What gets deleted
-----------------
CORE (the tables named in the B33 brief):
    daily_picks     the card + full board for the date
    pick_inputs     per-batter factor inputs behind those scores
    outcomes        the "did he homer" rows recorded against them

RESIDUE (same dead days, other tables — see --core-only):
    daily_slate     the All-Star Game's slate row
    daily_lineup    the All-Star rosters as a confirmed lineup
    hr_events       All-Star home runs, which feed HR Recap / leaderboard

The residue tables matter because `etl_outcomes.fetch_hr_events_for_date`
reads game_pks straight out of `daily_slate`: leave the ASG slate row
behind and any hr_events backfill re-inserts the exhibition home run.

Safety
------
  - Dry run by DEFAULT. Deletion requires --apply.
  - Every row that will be deleted is written to CSV *before* the DELETE,
    one file per table, under --archive-dir.
  - The whole purge runs in ONE transaction; any error rolls it all back.
  - Idempotent: re-running against an already-purged DB deletes 0 rows.
  - Refuses to run against a date that currently HAS eligible games,
    unless --force. Guards against a fat-fingered date wiping a real day.

Usage:
    # See what would go, and write the CSV archive:
    python purge_dead_days.py

    # Actually delete:
    python purge_dead_days.py --apply

    # Other dates / another DB:
    python purge_dead_days.py --dates 2026-07-13,2026-07-15 --db path/to.db --apply

Note the DB resolution rule from etl/db.py: with HR_BETS_DB unset, a run
from a git worktree resolves to a stray path, not the canonical DB. Set
HR_BETS_DB or pass --db explicitly.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from etl.db import get_db  # noqa: E402

# The three dates the B33 brief scopes the purge to. Overridable via --dates.
DEFAULT_DEAD_DATES = ("2026-07-13", "2026-07-14", "2026-07-15")

# Tables named in the brief. Ordered child-before-parent for readability;
# there are no FK constraints in this schema, so order is cosmetic.
CORE_TABLES = ("daily_picks", "pick_inputs", "outcomes")

# Same dead days in tables the brief doesn't name. Skipped with --core-only.
RESIDUE_TABLES = ("daily_slate", "daily_lineup", "hr_events")


def count_rows(conn, table: str, dates: tuple[str, ...]) -> int:
    marks = ",".join("?" * len(dates))
    return conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE date IN ({marks})", dates
    ).fetchone()[0]


def archive_rows(conn, table: str, dates: tuple[str, ...], out_path: Path) -> int:
    """Dump every row about to be deleted to CSV. Returns the row count.

    Written before the DELETE so a purge can always be reconstructed. A
    table with zero matching rows still gets a header-only CSV — the
    archive should show that the table was considered, not leave a gap
    that reads like an oversight.
    """
    marks = ",".join("?" * len(dates))
    cur = conn.execute(
        f"SELECT * FROM {table} WHERE date IN ({marks}) ORDER BY date", dates
    )
    cols = [d[0] for d in cur.description]
    n = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for row in cur:
            w.writerow(row)
            n += 1
    return n


def assert_dates_are_dead(dates: tuple[str, ...], force: bool) -> None:
    """Refuse to purge a date that actually has eligible MLB games.

    Uses the same gameType allowlist as the pipeline, so "eligible" here
    means exactly what it means to get_schedule(). Network failure is not
    treated as consent: it aborts unless --force.
    """
    if force:
        print("  [!] --force: skipping the live 'is this date really dead' check.")
        return

    from fetch_daily_data import get_schedule

    for d in dates:
        try:
            games = get_schedule(d)
        except Exception as e:
            raise SystemExit(
                f"ERROR: could not verify {d} against the MLB schedule API "
                f"({type(e).__name__}: {e}). Refusing to purge blind — re-run "
                f"when the API is reachable, or pass --force if you are certain."
            )
        if games:
            raise SystemExit(
                f"ERROR: {d} has {len(games)} eligible game(s) on the MLB "
                f"schedule — that is a REAL game day, not a dead one. "
                f"Refusing to purge. Pass --force only if you know better."
            )
        print(f"  [ok] {d}: 0 eligible games on the MLB schedule (confirmed dead)")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="B33: purge picks/outcomes rows for no-game dates"
    )
    ap.add_argument(
        "--dates", default=",".join(DEFAULT_DEAD_DATES),
        help="Comma-separated YYYY-MM-DD list (default: the B33 All-Star-break dates)",
    )
    ap.add_argument("--db", default=None, help="DB path (default: canonical HR_BETS_DB / etl.db)")
    ap.add_argument(
        "--archive-dir", default=None,
        help="Where the pre-delete CSV archive goes "
             "(default: docs/purges/b33_<first-date>)",
    )
    ap.add_argument(
        "--apply", action="store_true",
        help="Actually delete. Without this the script is a dry run.",
    )
    ap.add_argument(
        "--core-only", action="store_true",
        help="Only purge daily_picks / pick_inputs / outcomes; leave "
             "daily_slate / daily_lineup / hr_events residue in place.",
    )
    ap.add_argument(
        "--force", action="store_true",
        help="Skip the 'this date really has no games' schedule check.",
    )
    args = ap.parse_args()

    dates = tuple(d.strip() for d in args.dates.split(",") if d.strip())
    if not dates:
        raise SystemExit("ERROR: --dates resolved to an empty list.")

    tables = CORE_TABLES + (() if args.core_only else RESIDUE_TABLES)

    archive_dir = (
        Path(args.archive_dir) if args.archive_dir
        else Path(__file__).parent / "docs" / "purges" / f"b33_{dates[0]}"
    )

    mode = "APPLY (rows will be deleted)" if args.apply else "DRY RUN (nothing deleted)"
    print("=" * 74)
    print("  B33 dead-day purge")
    print(f"  Mode:    {mode}")
    print(f"  Dates:   {', '.join(dates)}")
    print(f"  Tables:  {', '.join(tables)}")
    print(f"  Archive: {archive_dir}")
    print("=" * 74)

    print("\nVerifying the dates are genuinely dead...")
    assert_dates_are_dead(dates, args.force)

    conn = get_db(Path(args.db) if args.db else None)
    try:
        print("\nArchiving rows to CSV (before any delete)...")
        counts: dict[str, int] = {}
        for t in tables:
            n = archive_rows(conn, t, dates, archive_dir / f"{t}.csv")
            counts[t] = n
            print(f"  {t:<16} {n:>6} row(s) -> {archive_dir / (t + '.csv')}")

        # Per-date breakdown, so the PR can report more than a lump sum.
        print("\nPer-date breakdown:")
        header = f"  {'date':<12}" + "".join(f"{t:>16}" for t in tables)
        print(header)
        print("  " + "-" * (len(header) - 2))
        for d in dates:
            cells = "".join(f"{count_rows(conn, t, (d,)):>16}" for t in tables)
            print(f"  {d:<12}{cells}")
        total = sum(counts.values())
        print(f"\n  TOTAL rows in scope: {total}")

        if not args.apply:
            print("\nDry run — nothing was deleted. Re-run with --apply to commit.")
            return 0

        if total == 0:
            print("\nNothing to delete (already purged). No-op.")
            return 0

        print("\nDeleting...")
        marks = ",".join("?" * len(dates))
        deleted: dict[str, int] = {}
        try:
            conn.execute("BEGIN")
            for t in tables:
                cur = conn.execute(
                    f"DELETE FROM {t} WHERE date IN ({marks})", dates
                )
                deleted[t] = cur.rowcount
                print(f"  {t:<16} deleted {cur.rowcount:>6} row(s)")
            conn.commit()
        except Exception:
            conn.rollback()
            print("  ROLLED BACK — no rows deleted.")
            raise

        print(f"\n  TOTAL deleted: {sum(deleted.values())}")

        # Re-count so the log proves the tables are actually empty for
        # these dates rather than just reporting what DELETE claimed.
        print("\nPost-purge verification:")
        residual = 0
        for t in tables:
            n = count_rows(conn, t, dates)
            residual += n
            flag = "OK" if n == 0 else "STILL PRESENT"
            print(f"  {t:<16} {n:>6} row(s)  [{flag}]")
        if residual:
            print("\n  WARNING: rows remain for the purged dates.")
            return 1

        manifest = archive_dir / "MANIFEST.txt"
        with open(manifest, "w", encoding="utf-8") as f:
            f.write("B33 dead-day purge\n")
            f.write(f"purged_at: {datetime.now().isoformat()}\n")
            f.write(f"dates: {', '.join(dates)}\n")
            f.write(f"db: {Path(args.db).resolve() if args.db else 'canonical (etl.db)'}\n\n")
            for t in tables:
                f.write(f"{t}: {deleted.get(t, 0)} row(s) deleted\n")
            f.write(f"\ntotal: {sum(deleted.values())} row(s)\n")
        print(f"\n  Manifest written to {manifest}")
        print("\nDone. Re-run export_site_data.py to refresh the site surfaces.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
