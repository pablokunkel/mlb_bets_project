#!/usr/bin/env python3
"""
yesterday_hr_audit.py — Score yesterday's HR hitters by their pre-game rank.

For every batter who hit a HR on the target date, JOIN against
`daily_picks` and surface:

  * Whether they were on the board at all (off-board hitters mean we
    couldn't have picked them — different problem from "ranked low").
  * Where they ranked among the day's full board (composite + rank).
  * Whether we selected them in the 8-pick card.
  * Tier label (T1-Chalk / T2-Mid / T3-Longshot / T4-Untiered).

Aggregates per day: how many HR hitters came from the top decile, the
top quartile, etc., and how many were entirely off-board. Useful for
spotting "we ranked Drake Baldwin 97 the day he hit his 8th HR" patterns
systematically rather than only when one happens to catch your eye.

Usage:
    python diagnostics/yesterday_hr_audit.py                  # yesterday only
    python diagnostics/yesterday_hr_audit.py --date 2026-05-02
    python diagnostics/yesterday_hr_audit.py --days 7         # last 7 days
    python diagnostics/yesterday_hr_audit.py --days 30 --summary-only

Reads from etl.db.DB_PATH.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from etl.db import DB_PATH


def fetch_hr_audit(conn: sqlite3.Connection, target_date: str) -> list[dict]:
    """Pull every HR hitter for *target_date* with their daily_picks context.

    LEFT JOIN — off-board hitters appear with NULLs in the picks columns,
    which the formatter treats as "off-board" rather than "rank 0".
    """
    rows = conn.execute(
        """
        SELECT
            o.batter_id,
            o.batter_name,
            o.hr_count,
            o.ab,
            o.hits,
            dp.team,
            dp.tier_label,
            dp.opp_pitcher,
            dp.composite,
            dp.power_score,
            dp.matchup_score,
            dp.form_score,
            dp.rank_in_board,
            dp.selected
        FROM outcomes o
        LEFT JOIN daily_picks dp
               ON dp.date = o.date
              AND dp.batter_id = o.batter_id
        WHERE o.date = ? AND o.hr_count > 0
        ORDER BY o.hr_count DESC, dp.rank_in_board ASC NULLS LAST
        """,
        (target_date,),
    ).fetchall()
    return [dict(r) for r in rows]


def board_size(conn: sqlite3.Connection, target_date: str) -> int:
    """How many batters were on the full board for *target_date*?

    Used to compute deciles/quartiles. Returns 0 if the date had no picks.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM daily_picks WHERE date = ?",
        (target_date,),
    ).fetchone()
    return row[0] if row else 0


