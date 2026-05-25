#!/usr/bin/env python3
"""
backtest_power_inputs.py - synthetic vs. real Statcast, head to head.

The power factor (score_power) draws on two kinds of quality-contact
input, and B6a was built to find out which one predicts home runs better:

  * SYNTHETIC season inputs - formula-derived from season stats:
        barrel_pct  ~=  hr_per_pa * 200      (HR-rate-encoded)
        hr_fb_pct   ~=  hr_per_pa * 180      (HR-rate-encoded)
        exit_velo    =  82 + slg * 15        (SLG-encoded)
        iso          =  SLG - AVG            (SLG-encoded)
    Two of the four (barrel + hr_fb) are literally past HR rate in
    disguise. So "synthetic" here is not a measurement proxy - it's a
    season-aggregate performance lookup.

  * REAL rolling-14d Statcast - measured pitch-by-pitch from Savant:
        recent_barrel_real_14d
        recent_xwoba_contact_14d
        recent_iso_14d
    Added in B6a, gated behind USE_RECENT_STATCAST_BLEND.

This harness re-scores score_power off the backfilled pick_inputs and
grades each variant against actual HR outcomes. Variants:

  * synthetic-only           - the 4 synthetic season inputs, default anchors
  * real-only                - the 3 real 14d Statcast inputs, default anchors
  * blended                  - all 7 inputs, default anchors (what
                                USE_RECENT_STATCAST_BLEND=True ships)
  * real-tight-anchors       - real-only, anchors WIDENED (14d windows are
                                noisier than season; "elite" sits further out)
  * blended-tight-anchors    - blended with the widened real anchors
  * synthetic-no-hr-encoded  - synthetic minus barrel_pct + hr_fb_pct, so
                                only the SLG-encoded inputs remain (exit_velo
                                + iso). Tests whether the synthetic win is
                                purely past-HR-rate auto-correlation in
                                barrel + hr_fb, or if the SLG-encoded inputs
                                carry independent signal.

Headline numbers run on the COMMON SUBSET (rows that carry both
synthetic and real signal) - apples-to-apples on identical rows.

Metrics on the raw power score vs hit_hr, factor in isolation:

  - auc         - ROC-AUC; P(HR-hitter scored above non-hitter). >0.5 better.
  - top10_lift  - HR rate in top decile of power / overall. >1 better.
  - quint_mono  - monotone-up steps as power rises across 5 quintiles (max 4).
  - avg_rank_hr - mean within-date rank of HR hitters (lower better).

Caveats:

* Synthetic inputs are season-to-date; real inputs are rolling 14d.
  This is "season-synthetic vs. 14d-real," not a pure same-window test.
* The synthetic barrel_pct + hr_fb_pct are literally past HR rate.
  Auto-correlation gives them a strong baseline that contact-quality
  metrics on a noisy 14d window may not exceed. The
  synthetic-no-hr-encoded variant probes this directly.
* Wider real windows (21d, 28d) would need a new Statcast ETL pass to
  populate. Not in scope for this harness; flagged as follow-up if the
  tight-anchor sweep still has real-only trailing.

Usage:
    python diagnostics/backtest_power_inputs.py
    python diagnostics/backtest_power_inputs.py --start 2025-03-27 --end 2025-09-30
    python diagnostics/backtest_power_inputs.py --days 30
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


SYNTHETIC_KEYS = ("barrel_pct", "exit_velo", "hr_fb_pct", "iso")
REAL_KEYS = ("recent_barrel_real_14d", "recent_xwoba_contact_14d", "recent_iso_14d")

# Anchors mirror score_batters.score_power. Variants override individual entries.
DEFAULT_ANCHORS = {
    "barrel_pct":               (5.0, 15.0),
    "exit_velo":                (85.0, 95.0),
    "hr_fb_pct":                (8.0, 20.0),
    "iso":                      (0.130, 0.300),
    "recent_barrel_real_14d":   (8.0, 18.0),
    "recent_xwoba_contact_14d": (0.330, 0.450),
    "recent_iso_14d":           (0.100, 0.300),
}

# Widened anchors for the 14d real metrics. A 14d window has ~30-40
# batted balls per batter - much higher variance than the season
# aggregate. The default score_power anchors (priors set from
# "league_avg / elite" anchored to season distributions) clamp too
# aggressively on this thin window. These widened anchors push the
# "elite" pole further out, matching the upper distribution observed
# on the 2025 backfill.
TIGHT_REAL_ANCHORS = {
    "recent_barrel_real_14d":   (10.0, 22.0),
    "recent_xwoba_contact_14d": (0.320, 0.420),
    "recent_iso_14d":           (0.130, 0.320),
}

# (variant_name, input_keys, anchor_overrides_or_None)
VARIANTS = (
    ("synthetic-only",          SYNTHETIC_KEYS,                       None),
    ("real-only",               REAL_KEYS,                            None),
    ("blended",                 SYNTHETIC_KEYS + REAL_KEYS,           None),
    ("real-tight-anchors",      REAL_KEYS,                            TIGHT_REAL_ANCHORS),
    ("blended-tight-anchors",   SYNTHETIC_KEYS + REAL_KEYS,           TIGHT_REAL_ANCHORS),
    ("synthetic-no-hr-encoded", ("exit_velo", "iso"),                 None),
)
VARIANT_NAMES = tuple(v[0] for v in VARIANTS)


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
            pi.barrel_pct, pi.exit_velo, pi.hr_fb_pct, pi.iso,
            pi.recent_barrel_real_14d, pi.recent_xwoba_contact_14d,
            pi.recent_iso_14d,
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
# Scoring
# ---------------------------------------------------------------------------

def _scale(v: float, lo: float, hi: float) -> float:
    """Clamp + scale into 0-100, mirroring score_batters.min_max_scale."""
    return max(0.0, min(100.0, (v - lo) / (hi - lo) * 100.0))


def _has_signal(row: dict, keys) -> bool:
    """True if >=1 of `keys` is a usable input (not None, > 0)."""
    for k in keys:
        v = row.get(k)
        if v is not None and v > 0:
            return True
    return False


def _compute_power(row: dict, keys, anchor_overrides=None) -> float:
    """Power score from `keys`, with optional anchor overrides. Mirrors
    score_batters.score_power but parametric on anchors and stripped of
    the season-HR floor / xwoba_contact / pull_fb_pct (NULL on backfill).
    """
    anchors = {**DEFAULT_ANCHORS, **(anchor_overrides or {})}
    scores = []
    for k in keys:
        v = row.get(k)
        if v is None or v <= 0:
            continue
        # hr_fb_pct fraction-vs-percent quirk - mirror score_power's behavior.
        if k == "hr_fb_pct" and v < 1:
            v *= 100
        if k not in anchors:
            continue
        lo, hi = anchors[k]
        scores.append(_scale(v, lo, hi))
    return float(np.mean(scores)) if scores else 50.0


def score_variants(rows: list[dict]) -> list[dict]:
    """Score every row under every variant."""
    scored = []
    for r in rows:
        scored.append({
            "date": r["date"],
            "batter_id": r["batter_id"],
            "hit_hr": r["hit_hr"],
            "syn_signal": _has_signal(r, SYNTHETIC_KEYS),
            "real_signal": _has_signal(r, REAL_KEYS),
            "power": {
                name: _compute_power(r, keys, anchors)
                for name, keys, anchors in VARIANTS
            },
        })
    return scored


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
    """Grade one variant's power score on `rows`."""
    values = [r["power"][variant] for r in rows]
    labels = [r["hit_hr"] for r in rows]
    n = len(rows)
    n_hr = sum(labels)

    by_date: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_date[r["date"]].append(r)
    hr_ranks: list[int] = []
    for date_rows in by_date.values():
        ordered = sorted(date_rows, key=lambda r: r["power"][variant], reverse=True)
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
    hdr = (f"  {'variant':<26}{'n':>7}{'n_hr':>7}{'hr_rate':>9}"
           f"{'auc':>8}{'top10_lift':>12}{'quint_mono':>12}{'avg_rank_hr':>13}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for v in VARIANT_NAMES:
        m = results[v]
        mono = f"{m['quint_mono']}/4" if m["quint_mono"] is not None else "n/a"
        print(f"  {v:<26}{m['n']:>7d}{m['n_hr']:>7d}{_fmt(m['hr_rate'], 4):>9}"
              f"{_fmt(m['auc'], 3):>8}{_fmt(m['top10_lift'], 2):>12}"
              f"{mono:>12}{_fmt(m['avg_rank_hr'], 1):>13}")
    print()

    print("  Quintile HR rate (low -> high power; want strictly increasing):")
    for v in VARIANT_NAMES:
        rates = results[v]["quint_rates"]
        cells = "  ".join(_fmt(r, 4) for r in rates) if rates else "(n < 10)"
        print(f"    {v:<26}{cells}")
    print()

    # Verdict block - rank all variants by AUC, then deltas vs the "synthetic-
    # only" baseline (= what production scores on today with the blend off).
    print("  Variants ranked by AUC (delta vs synthetic-only baseline):")
    base = results["synthetic-only"]["auc"]
    ranked = sorted(
        [(v, results[v]["auc"]) for v in VARIANT_NAMES
         if results[v]["auc"] is not None],
        key=lambda t: t[1], reverse=True,
    )
    for name, a in ranked:
        if name == "synthetic-only":
            print(f"    {name:<26}{_fmt(a, 3):>7}  (baseline)")
            continue
        d = a - base
        tag = "HELPS" if d > 0.005 else "HURTS" if d < -0.005 else "neutral"
        print(f"    {name:<26}{_fmt(a, 3):>7}  delta {d:+.3f}  -> {tag}")
    print()

    if n_dates < 10:
        print(f"  [note] only {n_dates} date(s) of data - smoke test, not verdict.")
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

    print()
    print(f"=== Power input-source backtest ({start} -> {end}, {n_dates} dates) ===")
    print()

    n_syn = sum(1 for s in scored if s["syn_signal"])
    n_real = sum(1 for s in scored if s["real_signal"])
    common = [s for s in scored if s["syn_signal"] and s["real_signal"]]
    print(f"  Coverage: {len(scored)} rows | synthetic signal {n_syn} | "
          f"real signal {n_real} | both {len(common)} (comparison set)")
    print()
    if not common:
        print("  No rows carry BOTH signal types - nothing to compare.",
              file=sys.stderr)
        sys.exit(1)

    results = {v: compute_metrics(common, v) for v in VARIANT_NAMES}
    print_report(results, n_dates)


if __name__ == "__main__":
    main()
