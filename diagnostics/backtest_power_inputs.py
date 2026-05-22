#!/usr/bin/env python3
"""
backtest_power_inputs.py - synthetic vs. real Statcast, head to head.

The power factor (score_power) draws on two kinds of quality-contact
input, and B6a was built to find out which one predicts home runs better:

  * SYNTHETIC season inputs - barrel_pct / exit_velo / hr_fb_pct are
    formula-derived, not measured (barrel ~= hr_per_pa * 200, exit_velo
    ~= 82 + slg * 15); iso is a season-to-date aggregate. This is what
    production scores on today, and what the 2025 backfill reconstructs.
  * REAL rolling-14d Statcast - recent_barrel_real_14d /
    recent_xwoba_contact_14d / recent_iso_14d, measured pitch-by-pitch
    from Savant. Added in B6a, gated behind USE_RECENT_STATCAST_BLEND.

This harness re-scores score_power three ways off the backfilled
pick_inputs and grades each against actual HR outcomes:

  * synthetic-only - feed only the synthetic season inputs
  * real-only      - feed only the real 14d Statcast inputs
  * blended        - feed both (what USE_RECENT_STATCAST_BLEND=True does)

score_power needs no code change: its skip-on-missing design means
nulling one input set isolates the other. For the run,
USE_RECENT_STATCAST_BLEND is forced ON (so the real inputs are read) and
USE_SEASON_HR_FLOOR is forced OFF (so the floor cannot compress the
input-source signal). Both are restored on exit.

Metrics are computed on the raw power score vs. hit_hr, so the power
factor is graded in isolation - not diluted through the composite:

  * auc         - ROC-AUC; P(HR-hitter scored above non-hitter). 0.5 is
                  a coin flip; higher is better.
  * top10_lift  - HR rate in the top decile of power / overall HR rate.
                  > 1 means power concentrates HRs in its top rows.
  * quint_mono  - monotone-up steps as power rises across 5 quintiles
                  (4 = perfectly monotone).
  * avg_rank_hr - mean within-date rank of batters who homered
                  (lower is better; rank 1 = highest power that day).

Headline numbers run on the COMMON SUBSET - rows that carry both a
synthetic and a real signal - so it is a true apples-to-apples test.
A coverage line reports how many rows each variant could score at all.

Caveat: synthetic inputs are season-to-date; real inputs are rolling
14d. This is therefore "season-synthetic vs. 14d-real", not a pure
same-window test - interpret accordingly. A pure test would also need
real *season* Statcast, which the backfill does not fetch.

Usage:
    python diagnostics/backtest_power_inputs.py
    python diagnostics/backtest_power_inputs.py --start 2025-03-27 --end 2025-09-30
    python diagnostics/backtest_power_inputs.py --days 30

Requires the DB at <projects>/data/hr_bets.db (resolved via etl.db.DB_PATH).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

# Resolve project root so `import score_batters` works whether this is
# invoked directly or imported as diagnostics.backtest_power_inputs.
_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

import score_batters as sb
from etl.db import DB_PATH


# score_power reads these keys off the batter dict; anything absent or
# None is skipped (skip-on-missing). The two sets are DISJOINT - that is
# the property that lets each variant isolate one input source.
SYNTHETIC_KEYS = ("barrel_pct", "exit_velo", "hr_fb_pct", "iso")
REAL_KEYS = ("recent_barrel_real_14d", "recent_xwoba_contact_14d", "recent_iso_14d")

VARIANTS = ("synthetic-only", "real-only", "blended")
_VARIANT_KEYS = {
    "synthetic-only": SYNTHETIC_KEYS,
    "real-only": REAL_KEYS,
    "blended": SYNTHETIC_KEYS + REAL_KEYS,
}


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
# Variant scoring
# ---------------------------------------------------------------------------

def _has_signal(row: dict, keys) -> bool:
    """True if >=1 of `keys` is a usable input (not None, > 0) - i.e.
    score_power scores it rather than returning the neutral-50 default."""
    for k in keys:
        v = row.get(k)
        if v is not None and v > 0:
            return True
    return False


def _power(row: dict, keys) -> float:
    """score_power fed ONLY the named input keys (all others nulled)."""
    return sb.score_power({k: row.get(k) for k in keys})


def score_variants(rows: list[dict]) -> list[dict]:
    """Re-score every row under all three variants.

    Flags: USE_RECENT_STATCAST_BLEND ON (so the real inputs are read),
    USE_SEASON_HR_FLOOR OFF (so the season-HR floor cannot mask the
    input-source comparison). Both are restored in the finally block so
    importing this module never leaks scoring state into another process.
    """
    prev_blend = sb.USE_RECENT_STATCAST_BLEND
    prev_floor = sb.USE_SEASON_HR_FLOOR
    sb.USE_RECENT_STATCAST_BLEND = True
    sb.USE_SEASON_HR_FLOOR = False
    try:
        scored = []
        for r in rows:
            scored.append({
                "date": r["date"],
                "batter_id": r["batter_id"],
                "hit_hr": r["hit_hr"],
                "syn_signal": _has_signal(r, SYNTHETIC_KEYS),
                "real_signal": _has_signal(r, REAL_KEYS),
                "power": {v: _power(r, keys) for v, keys in _VARIANT_KEYS.items()},
            })
        return scored
    finally:
        sb.USE_RECENT_STATCAST_BLEND = prev_blend
        sb.USE_SEASON_HR_FLOOR = prev_floor


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _auc(values: list[float], labels: list[int]) -> float | None:
    """ROC-AUC via the Mann-Whitney U statistic with tie-averaged ranks.
    O(n log n). Returns None when either class is empty."""
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
        ranks[order[i:j]] = (i + j - 1) / 2.0 + 1.0   # 1-based average rank
        i = j
    rank_pos = ranks[y == 1].sum()
    u = rank_pos - n_pos * (n_pos + 1) / 2.0
    return u / (n_pos * n_neg)


def _top_decile_lift(values: list[float], labels: list[int]) -> float | None:
    """HR rate in the top 10% by value / overall HR rate. None if no HRs."""
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
    """HR rate per quintile, low -> high value. Returns [] when n < 10."""
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
    """Grade one variant's power score on `rows` (the common subset)."""
    values = [r["power"][variant] for r in rows]
    labels = [r["hit_hr"] for r in rows]
    n = len(rows)
    n_hr = sum(labels)

    # Within-date rank of the batters who homered (1 = highest power that day).
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
    if x is None or (isinstance(x, float) and x != x):  # None / NaN
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

    print("  Quintile HR rate (low -> high power; want strictly increasing):")
    for v in VARIANTS:
        rates = results[v]["quint_rates"]
        cells = "  ".join(_fmt(r, 4) for r in rates) if rates else "(n < 10)"
        print(f"    {v:<16}{cells}")
    print()

    syn = results["synthetic-only"]["auc"]
    real = results["real-only"]["auc"]
    if syn is not None and real is not None:
        d = real - syn
        if d > 0.005:
            verdict = "real Statcast beats synthetic"
        elif d < -0.005:
            verdict = "synthetic beats real Statcast"
        else:
            verdict = "real and synthetic are roughly tied"
        print(f"  Verdict (AUC): real-only {_fmt(real, 3)} vs synthetic-only "
              f"{_fmt(syn, 3)}  ->  {verdict} (delta {d:+.3f}).")
        ranked = sorted(
            [(v, results[v]["auc"]) for v in VARIANTS
             if results[v]["auc"] is not None],
            key=lambda t: t[1], reverse=True,
        )
        if ranked:
            print(f"  Best variant by AUC: {ranked[0][0]} ({_fmt(ranked[0][1], 3)}).")
    print()

    if n_dates < 10:
        print(f"  [note] only {n_dates} date(s) of data - this is a wiring "
              "smoke test, not a verdict.")
        print("         Re-run after the full 2025 backfill for a real result.")
        print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    ap.add_argument("--start",
                    help="start date YYYY-MM-DD (default: earliest in pick_inputs)")
    ap.add_argument("--end",
                    help="end date YYYY-MM-DD (default: latest in pick_inputs)")
    ap.add_argument("--days", type=int,
                    help="look-back window of N days ending at --end / latest "
                         "(overrides --start)")
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

    results = {v: compute_metrics(common, v) for v in VARIANTS}
    print_report(results, n_dates)


if __name__ == "__main__":
    main()
