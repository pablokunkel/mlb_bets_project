#!/usr/bin/env python3
"""
backtest_arsenal_inputs.py - Phase 2 (2026-05-25): runnable harness for
the pitch-type archetype matchup sub-signal.

Background. See docs/pitch_type_archetype_design.md. The new signal is
the blend:

    xSLG_vs_arsenal =
          pitcher.fb_usage_pct * batter.fb_slg
        + pitcher.br_usage_pct * batter.br_slg
        + pitcher.os_usage_pct * batter.os_slg

Mapped to 0-100 with min_max_scale(xslg, 0.350, 0.500). This harness
re-scores `score_matchup` off the backfilled pick_inputs and grades five
variants against actual HR outcomes (mirrors backtest_power_inputs.py
and backtest_form_anchors.py).

Variants:
  * current             - score_matchup unchanged (no arsenal term). The
                          production baseline at the moment Phase 3 starts.
  * arsenal_blend       - score_matchup + xslg_vs_arsenal_score averaged in
                          as a fifth sub-signal alongside vuln / sim /
                          total / woba. Equal-mean weighting; matches the
                          production helper exactly when USE_ARSENAL_SUBSIGNAL
                          is True (no separate weight knob).
  * arsenal_weight_0.25 - composite blend: 0.75 * current + 0.25 * arsenal_only.
  * arsenal_weight_0.5  - 0.5 * current + 0.5 * arsenal_only.
  * arsenal_weight_0.75 - 0.25 * current + 0.75 * arsenal_only.
  * arsenal_weight_1.0  - 1.0 * arsenal_only (sanity check; should be
                          worse than blends because vuln/sim/woba do real
                          work).

Headline numbers run on the COMMON SUBSET — rows where the 4 baseline
matchup sub-signals AND the arsenal blend (pitcher arsenal usage + all
three batter splits above PITCH_TYPE_SPLIT_MIN_BB) are computable.
Apples-to-apples on identical rows.

Metrics on the raw matchup score vs hit_hr, factor in isolation:

  - auc         - ROC-AUC; P(HR-hitter scored above non-hitter). >0.5 better.
  - top10_lift  - HR rate in top decile of matchup / overall. >1 better.
  - quint_mono  - monotone-up steps as matchup rises across 5 quintiles (max 4).
  - avg_rank_hr - mean within-date rank of HR hitters (lower better).

Caveats:

* This grades the FACTOR in isolation. A change that improves matchup's
  AUC may or may not move composite rankings - that integration is the
  next A1 refit's job.
* The arsenal signal is season-to-date through (date - 1), so it has
  the same end-of-season vs. early-April lopsidedness any season-aggregate
  factor has. Spot-check April separately if the season-wide blend wins.
* xSLG anchors (0.350-0.500) are league-distribution defaults from the
  design doc. Re-tunable in a follow-up sweep variant once the baseline
  arsenal_blend lands.
* None+skip policy applies: if any of fb_pa / br_pa / os_pa is below
  PITCH_TYPE_SPLIT_MIN_BB (30), the arsenal term is dropped from that
  row's blend — matching score_batters._compute_xslg_vs_arsenal exactly.

Usage:
    python diagnostics/backtest_arsenal_inputs.py
    python diagnostics/backtest_arsenal_inputs.py --start 2025-03-27 --end 2025-09-30
    python diagnostics/backtest_arsenal_inputs.py --days 30
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


# Five variants — baseline + production blend + 4 weight-sweep points.
# Order matters for the printed report (left-to-right == low-to-high weight).
VARIANTS = (
    "current",
    "arsenal_blend",
    "arsenal_weight_0.25",
    "arsenal_weight_0.5",
    "arsenal_weight_0.75",
    "arsenal_weight_1.0",
)


def _min_bb() -> int:
    from features_v2 import PITCH_TYPE_SPLIT_MIN_BB
    return PITCH_TYPE_SPLIT_MIN_BB


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def fetch_rows(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    """pick_inputs JOINed with outcomes (the HR label) over [start, end].

    Phase 2: adds the JOIN to daily_picks + pitcher_arsenals so each row
    carries the opposing pitcher's fb/breaking/offspeed usage at scoring
    time. pitcher_arsenals is keyed (pitcher_id, season) — we use the
    season matching the pick row's year. NULL on the join means the
    pitcher's arsenal wasn't measured yet (rookies, deep AAA call-ups);
    those rows drop out of the common subset.
    """
    rows = conn.execute(
        """
        SELECT
            pi.date,
            pi.batter_id,
            pi.pitcher_hr_per_9, pi.pitcher_era, pi.pitcher_hh_pct,
            pi.pitcher_k_per_9, pi.pitcher_fb_pct_allowed,
            pi.pitcher_recent_hr9_21d, pi.pitcher_recent_starts_21d,
            pi.pitcher_recent_era_21d, pi.pitcher_recent_k9_21d,
            pi.woba_vs_hand, pi.archetype_similarity, pi.vegas_team_total_pct,
            pi.fb_slg, pi.fb_pa, pi.br_slg, pi.br_pa, pi.os_slg, pi.os_pa,
            pa.fb_usage_pct      AS pitcher_fb_usage_pct,
            pa.breaking_pct      AS pitcher_br_usage_pct,
            pa.offspeed_pct      AS pitcher_os_usage_pct,
            CASE WHEN COALESCE(o.hr_count, 0) > 0 THEN 1 ELSE 0 END AS hit_hr
        FROM pick_inputs pi
        INNER JOIN outcomes o
                ON o.date = pi.date AND o.batter_id = pi.batter_id
        LEFT JOIN daily_picks dp
                ON dp.date = pi.date AND dp.batter_id = pi.batter_id
        LEFT JOIN pitcher_arsenals pa
                ON pa.pitcher_id = dp.opp_pitcher_id
               AND pa.season = CAST(SUBSTR(pi.date, 1, 4) AS INTEGER)
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


