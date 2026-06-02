"""
simulate_power_anchors.py
-------------------------
Read-only simulator: replays today's pick_inputs through a *proposed*
score_power with tightened anchors, then re-ranks composite to show
exactly how the picks would shift.

Anchors compared:
  barrel:        0-25         -> 3-17
  exit_velo:     80-100       -> 85-93
  hr_fb:         0-30         -> 5-22
  iso:           0.100-0.350  -> 0.130-0.280
  xwoba_contact: 0.280-0.500  (unchanged -- already calibrated to MLB)
  pull_fb_pct:   5-25         (unchanged)

Composite delta uses stored_composite + POWER_WEIGHT * (new_power - old_power)
because power_score is NOT slate-reranked at scoring time. POWER_WEIGHT is the
live default power weight (WEIGHT_CONFIGS["default"]["power"]). The script also
reproduces the CURRENT score_power and prints |repro - stored| so you can see
if my reproduction matches reality (it should be ~0).

Usage (from project root):
    python diagnostics/simulate_power_anchors.py [--db PATH]
"""

import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np

# This diagnostic lives in diagnostics/; put the repo root on sys.path so
# `etl.db` and `score_batters` import regardless of the cwd it's run from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from etl.db import DB_PATH            # canonical, HR_BETS_DB-aware DB path
from score_batters import WEIGHT_CONFIGS

POWER_WEIGHT = WEIGHT_CONFIGS["default"]["power"]  # live default power weight (0.48)
TOP_N = 30


def min_max_scale(value, min_val, max_val):
    if value is None:
        return None
    if value <= min_val:
        return 0.0
    if value >= max_val:
        return 100.0
    return (value - min_val) / (max_val - min_val) * 100.0


def _power(b, anchors):
    """Mirror score_batters.score_power exactly, with swappable anchors."""
    scores = []
    if b.get("barrel_pct") is not None and b["barrel_pct"] > 0:
        scores.append(min_max_scale(b["barrel_pct"], *anchors["barrel"]))
    if b.get("exit_velo") is not None and b["exit_velo"] > 0:
        scores.append(min_max_scale(b["exit_velo"], *anchors["ev"]))
    if b.get("hr_fb_pct") is not None and b["hr_fb_pct"] > 0:
        v = b["hr_fb_pct"] * 100 if b["hr_fb_pct"] < 1 else b["hr_fb_pct"]
        scores.append(min_max_scale(v, *anchors["hr_fb"]))
    if b.get("iso") is not None and b["iso"] > 0:
        scores.append(min_max_scale(b["iso"], *anchors["iso"]))
    if b.get("xwoba_contact") is not None and b["xwoba_contact"] > 0:
        scores.append(min_max_scale(b["xwoba_contact"], 0.280, 0.500))
    if b.get("pull_fb_pct") is not None and b["pull_fb_pct"] > 0:
        v = b["pull_fb_pct"] * 100 if b["pull_fb_pct"] < 1 else b["pull_fb_pct"]
        scores.append(min_max_scale(v, 5, 25))
    return float(np.mean(scores)) if scores else 50.0


CURRENT_ANCHORS = {
    "barrel": (0, 25),
    "ev":     (80, 100),
    "hr_fb":  (0, 30),
    "iso":    (0.100, 0.350),
}

PROPOSED_ANCHORS = {
    "barrel": (3, 17),
    "ev":     (85, 93),
    "hr_fb":  (5, 22),
    "iso":    (0.130, 0.280),
}


