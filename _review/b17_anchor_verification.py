"""
B17 verification — power input anchor recalibration.

One-off script. Pulls 2025 backfill rows from pick_inputs for each affected
input, computes empirical p10/p25/p50/p75/p90, re-scores under the new
anchors, reports p50_score, %@0, %@100. Cross-references 2026 live where
available.

Usage:
    python _review/b17_anchor_verification.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from etl.db import get_db

# (column, OLD anchor, NEW anchor)
INPUTS = [
    ("xwoba_contact",            (0.330, 0.450), (0.260, 0.390)),
    ("barrel_pct",               (5.0,   15.0),  (3.0,   11.0)),
    ("iso",                      (0.130, 0.300), (0.100, 0.250)),
    ("hr_fb_pct",                (8.0,   20.0),  (3.0,   10.0)),
    ("recent_xwoba_contact_14d", (0.330, 0.450), (0.225, 0.410)),
]


def min_max_scale(value: float, lo: float, hi: float) -> float:
    if hi == lo:
        return 50.0
    return max(0, min(100, (value - lo) / (hi - lo) * 100))


def quantiles(vals: list[float]) -> dict:
    arr = np.array(vals)
    return {
        "n": len(arr),
        "p10": float(np.quantile(arr, 0.10)),
        "p25": float(np.quantile(arr, 0.25)),
        "p50": float(np.quantile(arr, 0.50)),
        "p75": float(np.quantile(arr, 0.75)),
        "p90": float(np.quantile(arr, 0.90)),
    }


def score_dist(vals: list[float], lo: float, hi: float) -> dict:
    scored = np.array([min_max_scale(v, lo, hi) for v in vals])
    return {
        "p50_score": float(np.quantile(scored, 0.50)),
        "pct_at_0": float((scored == 0).mean() * 100),
        "pct_at_100": float((scored == 100).mean() * 100),
        "mean_score": float(scored.mean()),
    }


SAMPLES = {
    "2025_backfill": (
        "SELECT pi.{col} FROM pick_inputs pi "
        "INNER JOIN daily_picks dp ON dp.date = pi.date AND dp.batter_id = pi.batter_id "
        "WHERE dp.mode = 'backfill_2025' AND pi.{col} IS NOT NULL AND pi.{col} > 0"
    ),
    "2026_live": (
        "SELECT pi.{col} FROM pick_inputs pi "
        "WHERE pi.date >= '2026-05-03' AND pi.{col} IS NOT NULL AND pi.{col} > 0"
    ),
}


def fetch_vals(conn, col: str, sample_sql: str) -> list[float]:
    sql = sample_sql.format(col=col)
    return [float(r[0]) for r in conn.execute(sql).fetchall()]


def main():
    conn = get_db()
    print("# B17 verification — power input anchor recalibration")
    print("# pick_inputs 2025 backfill (daily_picks.mode='backfill_2025')")
    print("# Calibration rule: empirical p10 -> score 0, empirical p90 -> score 100")
    print("# Acceptance: p50_score in [40, 60], <15% clamped @ 0, <15% clamped @ 100")
    print()

    summary_rows = []
    for col, old_anchor, new_anchor in INPUTS:
        print(f"## {col}")
        print(f"   OLD anchor: {old_anchor}")
        print(f"   NEW anchor: {new_anchor}")
        for sample_name, sql in SAMPLES.items():
            vals = fetch_vals(conn, col, sql)
            if not vals:
                print(f"   - {sample_name}: NO ROWS (column NULL for this sample)")
                continue
            q = quantiles(vals)
            sd_old = score_dist(vals, *old_anchor)
            sd_new = score_dist(vals, *new_anchor)
            print(
                f"   - {sample_name}: n={q['n']:>6}  "
                f"p10={q['p10']:.4f}  p25={q['p25']:.4f}  "
                f"p50={q['p50']:.4f}  p75={q['p75']:.4f}  p90={q['p90']:.4f}"
            )
            print(
                f"     OLD: p50_score={sd_old['p50_score']:>5.1f}  "
                f"%@0={sd_old['pct_at_0']:>5.1f}%  %@100={sd_old['pct_at_100']:>5.1f}%"
            )
            print(
                f"     NEW: p50_score={sd_new['p50_score']:>5.1f}  "
                f"%@0={sd_new['pct_at_0']:>5.1f}%  %@100={sd_new['pct_at_100']:>5.1f}%"
            )
            verdict = (
                40 <= sd_new["p50_score"] <= 60
                and sd_new["pct_at_0"] < 15
                and sd_new["pct_at_100"] < 15
            )
            print(f"     ACCEPT (p50_score in [40,60] AND %@0<15 AND %@100<15): {'PASS' if verdict else 'FAIL'}")
            if sample_name == "2025_backfill" or (
                sample_name == "2026_live" and col == "xwoba_contact"
            ):
                summary_rows.append((col, sample_name, q, sd_new, verdict))
        print()

    print("## Summary (anchor-setting samples)")
    print("| Input | Sample | n | p10 | p50 | p90 | p50_score (NEW) | %@0 | %@100 | ACCEPT |")
    print("|---|---|---:|---:|---:|---:|---:|---:|---:|:---:|")
    for col, sample, q, sd, ok in summary_rows:
        print(
            f"| `{col}` | {sample} | {q['n']} | {q['p10']:.4f} | {q['p50']:.4f} | {q['p90']:.4f} "
            f"| {sd['p50_score']:.1f} | {sd['pct_at_0']:.1f}% | {sd['pct_at_100']:.1f}% "
            f"| {'PASS' if ok else 'FAIL'} |"
        )


if __name__ == "__main__":
    main()