def _xslg_vs_arsenal(row: dict) -> float | None:
    """Mirror score_batters._compute_xslg_vs_arsenal off a pick_inputs row.

    Returns None when ANY of:
      - all three pitcher usages are missing (no measured arsenal), OR
      - any of the three batter groups is missing or below
        PITCH_TYPE_SPLIT_MIN_BB.
    Matches the production helper exactly so harness variants are
    apples-to-apples with what the flag-on production code would compute.
    """
    fb_use = row.get("pitcher_fb_usage_pct")
    br_use = row.get("pitcher_br_usage_pct")
    os_use = row.get("pitcher_os_usage_pct")
    if fb_use is None and br_use is None and os_use is None:
        return None

    min_bb = _min_bb()

    def _slg(group: str) -> float | None:
        slg = row.get(f"{group}_slg")
        pa = row.get(f"{group}_pa") or 0
        if slg is None or pa < min_bb:
            return None
        return float(slg)

    fb_slg = _slg("fb")
    br_slg = _slg("br")
    os_slg = _slg("os")
    if fb_slg is None or br_slg is None or os_slg is None:
        return None

    fb_use = fb_use or 0.0
    br_use = br_use or 0.0
    os_use = os_use or 0.0
    return fb_use * fb_slg + br_use * br_slg + os_use * os_slg


def _baseline_matchup(row: dict) -> float:
    """Equal-mean of the 4 baseline matchup sub-signals (no arsenal term).

    Mirrors score_batters.score_matchup v1 fallback: vuln (HR/9 + HH%),
    woba_vs_hand, archetype_similarity, vegas_team_total_pct. Same
    skip-on-missing semantics. Returns 50.0 on completely empty input
    (rare; only if every signal is null).
    """
    scores: list[float] = []
    hr9 = row.get("pitcher_hr_per_9")
    if hr9 is not None and hr9 > 0:
        scores.append(_scale(hr9, 0.0, 4.5))
    hh = row.get("pitcher_hh_pct")
    if hh is not None and hh > 0:
        scores.append(_scale(hh, 25.0, 50.0))
    woba = row.get("woba_vs_hand")
    if woba is not None and woba > 0:
        scores.append(_scale(woba, 0.290, 0.395))
    sim = row.get("archetype_similarity")
    if sim is not None:
        scores.append(float(sim))
    vtt = row.get("vegas_team_total_pct")
    if vtt is not None:
        scores.append(float(vtt))
    return float(np.mean(scores)) if scores else 50.0


def _arsenal_score(row: dict) -> float | None:
    """xSLG-vs-arsenal mapped to 0-100. None when the underlying signal
    is None (any group below threshold or pitcher arsenal unmeasured)."""
    xslg = _xslg_vs_arsenal(row)
    if xslg is None:
        return None
    return _scale(xslg, 0.350, 0.500)