def main(db_path=None):
    db_path = Path(db_path) if db_path else DB_PATH
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    latest = conn.execute("SELECT MAX(date) FROM pick_inputs").fetchone()[0]
    print(f"Simulating against {latest}")
    print(f"Anchors: current {CURRENT_ANCHORS} -> proposed {PROPOSED_ANCHORS}")
    print(f"Power weight: {POWER_WEIGHT}\n")

    # composite + power_score live on daily_picks, NOT pick_inputs.
    # daily_picks contains every scored batter (~277/day) with selected=1
    # marking the top-8 final picks.
    rows = conn.execute(
        """
        SELECT pi.barrel_pct, pi.exit_velo, pi.hr_fb_pct, pi.iso,
               pi.xwoba_contact, pi.pull_fb_pct,
               pi.batter_id,
               dp.batter_name,
               dp.composite,
               dp.power_score,
               dp.selected     AS is_pick,
               dp.rank_in_board
        FROM pick_inputs pi
        INNER JOIN daily_picks dp
          ON dp.date = pi.date AND dp.batter_id = pi.batter_id
        WHERE pi.date = ?
        ORDER BY dp.composite DESC
        """,
        (latest,),
    ).fetchall()
    conn.close()

    if not rows:
        print(f"No pick_inputs rows for {latest} -- nothing to simulate.")
        return

    results = []
    for r in rows:
        b = dict(r)
        old_power = b.get("power_score") or 0.0
        repro_power = _power(b, CURRENT_ANCHORS)
        new_power = _power(b, PROPOSED_ANCHORS)
        old_comp = b.get("composite") or 0.0
        new_comp = old_comp + POWER_WEIGHT * (new_power - old_power)
        results.append({
            "name": b.get("batter_name") or f"id={b.get('batter_id')}",
            "is_pick": bool(b.get("is_pick")),
            "old_power": old_power,
            "repro_power": repro_power,
            "new_power": new_power,
            "old_comp": old_comp,
            "new_comp": new_comp,
        })

    # Stable rank lookups via list-index identity
    by_old = sorted(results, key=lambda x: x["old_comp"], reverse=True)
    by_new = sorted(results, key=lambda x: x["new_comp"], reverse=True)
    old_rank = {id(r): i + 1 for i, r in enumerate(by_old)}
    new_rank = {id(r): i + 1 for i, r in enumerate(by_new)}

    # ------------------------------------------------------------------
    # Top-N table by NEW composite, with old-rank delta
    # ------------------------------------------------------------------
    print(f"=== Top {TOP_N} by PROPOSED composite (* = was in today's top-8 picks) ===")
    print(f"{'Pk':<3}{'NewR':<6}{'OldR':<6}{'dRk':<5}{'Name':<22}"
          f"{'OldP':>7}{'NewP':>7}{'dP':>7}{'OldC':>7}{'NewC':>7}{'dC':>7}")
    print("-" * 92)
    for r in by_new[:TOP_N]:
        nr = new_rank[id(r)]
        orr = old_rank[id(r)]
        d = orr - nr  # positive = climbed
        d_str = f"+{d}" if d > 0 else (str(d) if d < 0 else ".")
        marker = "*" if r["is_pick"] else " "
        print(
            f"{marker:<3}{nr:<6}{orr:<6}{d_str:<5}{r['name'][:20]:<22}"
            f"{r['old_power']:>7.1f}{r['new_power']:>7.1f}"
            f"{(r['new_power']-r['old_power']):>+7.1f}"
            f"{r['old_comp']:>7.1f}{r['new_comp']:>7.1f}"
            f"{(r['new_comp']-r['old_comp']):>+7.1f}"
        )

    # ------------------------------------------------------------------
    # Top-8 churn: who's IN under new that was OUT under old, and vice versa
    # ------------------------------------------------------------------
    old_top8 = {id(r) for r in by_old[:8]}
    new_top8 = {id(r) for r in by_new[:8]}
    new_in   = [r for r in by_new[:8] if id(r) not in old_top8]
    new_out  = [r for r in by_old[:8] if id(r) not in new_top8]

    print(f"\n=== Top-8 pick churn ===")
    print(f"Same picks: {8 - len(new_in)} of 8")
    if new_in:
        print(f"NEW into top-8 (would be added):")
        for r in new_in:
            print(f"  + {r['name'][:22]:<24} oldR {old_rank[id(r)]:>3} -> newR {new_rank[id(r)]:>3} | "
                  f"power {r['old_power']:.1f} -> {r['new_power']:.1f}")
    if new_out:
        print(f"DROPPED from top-8 (would be removed):")
        for r in new_out:
            print(f"  - {r['name'][:22]:<24} oldR {old_rank[id(r)]:>3} -> newR {new_rank[id(r)]:>3} | "
                  f"power {r['old_power']:.1f} -> {r['new_power']:.1f}")

    # ------------------------------------------------------------------
    # Biggest climbers / droppers anywhere in the slate
    # ------------------------------------------------------------------
    movers = sorted(results, key=lambda x: old_rank[id(x)] - new_rank[id(x)], reverse=True)
    print(f"\n=== Biggest climbers (top 10) ===")
    for r in movers[:10]:
        d = old_rank[id(r)] - new_rank[id(r)]
        print(f"  {r['name'][:22]:<24} {old_rank[id(r)]:>3} -> {new_rank[id(r)]:>3} ({d:+d}) | "
              f"power {r['old_power']:.1f} -> {r['new_power']:.1f}")
    print(f"\n=== Biggest droppers (top 10) ===")
    for r in movers[-10:]:
        d = old_rank[id(r)] - new_rank[id(r)]
        print(f"  {r['name'][:22]:<24} {old_rank[id(r)]:>3} -> {new_rank[id(r)]:>3} ({d:+d}) | "
              f"power {r['old_power']:.1f} -> {r['new_power']:.1f}")

    # ------------------------------------------------------------------
    # Sanity check: does my reproduced score_power match what's stored?
    # ------------------------------------------------------------------
    repro_diffs = [abs(r["repro_power"] - r["old_power"]) for r in results]
    max_diff = max(repro_diffs) if repro_diffs else 0.0
    n_off = sum(1 for d in repro_diffs if d > 0.1)
    print(f"\n=== Reproduction sanity ===")
    print(f"  N rows: {len(results)}")
    print(f"  Max |reproduced - stored| power_score: {max_diff:.3f}")
    print(f"  Rows where reproduction differs from stored by > 0.1: {n_off}")
    if n_off > 0:
        print(f"  --> WARN: stored power_score has drift vs current code path. Composite deltas")
        print(f"      printed above are still computed against stored composite, so the")
        print(f"      direction is right but absolute numbers may be slightly off.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Read-only score_power anchor simulator (re-ranks today's slate)."
    )
    parser.add_argument(
        "--db", default=None,
        help="DB path override (default: canonical etl.db.DB_PATH, HR_BETS_DB-aware)",
    )
    args = parser.parse_args()
    main(args.db)
