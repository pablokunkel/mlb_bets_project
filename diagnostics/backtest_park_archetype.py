#!/usr/bin/env python3
"""
backtest_park_archetype.py - Phase 1 skeleton for the park-archetype
sub-signal harness for score_park.

Background. See docs/park_archetype_design.md. The new signal is a
per-batter centroid of "park features at venues where this batter has
historically hit HRs", scored against today's park by L2 distance:

    park_archetype_match =
        1 - normalized_L2_distance(batter.centroid, today_park.features)

Mapped to 0-100 via inverse-distance anchors. This harness re-scores
`score_park` off the backfilled pick_inputs and grades six variants
against actual HR outcomes (mirrors backtest_form_anchors.py and
backtest_arsenal_inputs.py):

  * default                - score_park as it ships today (no archetype
                             term). Production baseline at the moment
                             Phase 3 starts.
  * archetype_5hr          - sub-signal weight 0.5, threshold 5 HRs.
  * archetype_10hr         - sub-signal weight 0.5, threshold 10 HRs.
                             Current default threshold.
  * archetype_20hr         - sub-signal weight 0.5, threshold 20 HRs.
                             High-confidence threshold.
  * archetype_weighted_low - threshold 10, sub-signal weight 0.25.
  * archetype_weighted_high- threshold 10, sub-signal weight 0.75.

Headline numbers run on the COMMON SUBSET - rows where the base park-
handedness signal AND the archetype centroid are both computable. Apples-
to-apples on identical rows.

Metrics on the raw park score vs hit_hr, factor in isolation:

  - auc         - ROC-AUC; P(HR-hitter scored above non-hitter). >0.5 better.
  - top10_lift  - HR rate in top decile of park / overall. >1 better.
  - quint_mono  - monotone-up steps as park rises across 5 quintiles (max 4).
  - avg_rank_hr - mean within-date rank of HR hitters (lower better).

Caveats:

* This grades the FACTOR in isolation. A change that improves park's AUC
  may or may not move composite rankings - that integration is the next
  A1 refit's job. Note: park's current weight is 0.000 in
  WEIGHT_CONFIGS["default"], so even a lift here is contingent on the
  A1 refit promoting park's weight off the floor (the Phase 4 path).
* The archetype centroid is built career-to-date through (date - 1). It
  benefits from the entire 2024+ history, so early-April reconstructions
  have the same coverage as September ones - unlike season-aggregate
  factors which lopside late.
* L2-distance anchors (0.0-7.0 -> 100-0) are picked from the design doc.
  Re-tunable in a follow-up sweep variant once the baseline variants land.

Status: **Phase 1 - not yet runnable.** The SQL fetch references
`pi.park_archetype_centroid_json`, which won't exist in `pick_inputs`
until Phase 2 (etl/backfill_park_archetype.py + the matching ALTER
in etl/db.py). `main()` bails with a clear message until that lands.

Run after batter_park_archetype is populated for the 2025 backfill.

Usage (Phase 3, post-Phase-2):
    python diagnostics/backtest_park_archetype.py
    python diagnostics/backtest_park_archetype.py --start 2025-03-27 --end 2025-09-30
    python diagnostics/backtest_park_archetype.py --days 30
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

from etl.db import DB_PATH


# Variant -> (threshold_min_hrs, subsignal_weight). The harness sweeps
# both axes for the Phase 3 decision. `default` reproduces today's
# score_park exactly (no archetype term).
VARIANTS: dict[str, dict] = {
    "default":                {"threshold": None, "weight": 0.0},
    "archetype_5hr":          {"threshold": 5,    "weight": 0.5},
    "archetype_10hr":         {"threshold": 10,   "weight": 0.5},
    "archetype_20hr":         {"threshold": 20,   "weight": 0.5},
    "archetype_weighted_low": {"threshold": 10,   "weight": 0.25},
    "archetype_weighted_high":{"threshold": 10,   "weight": 0.75},
}


# Imported at runtime from features_v2 so the harness stays in sync with
# the production constants.
def _park_feature_keys() -> tuple[str, ...]:
    from features_v2 import PARK_FEATURE_KEYS
    return PARK_FEATURE_KEYS


def _park_archetype_anchors() -> tuple[float, float]:
    from score_batters import PARK_ARCHETYPE_DIST_NEAR, PARK_ARCHETYPE_DIST_FAR
    return PARK_ARCHETYPE_DIST_NEAR, PARK_ARCHETYPE_DIST_FAR


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def fetch_rows(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    """pick_inputs JOINed with outcomes (the HR label) over [start, end].

    Phase 2 adds park_archetype_centroid_json + park_archetype_n_hrs to
    pick_inputs (so we can sweep thresholds at scoring time without
    re-running the centroid builder). Until then this query fails on the
    unknown column; main() handles the bail message.
    """
    rows = conn.execute(
        """
        SELECT
            pi.date,
            pi.batter_id,
            pi.bats,
            pi.venue,
            pi.park_archetype_centroid_json,
            pi.park_archetype_n_hrs,
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
    if hi == lo:
        return 50.0
    return max(0.0, min(100.0, (v - lo) / (hi - lo) * 100.0))