def _matchup_score(row: dict, variant: str) -> float:
    """Compute matchup score under one variant.

    The 'current' variant is the baseline 4-signal mean. 'arsenal_blend'
    reproduces the flag-on production helper (5-signal equal-mean) by
    appending the arsenal term to the baseline scores BEFORE the mean.
    The weighted variants compose: w * arsenal_only + (1-w) * baseline,
    so they parameterize how much the arsenal moves the final score
    independent of the baseline's signal count.

    When the arsenal score isn't computable (None+skip), all variants
    fall back to the baseline — same row drops out of any harness
    variant by virtue of the common-subset filter.
    """
    baseline = _baseline_matchup(row)
    arsenal = _arsenal_score(row)

    if variant == "current":
        return baseline
    if arsenal is None:
        # No arsenal signal -> every variant reduces to baseline. The
        # common-subset filter in _has_arsenal_signal usually drops these
        # rows; this branch covers any that sneak through.
        return baseline

    if variant == "arsenal_blend":
        # Production-equivalent: arsenal is one of 5 equal-mean signals.
        # Recompute as a true equal-mean over the same scores the baseline
        # mean would produce, but with arsenal appended.
        scores: list[float] = []
        hr9 = row.get("pitcher_hr_per_9")
        if hr9 is not None and hr9 > 0:
            scores.append(_scale(hr9, 0.0, 4.5))
        hh = row.get("pitcher_hh_pct")
        if hh is not None and hh > 0:
            scores.append(_scale(hh, 25.0, 50.0))
        woba = row.get("woba_vs_hand")
        if woba is not None and woba > 0:
            scores.append(_scale(woba, 0.290, 0.395))
        sim = row.get("archetype_similarity")
        if sim is not None:
            scores.append(float(sim))
        vtt = row.get("vegas_team_total_pct")
        if vtt is not None:
            scores.append(float(vtt))
        scores.append(arsenal)
        return float(np.mean(scores))

    # Parametric weight sweep: w * arsenal_only + (1-w) * baseline.
    weight_map = {
        "arsenal_weight_0.25": 0.25,
        "arsenal_weight_0.5":  0.50,
        "arsenal_weight_0.75": 0.75,
        "arsenal_weight_1.0":  1.00,
    }
    w = weight_map.get(variant)
    if w is None:
        return baseline
    return w * arsenal + (1.0 - w) * baseline


def _has_arsenal_signal(row: dict) -> bool:
    """Common-subset filter: True iff the row has all three pitcher
    arsenal usages AND all three batter splits at-or-above the PA
    threshold. Same gate as _xslg_vs_arsenal returning non-None.
    """
    return _xslg_vs_arsenal(row) is not None


def score_variants(rows: list[dict]) -> list[dict]:
    """Score every row under every variant."""
    scored = []
    for r in rows:
        scored.append({
            "date": r["date"],
            "batter_id": r["batter_id"],
            "hit_hr": r["hit_hr"],
            "has_arsenal": _has_arsenal_signal(r),
            "matchup": {v: _matchup_score(r, v) for v in VARIANTS},
        })
    return scored


# ---------------------------------------------------------------------------
# Metrics (identical to backtest_power_inputs.py — copy-pasted on purpose
# so the harness is self-contained and doesn't drift if the other harness
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
    values = [r["matchup"][variant] for r in rows]
    labels = [r["hit_hr"] for r in rows]
    n = len(rows)
    n_hr = sum(labels)

    by_date: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_date[r["date"]].append(r)
    hr_ranks: list[int] = []
    for date_rows in by_date.values():
        ordered = sorted(date_rows, key=lambda r: r["matchup"][variant], reverse=True)
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

    print("  Quintile HR rate (low -> high matchup; want strictly increasing):")
    for v in VARIANTS:
        rates = results[v]["quint_rates"]
        cells = "  ".join(_fmt(r, 4) for r in rates) if rates else "(n < 10)"
        print(f"    {v:<22}{cells}")
    print()

    # Verdict: pick the variant with the best AUC.
    aucs = {v: results[v]["auc"] for v in VARIANTS if results[v]["auc"] is not None}
    if aucs:
        best = max(aucs, key=aucs.get)
        base = aucs.get("current")
        if base is not None and best != "current":
            d = aucs[best] - base
            tag = "HELPS" if d > 0.005 else "HURTS" if d < -0.005 else "neutral"
            print(f"  Best AUC: {best} ({aucs[best]:.3f}); delta {d:+.3f} vs current -> {tag}")
        else:
            print(f"  Best AUC: {best} ({aucs[best]:.3f})")
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
        print(f"\n  [schema error] {e}", file=sys.stderr)
        print(
            "\n  Phase 2 columns fb_slg / br_slg / os_slg / *_pa not on\n"
            "  pick_inputs yet. Run etl/db.create_tables (idempotent ALTER)\n"
            "  then populate via etl/backfill_pitch_type_splits.py.\n",
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
    print(f"=== Arsenal sub-signal backtest ({start} -> {end}, {n_dates} dates) ===")
    print()

    n_arsenal = sum(1 for s in scored if s["has_arsenal"])
    common = [s for s in scored if s["has_arsenal"]]
    print(f"  Coverage: {len(scored)} rows | arsenal signal {n_arsenal} "
          f"(comparison set)")
    print()
    if not common:
        print("  No rows carry the arsenal signal yet — nothing to compare.",
              file=sys.stderr)
        sys.exit(1)

    results = {v: compute_metrics(common, v) for v in VARIANTS}
    print_report(results, n_dates)


if __name__ == "__main__":
    main()
