#!/usr/bin/env python3
"""
revalidate_picks.py — pre-game re-check of the published card (audit P0-1 / P1-4).

Why this exists. The daily pipeline is scheduled for 09:07 ET and, with GitHub
Actions cron drift, actually fires anywhere from ~9:30 AM to ~7 PM ET. Most
days it scores on the recent-lineup fallback (yesterday's batting order), and
2026-06-03 → 09-06 that put 38 of 738 published picks (5.1%) on the card for
a game they never played: not in the posted lineup, or the game was postponed
after the run. A pick that does not play is a guaranteed miss.

What it does. For today's card (daily_picks, selected=1):
  1. Re-fetch the schedule (game status) and every posted lineup.
  2. A pick is INVALID when its game is dead (postponed / cancelled /
     suspended) or its side's lineup is posted and the batter is not in it.
     Games already in progress or final are left alone — that card is
     history and the bet is placed.
  3. Each invalid pick is replaced by the next-best row on the stored full
     board (composite order, same rules as generate_picks: confirmed 1-9
     starter — re-checked against the posted lineup when there is one —
     not likely-out, game not dead and not started, max 2 per game, no
     duplicate names).
  4. daily_picks is updated in place: removed rows get selected=0 +
     status_description='revalidate <ts>: <reason>'; replacements get
     selected=1 + promoted_due_to='revalidate' (+ the posted batting
     order). is_likely_out is NOT touched — that column is the B7 roster
     status flag (IL / paternity) and feeds its own audit; a scratched or
     rained-out pick is a different thing. Nothing is rescored; composites
     are the morning's.

Caveats. A row whose game_pk cannot be resolved is treated as pending and
kept. A same-day re-run of the morning pipeline (load_picks_to_db DELETEs
the date) rebuilds the board from scratch and drops any earlier swap — it
logs a warning when that happens; run this again afterwards.

The workflow that runs this (revalidate-picks.yml) re-exports the site JSON
and pushes only when something changed. Idempotent: a second run on an
unchanged slate is a no-op.

Usage:
    python revalidate_picks.py                    # today, canonical DB
    python revalidate_picks.py --date 2026-09-07
    python revalidate_picks.py --dry-run          # plan + log, no writes
    python revalidate_picks.py --db path/to.db

Exit codes: 0 always for the pipeline (fail-soft, like fetch_pick_odds);
--strict makes a crash exit 1. Prints `REVALIDATE_CHANGED=1|0` on the last
line so the workflow can gate the export + push on it.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from etl.db import get_db, create_tables

MAX_PER_GAME = 2
N_PICKS = 8

# detailedState families. A game we may still act on is one that has not
# started; anything else is left exactly as the morning run published it.
DEAD_KEYWORDS = ("postpon", "cancel", "suspend")
NOT_STARTED_PREFIXES = ("scheduled", "pre-game", "warmup", "delayed")


def classify_status(detailed_state: str | None) -> str:
    """'dead' | 'pending' | 'started' from an MLB schedule detailedState."""
    s = (detailed_state or "").strip().lower()
    if any(k in s for k in DEAD_KEYWORDS):
        return "dead"
    if not s or s.startswith(NOT_STARTED_PREFIXES):
        return "pending"
    return "started"


# ---------------------------------------------------------------------------
# Pure planning — no I/O, pinned in tests/smoke.py
# ---------------------------------------------------------------------------

def plan_revalidation(
    board: list[dict],
    posted: dict[int, dict],
    game_status: dict[int, str],
    max_per_game: int = MAX_PER_GAME,
    n_picks: int = N_PICKS,
) -> dict:
    """
    board:       daily_picks rows for the date. Each: id, batter_id, batter_name,
                 game_pk, side ('home'/'away'/None), composite, batting_order
                 (str), selected (0/1), is_likely_out (0/1).
    posted:      {game_pk: {"home": {player_id: order}, "away": {...}}} — a
                 side is present only when its lineup is actually posted.
    game_status: {game_pk: detailedState}.

    Returns {"removed": [(row, reason)], "added": [row], "kept": [row]}.
    """
    def side_lineup(row):
        g = posted.get(row.get("game_pk")) or {}
        side = row.get("side")
        return g.get(side) if side else None

    def invalid_reason(row):
        status = classify_status(game_status.get(row.get("game_pk")))
        if status == "dead":
            return f"game {game_status.get(row.get('game_pk'))}"
        if status == "started":
            return None  # too late to act; leave it
        lineup = side_lineup(row)
        if lineup is not None and row["batter_id"] not in lineup:
            return "not in posted lineup"
        return None

    selected = [r for r in board if int(r.get("selected") or 0) == 1]
    kept, removed = [], []
    for r in selected:
        reason = invalid_reason(r)
        (removed if reason else kept).append((r, reason) if reason else r)

    if not removed:
        return {"removed": [], "added": [], "kept": kept}

    per_game: dict[int, int] = {}
    names: set[str] = set()
    for r in kept:
        per_game[r["game_pk"]] = per_game.get(r["game_pk"], 0) + 1
        names.add(r.get("batter_name") or "")

    added = []
    candidates = sorted(
        (r for r in board if int(r.get("selected") or 0) == 0),
        key=lambda r: -(r.get("composite") or 0),
    )
    for c in candidates:
        if len(kept) + len(added) >= n_picks:
            break
        name = c.get("batter_name") or ""
        if name in names:
            continue
        if int(c.get("is_likely_out") or 0):
            continue
        status = classify_status(game_status.get(c.get("game_pk")))
        if status != "pending":
            continue
        lineup = side_lineup(c)
        if lineup is not None:
            if c["batter_id"] not in lineup:
                continue
            bo = lineup[c["batter_id"]]
        else:
            try:
                bo = int(c.get("batting_order"))
            except (TypeError, ValueError):
                continue
        if not (1 <= bo <= 9):
            continue
        if per_game.get(c["game_pk"], 0) >= max_per_game:
            continue
        c = dict(c)
        c["batting_order"] = bo
        added.append(c)
        names.add(name)
        per_game[c["game_pk"]] = per_game.get(c["game_pk"], 0) + 1

    return {"removed": removed, "added": added, "kept": kept}


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def load_board(conn, date_str: str) -> list[dict]:
    """daily_picks rows for the date with the batter's side resolved via
    daily_slate (team abbreviation -> home/away)."""
    from generate_picks import TEAM_ABBREV_TO_FULL  # lazy: heavy module

    slate = {
        int(r[0]): (r[1], r[2])
        for r in conn.execute(
            "SELECT game_pk, home_team, away_team FROM daily_slate WHERE date = ?",
            (date_str,),
        ).fetchall()
    }
    rows = conn.execute(
        """
        SELECT id, batter_id, batter_name, team, game_pk, composite,
               batting_order, selected, is_likely_out, rank_in_board
        FROM daily_picks
        WHERE date = ? AND mode = 'live'
        """,
        (date_str,),
    ).fetchall()
    board = []
    for r in rows:
        gpk = int(r[4]) if r[4] is not None else None
        home, away = slate.get(gpk, (None, None))
        team = r[3] or ""
        full = TEAM_ABBREV_TO_FULL.get(team, team)
        side = None
        if home and (team == home or full == home):
            side = "home"
        elif away and (team == away or full == away):
            side = "away"
        board.append({
            "id": r[0], "batter_id": int(r[1]), "batter_name": r[2], "team": team,
            "game_pk": gpk, "composite": r[5], "batting_order": r[6],
            "selected": r[7], "is_likely_out": r[8], "rank_in_board": r[9],
            "side": side,
        })
    return board


def fetch_live_context(date_str: str) -> tuple[dict[int, dict], dict[int, str]]:
    """(posted lineups by game/side, detailedState by game) from the MLB API."""
    from fetch_daily_data import fetch_lineups_for_date, get_schedule

    posted: dict[int, dict] = {}
    for gpk, entry in (fetch_lineups_for_date(date_str) or {}).items():
        sides = {}
        for side in ("home", "away"):
            players = entry.get(side) or []
            if players:
                sides[side] = {
                    int(p["player_id"]): p.get("batting_order") or 99
                    for p in players if p.get("player_id")
                }
        if sides:
            posted[int(gpk)] = sides
    status = {int(g["game_pk"]): g.get("status") for g in get_schedule(date_str)}
    return posted, status


def apply_plan(conn, plan: dict, dry_run: bool) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for row, reason in plan["removed"]:
        print(f"  [revalidate] REMOVE  {row['batter_name']:<24} gpk={row['game_pk']}  {reason}")
        if not dry_run:
            conn.execute(
                """
                UPDATE daily_picks
                SET selected = 0, status_description = ?
                WHERE id = ?
                """,
                (f"revalidate {now}: {reason}"[:120], row["id"]),
            )
    for row in plan["added"]:
        print(f"  [revalidate] ADD     {row['batter_name']:<24} gpk={row['game_pk']}  "
              f"composite={row['composite']:.1f} bo={row['batting_order']}")
        if not dry_run:
            conn.execute(
                """
                UPDATE daily_picks
                SET selected = 1, promoted_due_to = 'revalidate',
                    batting_order = ?
                WHERE id = ?
                """,
                (str(row["batting_order"]), row["id"]),
            )
    if not dry_run:
        conn.commit()


def run(date_str: str, db_path: str | None, dry_run: bool) -> bool:
    conn = get_db(db_path)
    create_tables(conn)
    board = load_board(conn, date_str)
    if not board:
        print(f"  [revalidate] no live daily_picks rows for {date_str} - nothing to check")
        return False
    n_sel = sum(1 for r in board if int(r.get("selected") or 0) == 1)
    posted, status = fetch_live_context(date_str)
    n_posted_sides = sum(len(v) for v in posted.values())
    n_games = len(status)
    st_counts = {}
    for s in status.values():
        k = classify_status(s)
        st_counts[k] = st_counts.get(k, 0) + 1
    print(f"  [revalidate] {date_str}: {n_sel} selected on a {len(board)}-row board; "
          f"{n_games} games ({st_counts}); {n_posted_sides}/{2 * n_games} lineup sides posted")

    plan = plan_revalidation(board, posted, status)
    if not plan["removed"]:
        print("  [revalidate] every pick still valid - no change")
        return False
    apply_plan(conn, plan, dry_run)
    short = N_PICKS - len(plan["kept"]) - len(plan["added"])
    if short > 0:
        print(f"  [revalidate] WARNING: only {N_PICKS - short} picks after replacement "
              f"({short} slot(s) unfilled - no eligible pending-game batter left)")
    print(f"  [revalidate] SUMMARY {date_str}: removed {len(plan['removed'])}, "
          f"added {len(plan['added'])}, kept {len(plan['kept'])}"
          + (" (DRY RUN)" if dry_run else ""))
    return not dry_run


def main() -> int:
    ap = argparse.ArgumentParser(description="Pre-game re-check of today's published picks")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--db", default=None, help="Path to hr_bets.db (default: canonical)")
    ap.add_argument("--dry-run", action="store_true", help="Plan + log, no writes")
    ap.add_argument("--strict", action="store_true", help="Exit 1 on a crash (default: 0, fail-soft)")
    args = ap.parse_args()
    date_str = args.date or datetime.now().strftime("%Y-%m-%d")
    changed = False
    try:
        changed = run(date_str, args.db, args.dry_run)
    except Exception as e:
        print(f"  [revalidate] FAILED (non-fatal): {type(e).__name__}: {e}")
        print("REVALIDATE_CHANGED=0")
        return 1 if args.strict else 0
    print(f"REVALIDATE_CHANGED={1 if changed else 0}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
