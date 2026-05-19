#!/usr/bin/env python3
"""
compute_lab_accuracy.py — Per-view hit-rate metrics for the dashboard Lab tab.

The Lab tab (mlb_hr_bet_site/index.html, renderLab()) renders 5 scored
views + 1 Game-Stacks view, all computed client-side from picks_latest.json.
Their historical accuracy is never stored. This script reconstructs each of
the 5 scored views for every past board in `daily_picks`, scores the batters
each view surfaced against the `outcomes` table, and writes a small JSON the
Lab tab reads to render an "L7" (last-7-slates) hit-rate badge on each card.

Game Stacks is intentionally excluded — its success metric is stack-level
(P(>=1 of N homers)), not a per-batter hit rate.

View logic mirrors renderLab() exactly:
  homerun_leaders  top10 by season_hr>=1; then top3 by composite-0.25*power
  power_matchup    top5 by power_score*matchup_score/100
  hot_streak       top10 by recent_hr_7d>=1; sorted by matchup*park*weather
  park_pitcher     season_hr>=5 & park>=65 & matchup>=70; top5 by park+matchup
  pure_longshots   season_hr<5 & composite>=55; top5 by composite

Derived fields are reconstructed AS OF each historical date, so a past board
is scored as it looked that day rather than with today's totals:
  season_hr     cumulative SUM(outcomes.hr_count) for date < X
  recent_hr_7d  SUM(outcomes.hr_count) for date(X,'-7 days') <= date < X
                (identical window to export_site_data.py's Hot Streak feed)
season_hr is sourced from `outcomes` rather than the `season_batting`
snapshot because season_batting is overwritten nightly — only its current
value survives, so it can't give a faithful as-of-date number for back days.

Scoring counts only surfaced batters with a box-score row that day; pre-game
scratches / DNPs are dropped from the denominator (not a hit-or-miss event).
A day is counted only if its confirmed-starter board joins the outcomes
table — this gates out both the season's first ~3 weeks (daily_picks.
batting_order is empty) and a few early boards loaded with a non-lineup
batting_order encoding whose batter ids don't join outcomes at all.

Usage:
    python compute_lab_accuracy.py                  # -> mlb_hr_bet_site/data/
    python compute_lab_accuracy.py --window 7
    python compute_lab_accuracy.py --db custom.db --out ./some/dir
"""

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from etl.db import get_db

# View key -> human label. Order matches the Lab tab top-to-bottom.
VIEW_LABELS = {
    "homerun_leaders": "Homerun Leaders",
    "power_matchup":   "Power x Matchup",
    "hot_streak":      "Hot Streak Watch",
    "park_pitcher":    "Park x Pitcher Exploit",
    "pure_longshots":  "Pure Longshots",
}


def atomic_write_json(path: Path, data, indent: int = 2) -> None:
    """Serialize into a temp file in the same dir, fsync, then os.replace().

    Mirrors export_site_data.atomic_write_json — a plain json.dump into an
    open file is not atomic, and OneDrive/Cloudflare can pick up a
    half-written file. The rename is atomic on Windows and POSIX.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                    dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def numeric_batting_order(value):
    """Return the batting order as an int 1-9, or None.

    daily_picks.batting_order is TEXT — '1'..'9' for confirmed starters,
    but also 'bench', 'roster_only', NULL, or (on a few malformed early
    boards) values >9. The Lab tab keeps only 1-9; we do the same.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 9 else None


def reconstruct_board(conn, date):
    """Return the confirmed-starter board for `date`, augmented with
    as-of-date season_hr and recent_hr_7d. Empty list if no usable rows."""
    board = []
    for r in conn.execute("SELECT * FROM daily_picks WHERE date = ?", (date,)):
        if numeric_batting_order(r["batting_order"]) is None:
            continue
        b = dict(r)
        for k in ("composite", "power_score", "matchup_score", "park_score",
                  "form_score", "weather_score", "lineup_score"):
            b[k] = b[k] or 0.0
        board.append(b)
    if not board:
        return board

    season_hr = {
        row["batter_id"]: (row["hr"] or 0)
        for row in conn.execute(
            "SELECT batter_id, SUM(hr_count) AS hr FROM outcomes "
            "WHERE date < ? GROUP BY batter_id", (date,))
    }
    recent_7d = {
        row["batter_id"]: (row["hr"] or 0)
        for row in conn.execute(
            "SELECT batter_id, SUM(hr_count) AS hr FROM outcomes "
            "WHERE date >= date(?, '-7 days') AND date < ? GROUP BY batter_id",
            (date, date))
    }
    for b in board:
        b["season_hr"] = season_hr.get(b["batter_id"], 0)
        b["recent_hr_7d"] = recent_7d.get(b["batter_id"], 0)
    return board


def views_for_board(board):
    """Replay the 5 scored Lab views. Returns {view_key: [batter_id, ...]}.

    Each formula is a 1:1 port of renderLab() in mlb_hr_bet_site/index.html.
    """
    top10_hr = sorted((b for b in board if b["season_hr"] >= 1),
                      key=lambda x: -x["season_hr"])[:10]
    homerun_leaders = sorted(
        top10_hr, key=lambda x: -(x["composite"] - 0.25 * x["power_score"]))[:3]

    power_matchup = sorted(
        board, key=lambda x: -(x["power_score"] * x["matchup_score"] / 100))[:5]

    top10_hot = sorted((b for b in board if b["recent_hr_7d"] >= 1),
                       key=lambda x: -x["recent_hr_7d"])[:10]
    hot_streak = sorted(top10_hot, key=lambda x: -(
        x["matchup_score"] / 100 * x["park_score"] / 100
        * x["weather_score"] / 100))

    park_pitcher = sorted(
        (b for b in board if b["season_hr"] >= 5
         and b["park_score"] >= 65 and b["matchup_score"] >= 70),
        key=lambda x: -(x["park_score"] + x["matchup_score"]))[:5]

    pure_longshots = sorted(
        (b for b in board if b["season_hr"] < 5 and b["composite"] >= 55),
        key=lambda x: -x["composite"])[:5]

    return {
        "homerun_leaders": [b["batter_id"] for b in homerun_leaders],
        "power_matchup":   [b["batter_id"] for b in power_matchup],
        "hot_streak":      [b["batter_id"] for b in hot_streak],
        "park_pitcher":    [b["batter_id"] for b in park_pitcher],
        "pure_longshots":  [b["batter_id"] for b in pure_longshots],
    }