def _base_park_score(row: dict, park_factors_lookup: dict[str, dict]) -> float:
    """Reproduce score_park's base (no-archetype) score from a row.

    Mirrors the slate-relative path: hr_pf_overall percentile across the
    day's slate + a small L/R handedness adjustment. Phase 2's
    pick_inputs schema will include `slate_park_pct` directly to avoid
    re-percentiling here; until then this is a simple fixed-anchor
    fallback (the same path score_park hits without slate_ctx).
    """
    venue = row.get("venue") or ""
    pf = park_factors_lookup.get(venue)
    if pf is None:
        return 50.0
    bats = (row.get("bats") or "R").upper()
    lhb = float(pf["hr_pf_lhb"])
    rhb = float(pf["hr_pf_rhb"])
    if bats == "L":
        v = lhb
    elif bats == "R":
        v = rhb
    elif bats == "S":
        v = (lhb + rhb) / 2.0
    else:
        v = float(pf["hr_pf_overall"])
    return _scale(v, 70, 130)


def _archetype_score(row: dict, today_vec: list[float] | None) -> float | None:
    """Compute the archetype match score for one row.

    Returns None when the batter's centroid is NULL (below threshold) or
    today's park features can't be built.
    """
    cj = row.get("park_archetype_centroid_json")
    if not cj or today_vec is None:
        return None
    try:
        centroid = json.loads(cj)
    except (TypeError, ValueError):
        return None
    if not isinstance(centroid, list) or not centroid:
        return None
    if len(centroid) != len(today_vec):
        return None

    near, far = _park_archetype_anchors()
    sq = sum((float(a) - float(b)) ** 2 for a, b in zip(today_vec, centroid))
    dist = sq ** 0.5
    return _scale(far - dist, far - far, far - near)


def _park_score(
    row: dict,
    variant: str,
    park_factors_lookup: dict[str, dict],
    today_vec_cache: dict[str, list[float] | None],
) -> float:
    """Compute the park score under one variant.

    `default` reproduces base score_park exactly. Other variants
    additively blend the archetype term when:
      (a) the centroid is non-NULL and well-formed,
      (b) the batter's n_hrs meets the variant's threshold,
      (c) today's park features are computable.
    """
    base = _base_park_score(row, park_factors_lookup)
    cfg = VARIANTS[variant]
    if cfg["threshold"] is None or cfg["weight"] == 0:
        return base

    n_hrs = row.get("park_archetype_n_hrs") or 0
    if n_hrs < cfg["threshold"]:
        return base

    venue = row.get("venue") or ""
    today_vec = today_vec_cache.get(venue)
    if today_vec is None and venue:
        try:
            from features_v2 import build_park_feature_vector
            today_vec = build_park_feature_vector(venue, park_factors_lookup)
        except Exception:
            today_vec = None
        today_vec_cache[venue] = today_vec

    arch = _archetype_score(row, today_vec)
    if arch is None:
        return base

    w = float(cfg["weight"])
    return max(0.0, min(100.0, (1.0 - w) * base + w * arch))


def _has_archetype_signal(row: dict, threshold: int) -> bool:
    """Row carries the archetype centroid AND meets the variant's
    n_hrs threshold. Defines the common-subset filter for the comparison.
    """
    cj = row.get("park_archetype_centroid_json")
    n_hrs = row.get("park_archetype_n_hrs") or 0
    return bool(cj) and n_hrs >= threshold


def score_variants(rows: list[dict]) -> list[dict]:
    """Score every row under every variant. Caches today-park feature
    vectors so each unique venue is built once."""
    try:
        from features_v2 import _build_park_factors_lookup
        park_lookup = _build_park_factors_lookup()
    except Exception:
        park_lookup = {}

    today_vec_cache: dict[str, list[float] | None] = {}
    scored = []
    for r in rows:
        # Min-threshold for any archetype variant. If nobody meets even
        # the loosest threshold (5 HRs), the row falls out of the common
        # subset across all archetype variants.
        has_any = _has_archetype_signal(r, 5)
        scored.append({
            "date": r["date"],
            "batter_id": r["batter_id"],
            "hit_hr": r["hit_hr"],
            "has_archetype": has_any,
            "park": {
                v: _park_score(r, v, park_lookup, today_vec_cache)
                for v in VARIANTS
            },
        })
    return scored


