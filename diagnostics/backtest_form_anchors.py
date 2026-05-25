#!/usr/bin/env python3
"""
backtest_form_anchors.py - sweep Form anchor + weighting candidates.

Form (score_form, weight 0.279) reads 3 active inputs from each batter's
recent gameLog (the 4th, ev_trend, is always None until A2):
  - recent_hr_10g    anchor 0-5
  - recent_iso_30g   anchor 0.100-0.300
  - recent_avg_30g   anchor 0.210-0.330  <-- clamps slumping-but-HR-active hitters
The three terms are equal-mean averaged.

Motivating cases: feast-or-famine power hitters get dragged. Harrison
Bader on 2026-05-23 had 3 HRs in his last 10 games + recent_iso_30g of
0.186 (real power signal) but recent_avg_30g of 0.163 - clamped to 0 by
the anchor floor. Form score 35.1. He hit a grand slam that day. Same
pattern, different shade: Jacob Young 5/20.

This harness re-scores the Form factor under six variants and grades
each against actual HR outcomes:

  current        - baseline (0.210 AVG floor, equal-mean weighting)
  avg_floor_180  - lower AVG floor 0.210 -> 0.180 ("too tight?" test)
  no_avg         - drop AVG term entirely
  2x_hr          - weight recent_hr_10g 2x in the mean
  hr_iso_only    - HR + ISO only, equal mean (drop AVG)
  hr_only        - extreme test: only recent_hr_10g

Headline numbers run on the COMMON SUBSET (rows where recent_hr_10g is
non-NULL - the minimum input every variant relies on) for apples-to-
apples comparison. The layoff dampener is deliberately NOT applied: this
is about input anchors + weighting, not the dampener layer.

Metrics on the raw Form score vs hit_hr - factor measured in isolation:

  - auc         - ROC-AUC; P(HR-hitter scored above non-hitter). >0.5 better.
  - top10_lift  - HR rate in top decile of form / overall HR rate. >1 better.
  - quint_mono  - monotone-up steps as Form rises across 5 quintiles (max 4).
  - avg_rank_hr - mean within-date rank of HR hitters (lower better).

Usage:
    python diagnostics/backtest_form_anchors.py
    python diagnostics/backtest_form_anchors.py --start 2025-03-27 --end 2025-09-30
    python diagnostics/backtest_form_anchors.py --days 30

Caveat: like backtest_power_inputs, this grades the FACTOR in isolation.
A change that improves Form's AUC may or may not move composite rankings
- that integration is the A1 refit's job.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

from etl.db import DB_PATH


VARIANTS = (
    "current",
    "avg_floor_180",
    "no_avg",
    "2x_hr",
    "hr_iso_only",
    "hr_only",
)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def fetch_rows(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    """pick_inputs JOINed with outcomes (the HR label) over [start, end]."""
    rows = conn.execute(
        """
        SELECT
            pi.date,
            pi.batter_id,
            pi.recent_hr_10g,
            pi.recent_iso_30g,
            pi.recent_avg_30g,
            pi.ev_trend,
            CASE WHEN COALESCE(o.hr_count, 0) > 0 THEN 1 ELSE 0 END AS hit_hr
        FROM pick_inputs pi
        INNER JOIN outcomes o
                ON o.date = pi.date AND o.batter_id = pi.batter_id
        WHERE pi.date >= ? AND pi.date <= ?
        """,
        (start, end),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Variant scoring
# ---------------------------------------------------------------------------

def _scale(x: float, lo: float, hi: float) -> float:
    """Clamp + scale into 0-100, mirroring score_batters.min_max_scale."""
    return max(0.0, min(100.0, (x - lo) / (hi - lo) * 100.0))


def _form_score(row: dict, variant: str) -> float:
    """Compute the Form score under one variant. No dampener applied -
    this isolates the input-formula change from the layoff layer."""
    hr10 = row.get("recent_hr_10g")
    iso30 = row.get("recent_iso_30g")
    avg30 = row.get("recent_avg_30g")
    ev_trend = row.get("ev_trend")

    pieces: list[tuple[float, float]] = []  # (weight, value)

    # recent_hr_10g - all variants use it. 2x_hr doubles its weight.
    if hr10 is not None:
        weight = 2.0 if variant == "2x_hr" else 1.0
        pieces.append((weight, _scale(hr10, 0.0, 5.0)))

    # recent_iso_30g - skipped only by hr_only.
    if iso30 is not None and iso30 > 0 and variant != "hr_only":
        pieces.append((1.0, _scale(iso30, 0.100, 0.300)))

    # recent_avg_30g - variant-dependent (skipped or floor-lowered).
    if avg30 is not None and avg30 > 0 and variant not in (
        "no_avg", "hr_iso_only", "hr_only",
    ):
        floor = 0.180 if variant == "avg_floor_180" else 0.210
        pieces.append((1.0, _scale(avg30, floor, 0.330)))

    # ev_trend - currently always None in production data (gated on A2).
    if ev_trend is not None:
        pieces.append((1.0, _scale(ev_trend, -3.0, 3.0)))

    if not pieces:
        return 50.0
    total_w = sum(w for w, _ in pieces)
    return sum(w * v for w, v in pieces) / total_w


def score_variants(rows: list[dict]) -> list[dict]:
    """Score every row under every variant."""
    out = []
    for r in rows:
        out.append({
            "date": r["date"],
            "batter_id": r["batter_id"],
            "hit_hr": r["hit_hr"],
            "has_hr10": r.get("recent_hr_10g") is not None,
            "form": {v: _form_score(r, v) for v in VARIANTS},
        })
    return out


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _auc(values: list[float], labels: list[int]) -> float | None:
    """ROC-AUC via Mann-Whitney U with tie-averaged ranks. O(n log n)."""
    v = np.asarray(values, dtype=float)
    y = np.asarray(labels, dtype=float)
    n_pos = float(y.sum())
    n_neg = float(len(y) - n_pos)
    if n_pos == 0 or n_neg == 0:
        return None
    order = np.argsort(v, kind="mergesort")
    sv = v[order]
    ranks = np.empty(len(v), dtype=float)
    i = 0
    n = len(v)
    while i < n:
        j = i
        while j < n and sv[j] == sv[i]:
            j += 1
        ranks[order[i:j]] = (i + j - 1) / 2.0 + 1.0
        i = j
    rank_pos = ranks[y == 1].sum()
    u = rank_pos - n_pos * (n_pos + 1) / 2.0
    return u / (n_pos * n_neg)


def _top_decile_lift(values: list[float], labels: list[int]) -> float | None:
    n = len(values)
    if n == 0:
        return None
    overall = sum(labels) / n
    if overall == 0:
        return None
    paired = sorted(zip(values, labels), key=lambda t: t[0], reverse=True)
    cut = max(1, n // 10)
    top = paired[:cut]
    return (sum(y for _, y in top) / len(top)) / overall


def _quintile_rates(values: list[float], labels: list[int]) -> list[float]:
    n = len(values)
    if n < 10:
        return []
    paired = sorted(zip(values, labels), key=lambda t: t[0])
    binsz = n // 5
    rates = []
    for q in range(5):
        lo = q * binsz
        hi = (q + 1) * binsz if q < 4 else n
        chunk = paired[lo:hi]
        rates.append(sum(y for _, y in chunk) / len(chunk) if chunk else float("nan"))
    return rates


def compute_metrics(rows: list[dict], variant: str) -> dict:
    """Grade one variant's form score on the given rows."""
    values = [r["form"][variant] for r in rows]
    labels = [r["hit_hr"] for r in rows]
    n = len(rows)
    n_hr = sum(labels)

    # Within-date rank of HR hitters (1 = highest form that day).
    by_date: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_date[r["date"]].append(r)
    hr_ranks: list[int] = []
    for date_rows in by_date.values():
        ordered = sorted(date_rows, key=lambda r: r["form"][variant], reverse=True)
        for rank, r in enumerate(ordered, start=1):
            if r["hit_hr"] == 1:
                hr_ranks.append(rank)
    avg_rank_hr = sum(hr_ranks) / len(hr_ranks) if hr_ranks else None

    rates = _quintile_rates(values, labels)
    mono = (sum(1 for i in range(len(rates) - 1) if rates[i + 1] > rates[i])
            if rates else None)

    return {
        "n": n,
        "n_hr": n_hr,
        "hr_rate": n_hr / n if n else 0.0,
        "auc": _auc(values, labels),
        "top10_lift": _top_decile_lift(values, labels),
        "quint_mono": mono,
        "quint_rates": rates,
        "avg_rank_hr": avg_rank_hr,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _fmt(x, prec: int = 3) -> str:
    if x is None or (isinstance(x, float) and x != x):
        return "n/a"
    return f"{x:.{prec}f}"


def print_report(results: dict[str, dict], n_dates: int) -> None:
    hdr = (f"  {'variant':<16}{'n':>7}{'n_hr':>7}{'hr_rate':>9}"
           f"{'auc':>8}{'top10_lift':>12}{'quint_mono':>12}{'avg_rank_hr':>13}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for v in VARIANTS:
        m = results[v]
        mono = f"{m['quint_mono']}/4" if m["quint_mono"] is not None else "n/a"
        print(f"  {v:<16}{m['n']:>7d}{m['n_hr']:>7d}{_fmt(m['hr_rate'], 4):>9}"
              f"{_fmt(m['auc'], 3):>8}{_fmt(m['top10_lift'], 2):>12}"
              f"{mono:>12}{_fmt(m['avg_rank_hr'], 1):>13}")
    print()

    print("  Quintile HR rate (low -> high form; want strictly increasing):")
    for v in VARIANTS:
        rates = results[v]["quint_rates"]
        cells = "  ".join(_fmt(r, 4) for r in rates) if rates else "(n < 10)"
        print(f"    {v:<16}{cells}")
    print()

    base_auc = results["current"]["auc"]
    if base_auc is not None:
        print(f"  Verdict vs current (AUC delta - positive = better than baseline):")
        for v in VARIANTS:
            if v == "current":
                continue
            a = results[v]["auc"]
            if a is None:
                print(f"    {v:<16} n/a")
                continue
            d = a - base_auc
            tag = "HELPS" if d > 0.005 else "HURTS" if d < -0.005 else "neutral"
            print(f"    {v:<16}{_fmt(a, 3):>7}  delta {d:+.3f}  -> {tag}")
        ranked = sorted(
            [(v, results[v]["auc"]) for v in VARIANTS
             if results[v]["auc"] is not None],
            key=lambda t: t[1], reverse=True,
        )
        if ranked:
            print()
            print(f"  Best variant by AUC: {ranked[0][0]} ({_fmt(ranked[0][1], 3)})")
    print()

    if n_dates < 10:
        print(f"  [note] only {n_dates} date(s) of data - wiring smoke test, "
              "not a verdict.")
        print("         Re-run after more of the 2025 backfill lands.")
        print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    ap.add_argument("--start", help="start date YYYY-MM-DD (default: earliest)")
    ap.add_argument("--end", help="end date YYYY-MM-DD (default: latest)")
    ap.add_argument("--days", type=int, help="look-back N days from --end / latest")
    ap.add_argument("--db", default=str(DB_PATH),
                    help=f"DB path (default: {DB_PATH})")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    bounds = conn.execute("SELECT MIN(date), MAX(date) FROM pick_inputs").fetchone()
    if not bounds or not bounds[0]:
        print(f"No rows in pick_inputs at {args.db}", file=sys.stderr)
        sys.exit(1)
    lo_db, hi_db = bounds[0], bounds[1]

    end = args.end or hi_db
    if args.days:
        ed = datetime.strptime(end, "%Y-%m-%d").date()
        start = (ed - timedelta(days=args.days - 1)).isoformat()
    else:
        start = args.start or lo_db

    rows = fetch_rows(conn, start, end)
    conn.close()
    if not rows:
        print(f"No pick_inputs/outcomes rows in {start}..{end}", file=sys.stderr)
        sys.exit(1)

    scored = score_variants(rows)
    n_dates = len({s["date"] for s in scored})

    # Common subset: rows where recent_hr_10g is non-NULL (the minimum
    # input every variant uses). This makes the comparison apples-to-
    # apples - every variant scores every row in the subset.
    common = [s for s in scored if s["has_hr10"]]

    print()
    print(f"=== Form anchor + weighting sweep ({start} -> {end}, {n_dates} dates) ===")
    print()
    print(f"  Coverage: {len(scored)} rows total | with recent_hr_10g "
          f"(comparison set): {len(common)}")
    print()
    if not common:
        print("  No rows have recent_hr_10g - nothing to compare.", file=sys.stderr)
        sys.exit(1)

    results = {v: compute_metrics(common, v) for v in VARIANTS}
    print_report(results, n_dates)


if __name__ == "__main__":
    main()