def summarize_day(hr_rows: list[dict], n_board: int) -> dict:
    """Per-day aggregate: ranked-on-board, off-board, decile/quartile counts."""
    on_board = [r for r in hr_rows if r["rank_in_board"] is not None]
    off_board = [r for r in hr_rows if r["rank_in_board"] is None]
    selected = [r for r in hr_rows if r["selected"] == 1]

    ranks = [r["rank_in_board"] for r in on_board]
    avg_rank = sum(ranks) / len(ranks) if ranks else None

    top_decile_cut = max(1, n_board // 10) if n_board else 0
    top_quartile_cut = max(1, n_board // 4) if n_board else 0
    in_top_decile = sum(1 for r in ranks if r <= top_decile_cut)
    in_top_quartile = sum(1 for r in ranks if r <= top_quartile_cut)

    return {
        "n_hitters":        len(hr_rows),
        "n_on_board":       len(on_board),
        "n_off_board":      len(off_board),
        "n_selected":       len(selected),
        "avg_rank":         avg_rank,
        "n_top_decile":     in_top_decile,
        "n_top_quartile":   in_top_quartile,
        "board_size":       n_board,
        "top_decile_cut":   top_decile_cut,
        "top_quartile_cut": top_quartile_cut,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Formatting
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_rank(r: dict, board_size: int) -> str:
    rk = r["rank_in_board"]
    if rk is None:
        return "OFF "
    return f"{rk:>3d}/{board_size:<3d}"


def _fmt_score(v: float | None) -> str:
    if v is None:
        return "  -"
    return f"{v:>5.1f}"


def print_day_detail(target_date: str, hr_rows: list[dict],
                     summary: dict) -> None:
    """Per-row table for a single date."""
    n = summary["board_size"]

    print(f"\n=== HR audit for {target_date} ===")
    if not hr_rows:
        print("  No HR hitters recorded.")
        return

    # Headline summary first so it's the first thing you see.
    avg_rank = summary["avg_rank"]
    avg_rank_s = f"{avg_rank:.1f}" if avg_rank is not None else "n/a"
    print(
        f"  {summary['n_hitters']} HR hitters | board={n} | "
        f"selected={summary['n_selected']} | on_board={summary['n_on_board']} | "
        f"off_board={summary['n_off_board']} | "
        f"avg_rank={avg_rank_s} | "
        f"in_top_{summary['top_decile_cut']}={summary['n_top_decile']} | "
        f"in_top_{summary['top_quartile_cut']}={summary['n_top_quartile']}"
    )
    print()

    # Detail table — picked first (sel=1), then on-board sorted by rank,
    # then off-board.
    header = (
        f"  {'#':>3} {'Player':<22} {'Team':<5} {'HRs':>3} {'Tier':<13} "
        f"{'Rank':>8} {'Comp':>5} {'Pow':>5} {'Mtch':>5} {'Form':>5}  Sel"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    # Stable display order: picked first, then ascending rank, then off-board.
    def sort_key(r: dict) -> tuple:
        sel = 0 if r["selected"] == 1 else 1
        rk = r["rank_in_board"] if r["rank_in_board"] is not None else 9999
        return (sel, rk)

    for i, r in enumerate(sorted(hr_rows, key=sort_key), 1):
        name = r["batter_name"] or "?"
        if len(name) > 21:
            name = name[:20] + "."
        team = (r["team"] or "?")[:5]
        tier = (r["tier_label"] or "off-board")[:13]
        sel = "*" if r["selected"] == 1 else " "
        print(
            f"  {i:>3} {name:<22} {team:<5} {r['hr_count']:>3d} {tier:<13} "
            f"{_fmt_rank(r, n):>8} "
            f"{_fmt_score(r['composite']):>5} "
            f"{_fmt_score(r['power_score']):>5} "
            f"{_fmt_score(r['matchup_score']):>5} "
            f"{_fmt_score(r['form_score']):>5}  {sel}"
        )


def print_window_summary(window: list[tuple[str, dict]]) -> None:
    """Roll-up across all dates in the window."""
    if not window:
        return

    print()
    print("=== Window summary ===")
    header = (
        f"  {'Date':<12} {'HRs':>4} {'Sel':>4} {'OnBd':>5} {'OffBd':>6} "
        f"{'AvgRk':>6} {'TopDec':>7} {'TopQrt':>7} {'Board':>6}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    tot_hr = tot_sel = tot_on = tot_off = tot_dec = tot_qrt = 0
    weighted_rank_num = 0.0
    weighted_rank_den = 0
    for d, s in window:
        avg = s["avg_rank"]
        avg_s = f"{avg:5.1f}" if avg is not None else "  n/a"
        print(
            f"  {d:<12} {s['n_hitters']:>4d} {s['n_selected']:>4d} "
            f"{s['n_on_board']:>5d} {s['n_off_board']:>6d} {avg_s:>6} "
            f"{s['n_top_decile']:>7d} {s['n_top_quartile']:>7d} "
            f"{s['board_size']:>6d}"
        )
        tot_hr += s["n_hitters"]
        tot_sel += s["n_selected"]
        tot_on += s["n_on_board"]
        tot_off += s["n_off_board"]
        tot_dec += s["n_top_decile"]
        tot_qrt += s["n_top_quartile"]
        if avg is not None and s["n_on_board"]:
            weighted_rank_num += avg * s["n_on_board"]
            weighted_rank_den += s["n_on_board"]

    weighted_avg = (
        f"{weighted_rank_num / weighted_rank_den:5.1f}"
        if weighted_rank_den else "  n/a"
    )
    print("  " + "-" * (len(header) - 2))
    print(
        f"  {'TOTAL':<12} {tot_hr:>4d} {tot_sel:>4d} {tot_on:>5d} "
        f"{tot_off:>6d} {weighted_avg:>6} {tot_dec:>7d} {tot_qrt:>7d}"
    )
    print()
    if tot_hr:
        select_rate = 100.0 * tot_sel / tot_hr
        topdec_rate = 100.0 * tot_dec / tot_on if tot_on else 0
        topqrt_rate = 100.0 * tot_qrt / tot_on if tot_on else 0
        print(
            f"  Select rate (HR hitters in 8-pick card): {select_rate:.1f}%  "
            f"({tot_sel}/{tot_hr})"
        )
        print(
            f"  HR hitters in top decile (of those on-board):   "
            f"{topdec_rate:.1f}%  ({tot_dec}/{tot_on})"
        )
        print(
            f"  HR hitters in top quartile (of those on-board): "
            f"{topqrt_rate:.1f}%  ({tot_qrt}/{tot_on})"
        )
        if tot_off:
            off_rate = 100.0 * tot_off / tot_hr
            print(
                f"  Off-board HR hitters: {off_rate:.1f}%  ({tot_off}/{tot_hr}) "
                f"-- scoring couldn't have picked these"
            )
    print()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_dates(args, conn: sqlite3.Connection) -> list[str]:
    if args.date:
        return [args.date]

    if args.days and args.days > 1:
        # Anchor at the latest date with outcomes so weekends / off-days don't
        # produce empty rows for "today" before games have finished.
        latest = conn.execute(
            "SELECT MAX(date) FROM outcomes WHERE hr_count > 0"
        ).fetchone()[0]
        if not latest:
            return []
        end = datetime.strptime(latest, "%Y-%m-%d").date()
        start = end - timedelta(days=args.days - 1)
        return [
            (start + timedelta(days=i)).isoformat()
            for i in range((end - start).days + 1)
        ]

    # Default: yesterday (relative to system clock — same default as
    # etl_outcomes so the two scripts agree on what "yesterday" means).
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    return [yesterday]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--date", help="Specific date (YYYY-MM-DD); defaults to yesterday")
    p.add_argument("--days", type=int, help="Roll back N days from latest outcome date")
    p.add_argument("--summary-only", action="store_true",
                   help="Skip per-row tables; print only the window summary")
    p.add_argument("--db", default=str(DB_PATH), help=f"DB path (default: {DB_PATH})")
    args = p.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    dates = _resolve_dates(args, conn)
    if not dates:
        print("No outcomes data available; nothing to audit.", file=sys.stderr)
        sys.exit(1)

    window: list[tuple[str, dict]] = []
    for d in dates:
        rows = fetch_hr_audit(conn, d)
        if not rows:
            # Empty days are still informative when scanning a window — they
            # tell us no HRs were recorded (could be off-day OR ETL gap).
            continue
        n_board = board_size(conn, d)
        s = summarize_day(rows, n_board)
        window.append((d, s))
        if not args.summary_only:
            print_day_detail(d, rows, s)

    if not window:
        print("No HR hitters in the requested window.", file=sys.stderr)
        conn.close()
        sys.exit(1)

    if len(window) > 1 or args.summary_only:
        print_window_summary(window)

    conn.close()


if __name__ == "__main__":
    main()
