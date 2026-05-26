#!/usr/bin/env python3
"""
backtest_form_archetype.py — sweep Form archetype window + sample-size variants.

Grades the Form-archetype sub-signal (see docs/form_archetype_design.md)
on actual HR outcomes by re-scoring `score_form` under variant configurations:

  default                — current score_form only (no archetype term)
  archetype_7d_5hr       — archetype on, 7d pre-HR window, min 5 career HRs
  archetype_7d_10hr      — archetype on, 7d pre-HR window, min 10 career HRs  <- Phase 1 default
  archetype_7d_20hr      — archetype on, 7d pre-HR window, min 20 career HRs
  archetype_14d_5hr      — archetype on, 14d pre-HR window, min 5 career HRs
  archetype_14d_10hr     — archetype on, 14d pre-HR window, min 10 career HRs
  archetype_14d_20hr     — archetype on, 14d pre-HR window, min 20 career HRs
  archetype_21d_5hr      — archetype on, 21d pre-HR window, min 5 career HRs
  archetype_21d_10hr     — archetype on, 21d pre-HR window, min 10 career HRs
  archetype_21d_20hr     — archetype on, 21d pre-HR window, min 20 career HRs

The 3x3 sweep crosses prior-snapshot window (7/14/21d) with the min-HRs
sample-size policy (5/10/20). Phase 1 default is `archetype_7d_10hr`.

Metrics (same as backtest_form_anchors / backtest_power_inputs):
  - auc         ROC-AUC; P(HR-hitter scored above non-hitter). >0.5 better.
  - top10_lift  HR rate in top decile of form / overall HR rate. >1 better.
  - quint_mono  monotone-up steps as form rises across 5 quintiles (max 4).
  - avg_rank_hr mean within-date rank of HR hitters (lower better).

PHASE 1 STATUS — NOT RUNNABLE YET.

This harness needs:
  (a) the batter_form_archetype table populated for the backtest date range,
  (b) per-row `form_archetype_today_vector` available in pick_inputs.

Phase 2 ships both. Until then the harness exits with a clear message
identifying what's missing, modeled after the pitch-type harness skeleton
(diagnostics/backtest_arsenal_inputs.py).

Usage (post-Phase-2):
    python diagnostics/backtest_form_archetype.py
    python diagnostics/backtest_form_archetype.py --start 2025-04-01 --end 2025-09-30
    python diagnostics/backtest_form_archetype.py --days 90

Caveat: like backtest_form_anchors, this grades the FACTOR in isolation.
A change that improves Form's AUC may or may not move composite rankings —
that integration is the A1 refit's job.
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


# (window_days, min_hrs) sweep — 3 windows x 3 sample-size thresholds.
ARCHETYPE_SWEEP: list[tuple[int, int]] = [
    (7, 5), (7, 10), (7, 20),
    (14, 5), (14, 10), (14, 20),
    (21, 5), (21, 10), (21, 20),
]

VARIANTS = (
    "default",
    *(f"archetype_{w}d_{n}hr" for w, n in ARCHETYPE_SWEEP),
)


# ---------------------------------------------------------------------------
# Phase 1 guard — bail with a clear message if the prerequisites aren't met
# ---------------------------------------------------------------------------

def _phase1_guard(conn: sqlite3.Connection) -> None:
    """Exit early with a clear note if the harness can't run yet.

    Checks:
      1. `batter_form_archetype` exists AND has rows.
      2. `pick_inputs` has a `form_archetype_today_vector` column (Phase 2 adds it).

    If either fails, prints the missing piece and exits 0 (no error — this
    is documented Phase-1 behavior, not a bug).
    """
    try:
        n_arch = conn.execute(
            "SELECT COUNT(*) FROM batter_form_archetype"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        # Table missing — DB was created before this PR's schema migration ran.
        n_arch = 0
    if n_arch == 0:
        print("Phase 1 — run after batter_form_archetype is populated")
        print()
        print("  This harness compares the Form score with vs. without the")
        print("  archetype sub-signal across a 3x3 (window x min_hrs) sweep.")
        print("  It needs (a) batter_form_archetype rows for the backtest")
        print("  date range and (b) form_archetype_today_vector available in")
        print("  pick_inputs (or computable from cached per-date Statcast).")
        print()
        print("  Phase 2 (etl/backfill_form_archetype.py + nightly hook)")
        print("  populates both. Re-run this then.")
        sys.exit(0)

    # Phase 2 will add this column; for now, the harness can't compute
    # today's state-vector without it, so bail.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pick_inputs)").fetchall()}
    if "form_archetype_today_vector" not in cols:
        print("Phase 1 — run after batter_form_archetype is populated")
        print()
        print("  batter_form_archetype has rows, but pick_inputs is missing")
        print("  the form_archetype_today_vector column. Phase 2 adds it.")
        sys.exit(0)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def fetch_rows(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    """pick_inputs JOIN outcomes JOIN batter_form_archetype.

    Phase 2 will fill in this query body. For Phase 1 the harness exits
    in _phase1_guard before this is reached; the function is kept as a
    documented placeholder so Phase 2's wiring is one localized change.
    """
    # TODO Phase 2: complete the SQL once form_archetype_today_vector is
    # populated. The shape will be:
    #   SELECT pi.date, pi.batter_id, pi.recent_hr_10g, pi.recent_iso_30g,
    #          pi.ev_trend, pi.form_archetype_today_vector,
    #          bfa.feature_centroid_json AS centroid, bfa.window_days,
    #          bfa.n_hrs_used,
    #          CASE WHEN COALESCE(o.hr_count, 0) > 0 THEN 1 ELSE 0 END AS hit_hr
    #     FROM pick_inputs pi
    #     INNER JOIN outcomes o ON o.date = pi.date AND o.batter_id = pi.batter_id
    #     LEFT JOIN batter_form_archetype bfa
    #            ON bfa.player_id = pi.batter_id
    #           AND bfa.date_through = date(pi.date, '-1 day')
    #    WHERE pi.date >= ? AND pi.date <= ?
    return []


# ---------------------------------------------------------------------------
# Variant scoring — mirrors backtest_form_anchors' structure
# ---------------------------------------------------------------------------

def _scale(x: float, lo: float, hi: float) -> float:
    """Clamp + scale into 0-100, mirroring score_batters.min_max_scale."""
    return max(0.0, min(100.0, (x - lo) / (hi - lo) * 100.0))


def _form_score(row: dict, variant: str) -> float:
    """Compute the Form score under one variant.

    No dampener applied — this isolates the input-formula change from the
    layoff layer, matching backtest_form_anchors.
    """
    hr10 = row.get("recent_hr_10g")
    iso30 = row.get("recent_iso_30g")
    ev_trend = row.get("ev_trend")

    pieces: list[tuple[float, float]] = []

    if hr10 is not None:
        pieces.append((1.0, _scale(hr10, 0.0, 5.0)))
    if iso30 is not None and iso30 > 0:
        pieces.append((1.0, _scale(iso30, 0.100, 0.300)))
    if ev_trend is not None:
        pieces.append((1.0, _scale(ev_trend, -3.0, 3.0)))

    # Archetype sub-signal — variant-dependent. Default has no sub-signal.
    if variant != "default":
        # Variant name shape: "archetype_{window}d_{min_hrs}hr"
        # Phase 2 fills in: per-variant today_vector + centroid lookup,
        # plus the L2-similarity computation. The math will mirror
        # score_batters._compute_form_archetype_match.
        archetype_score = row.get(f"archetype_score_{variant}")
        if archetype_score is not None:
            pieces.append((1.0, archetype_score))

    if not pieces:
        return 50.0
    total_w = sum(w for w, _ in pieces)
    return sum(w * v for w, v in pieces) / total_w


def score_variants(rows: list[dict]) -> list[dict]:
    """Score every row under every variant."""
    return [
        {
            "date": r["date"],
            "batter_id": r["batter_id"],
            "hit_hr": r["hit_hr"],
            "has_hr10": r.get("recent_hr_10g") is not None,
            "form": {v: _form_score(r, v) for v in VARIANTS},
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Metrics — copied from backtest_form_anchors (same metric definitions)
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
    hdr = (f"  {'variant':<22}{'n':>7}{'n_hr':>7}{'hr_rate':>9}"
           f"{'auc':>8}{'top10_lift':>12}{'quint_mono':>12}{'avg_rank_hr':>13}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for v in VARIANTS:
        m = results[v]
        mono = f"{m['quint_mono']}/4" if m["quint_mono"] is not None else "n/a"
        print(f"  {v:<22}{m['n']:>7d}{m['n_hr']:>7d}{_fmt(m['hr_rate'], 4):>9}"
              f"{_fmt(m['auc'], 3):>8}{_fmt(m['top10_lift'], 2):>12}"
              f"{mono:>12}{_fmt(m['avg_rank_hr'], 1):>13}")
    print()

    base_auc = results["default"]["auc"]
    if base_auc is not None:
        print(f"  Verdict vs default (AUC delta — positive = better than baseline):")
        for v in VARIANTS:
            if v == "default":
                continue
            a = results[v]["auc"]
            if a is None:
                print(f"    {v:<22} n/a")
                continue
            d = a - base_auc
            tag = "HELPS" if d > 0.005 else "HURTS" if d < -0.005 else "neutral"
            print(f"    {v:<22}{_fmt(a, 3):>7}  delta {d:+.3f}  -> {tag}")
    print()

    if n_dates < 10:
        print(f"  [note] only {n_dates} date(s) of data — wiring smoke test, "
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

    _phase1_guard(conn)

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

    common = [s for s in scored if s["has_hr10"]]

    print()
    print(f"=== Form archetype sweep ({start} -> {end}, {n_dates} dates) ===")
    print()
    print(f"  Coverage: {len(scored)} rows total | with recent_hr_10g "
          f"(comparison set): {len(common)}")
    print()
    if not common:
        print("  No rows have recent_hr_10g — nothing to compare.", file=sys.stderr)
        sys.exit(1)

    results = {v: compute_metrics(common, v) for v in VARIANTS}
    print_report(results, n_dates)


if __name__ == "__main__":
    main()