# ---------------------------------------------------------------------------
# Metrics (identical to backtest_form_anchors.py — copy-pasted so the
# harness is self-contained and doesn't drift if the other harness
# evolves its metric set)
# ---------------------------------------------------------------------------

def _auc(values: list[float], labels: list[int]) -> float | None:
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
    values = [r["park"][variant] for r in rows]
    labels = [r["hit_hr"] for r in rows]
    n = len(rows)
    n_hr = sum(labels)

    by_date: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_date[r["date"]].append(r)
    hr_ranks: list[int] = []
    for date_rows in by_date.values():
        ordered = sorted(date_rows, key=lambda r: r["park"][variant], reverse=True)
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
    variant_names = list(VARIANTS.keys())
    hdr = (f"  {'variant':<26}{'n':>7}{'n_hr':>7}{'hr_rate':>9}"
           f"{'auc':>8}{'top10_lift':>12}{'quint_mono':>12}{'avg_rank_hr':>13}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for v in variant_names:
        m = results[v]
        mono = f"{m['quint_mono']}/4" if m["quint_mono"] is not None else "n/a"
        print(f"  {v:<26}{m['n']:>7d}{m['n_hr']:>7d}{_fmt(m['hr_rate'], 4):>9}"
              f"{_fmt(m['auc'], 3):>8}{_fmt(m['top10_lift'], 2):>12}"
              f"{mono:>12}{_fmt(m['avg_rank_hr'], 1):>13}")
    print()

    print("  Quintile HR rate (low -> high park; want strictly increasing):")
    for v in variant_names:
        rates = results[v]["quint_rates"]
        cells = "  ".join(_fmt(r, 4) for r in rates) if rates else "(n < 10)"
        print(f"    {v:<26}{cells}")
    print()

    base_auc = results["default"]["auc"]
    if base_auc is not None:
        print("  Verdict vs default (AUC delta - positive = better than baseline):")
        for v in variant_names:
            if v == "default":
                continue
            a = results[v]["auc"]
            if a is None:
                print(f"    {v:<26} n/a")
                continue
            d = a - base_auc
            tag = "HELPS" if d > 0.005 else "HURTS" if d < -0.005 else "neutral"
            print(f"    {v:<26}{_fmt(a, 3):>7}  delta {d:+.3f}  -> {tag}")
        ranked = sorted(
            [(v, results[v]["auc"]) for v in variant_names
             if results[v]["auc"] is not None],
            key=lambda t: t[1], reverse=True,
        )
        if ranked:
            print()
            print(f"  Best variant by AUC: {ranked[0][0]} ({_fmt(ranked[0][1], 3)})")
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

    try:
        rows = fetch_rows(conn, start, end)
    except sqlite3.OperationalError as e:
        conn.close()
        print(f"\n  [phase-1-skeleton] {e}", file=sys.stderr)
        print(
            "\n  Phase 1 - run after batter_park_archetype is populated.\n"
            "\n  This harness is the Phase 1 skeleton for the park-archetype\n"
            "  sub-signal of score_park. It requires the Phase 2 columns\n"
            "  park_archetype_centroid_json + park_archetype_n_hrs on\n"
            "  pick_inputs, populated by etl/backfill_park_archetype.py.\n"
            "  See docs/park_archetype_design.md.\n",
            file=sys.stderr,
        )
        sys.exit(2)
    conn.close()

    if not rows:
        print(f"No pick_inputs/outcomes rows in {start}..{end}", file=sys.stderr)
        sys.exit(1)

    scored = score_variants(rows)
    n_dates = len({s["date"] for s in scored})

    print()
    print(f"=== Park-archetype sub-signal backtest ({start} -> {end}, "
          f"{n_dates} dates) ===")
    print()

    n_arch = sum(1 for s in scored if s["has_archetype"])
    common = [s for s in scored if s["has_archetype"]]
    print(f"  Coverage: {len(scored)} rows | archetype signal {n_arch} "
          f"(comparison set, threshold=5)")
    print()
    if not common:
        print("  No rows carry an archetype centroid - nothing to compare.",
              file=sys.stderr)
        sys.exit(1)

    results = {v: compute_metrics(common, v) for v in VARIANTS}
    print_report(results, n_dates)


if __name__ == "__main__":
    main()