def _rate(hits, picks):
    return round(100 * hits / picks, 1) if picks else None


def compute(conn, window: int):
    """Reconstruct + score every usable board. Returns the JSON payload."""
    pick_dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM daily_picks ORDER BY date")]
    outcome_dates = {r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM outcomes")}

    # (date, batter_id) -> homered?  — aggregated over doubleheaders. The key
    # set doubles as the "this batter has a box score that day" set: a
    # surfaced batter missing from it was a scratch / DNP and isn't scored.
    homered = {}
    for r in conn.execute("SELECT date, batter_id, SUM(hr_count) AS hr "
                          "FROM outcomes GROUP BY date, batter_id"):
        homered[(r["date"], r["batter_id"])] = (r["hr"] or 0) > 0

    # Usable date: has outcomes, a confirmed-starter board, and that board
    # actually joins the outcomes table. The join check drops the malformed
    # early boards whose batting_order used a non-lineup encoding.
    usable, boards = [], {}
    for d in pick_dates:
        if d not in outcome_dates:
            continue
        board = reconstruct_board(conn, d)
        if not board:
            continue
        if not any((d, b["batter_id"]) in homered for b in board):
            continue
        usable.append(d)
        boards[d] = board

    l7_dates = set(usable[-window:])

    def fresh():
        # [hits, picks] per window, plus the set of slates that contributed
        # at least one scored pick (the badge's "over N slates").
        return {"l7": [0, 0], "all": [0, 0], "l7_days": set(), "all_days": set()}

    view_acc = {k: fresh() for k in VIEW_LABELS}
    base = {"l7": [0, 0], "all": [0, 0]}

    for d in usable:
        in_l7 = d in l7_dates
        for b in boards[d]:
            if (d, b["batter_id"]) not in homered:
                continue
            hit = 1 if homered[(d, b["batter_id"])] else 0
            base["all"][0] += hit
            base["all"][1] += 1
            if in_l7:
                base["l7"][0] += hit
                base["l7"][1] += 1
        for key, batter_ids in views_for_board(boards[d]).items():
            acc = view_acc[key]
            for bid in batter_ids:
                if (d, bid) not in homered:
                    continue  # scratch / DNP — not a hit-or-miss event
                hit = 1 if homered[(d, bid)] else 0
                acc["all"][0] += hit
                acc["all"][1] += 1
                acc["all_days"].add(d)
                if in_l7:
                    acc["l7"][0] += hit
                    acc["l7"][1] += 1
                    acc["l7_days"].add(d)

    def bucket(pair, days):
        hits, picks = pair
        return {"hits": hits, "picks": picks, "rate": _rate(hits, picks),
                "days": days}

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of": usable[-1] if usable else None,
        "window_days": window,
        "baseline": {
            "l7_rate":  _rate(*base["l7"]),
            "all_rate": _rate(*base["all"]),
            "note": "confirmed-starter board base HR rate (same window)",
        },
        "views": {
            key: {
                "label": VIEW_LABELS[key],
                "l7":  bucket(view_acc[key]["l7"], len(view_acc[key]["l7_days"])),
                "all": bucket(view_acc[key]["all"], len(view_acc[key]["all_days"])),
            }
            for key in VIEW_LABELS
        },
    }


def main():
    ap = argparse.ArgumentParser(description="Compute Lab-tab per-view hit rates")
    ap.add_argument("--db", default=None, help="Custom DB path")
    ap.add_argument("--out", default=None,
                    help="Output dir (default: mlb_hr_bet_site/data)")
    ap.add_argument("--window", type=int, default=7,
                    help="Rolling window in slates for the L7 badge (default 7)")
    args = ap.parse_args()

    out_dir = (Path(args.out) if args.out
               else Path(__file__).parent / "mlb_hr_bet_site" / "data")
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = get_db(Path(args.db) if args.db else None)
    try:
        payload = compute(conn, args.window)
    finally:
        conn.close()

    atomic_write_json(out_dir / "lab_accuracy.json", payload)

    win = args.window
    print(f"Wrote {out_dir / 'lab_accuracy.json'}  "
          f"(as_of {payload['as_of']}, L{win} over "
          f"{payload['views']['hot_streak']['l7']['days']} slates)")
    for key, v in payload["views"].items():
        l7, al = v["l7"], v["all"]
        print(f"  {v['label']:24} L{win} {str(l7['rate'])+'%':>7} "
              f"({l7['hits']}/{l7['picks']})   season {str(al['rate'])+'%':>7} "
              f"({al['hits']}/{al['picks']})")
    print(f"  {'baseline (board)':24} L{win} "
          f"{str(payload['baseline']['l7_rate'])+'%':>7}"
          f"            season {str(payload['baseline']['all_rate'])+'%':>7}")


if __name__ == "__main__":
    main()
