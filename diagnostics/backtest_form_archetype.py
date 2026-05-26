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

Phase 2 (2026-05-26): wired against persisted data. Reads centroids from
the `batter_form_archetype` table (populated by
`etl/backfill_form_archetype.py`) and today's state vector from the same
table on the previous date as a proxy (until a dedicated today-state
column is added). Variant scoring uses `_compute_form_archetype_match`
from `score_batters` so the math matches production.

Metrics (same as backtest_form_anchors / backtest_power_inputs):
  - auc         ROC-AUC; P(HR-hitter scored above non-hitter). >0.5 better.
  - top10_lift  HR rate in top decile of form / overall HR rate. >1 better.
  - quint_mono  monotone-up steps as form rises across 5 quintiles (max 4).
  - avg_rank_hr mean within-date rank of HR hitters (lower better).

Sub-signal weight sweep (--weight-sweep): for the best (window, min_hrs)
variant, also reports the same 4 metrics at FORM_ARCHETYPE_SUBSIGNAL_WEIGHT
in {0.25, 0.5, 0.75, 1.0}. Phase 1 ships at 1.0 (equal-mean with the 3
base form terms); Phase 3 picks the empirically best weight.

Usage:
    python diagnostics/backtest_form_archetype.py
    python diagnostics/backtest_form_archetype.py --start 2025-04-01 --end 2025-09-30
    python diagnostics/backtest_form_archetype.py --days 90
    python diagnostics/backtest_form_archetype.py --weight-sweep

Caveat: like backtest_form_anchors, this grades the FACTOR in isolation.
A change that improves Form's AUC may or may not move composite rankings —
that integration is the A1 refit's job.
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
from score_batters import (
    _compute_form_archetype_match,
    FORM_ARCHETYPE_SUBSIGNAL_WEIGHT as _DEFAULT_SUBSIGNAL_WEIGHT,
)


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

# Sub-signal weight sweep — for the best (window, min_hrs) combo only.
WEIGHT_SWEEP: tuple[float, ...] = (0.25, 0.5, 0.75, 1.0)


# ---------------------------------------------------------------------------
# Phase 1 / Phase 2 prerequisite guard
# ---------------------------------------------------------------------------

def _phase1_guard(conn: sqlite3.Connection) -> None:
    """Exit early with a clear note if the harness can't run yet.

    Checks:
      1. `batter_form_archetype` exists AND has rows (Phase 2 backfill done).
      2. `pick_inputs` has the `form_archetype_centroid_json` column added
         by the Phase 2 migration block in etl/db.py.

    If either fails, prints the missing piece and exits 0 (no error — this
    is documented Phase-1 behavior, not a bug).
    """
    try:
        n_arch = conn.execute(
            "SELECT COUNT(*) FROM batter_form_archetype"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        # Table missing — DB was created before the Phase 1 migration ran.
        n_arch = 0
    if n_arch == 0:
        print("Phase 1 — run after batter_form_archetype is populated")
        print()
        print("  This harness compares the Form score with vs. without the")
        print("  archetype sub-signal across a 3x3 (window x min_hrs) sweep.")
        print("  It needs (a) batter_form_archetype rows for the backtest")
        print("  date range and (b) form_archetype_centroid_json available in")
        print("  pick_inputs (or computable from cached per-date Statcast).")
        print()
        print("  Phase 2 (etl/backfill_form_archetype.py + nightly hook)")
        print("  populates both. Re-run this then.")
        sys.exit(0)

    # Phase 2 column check — pick_inputs persisted centroid (used as today's
    # state proxy at the row's date).
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pick_inputs)").fetchall()}
    if "form_archetype_centroid_json" not in cols:
        print("Phase 1 — run after batter_form_archetype is populated")
        print()
        print("  batter_form_archetype has rows, but pick_inputs is missing")
        print("  the form_archetype_centroid_json column. Phase 2 migration")
        print("  adds it — call create_tables() against the active DB and")
        print("  re-load picks JSON for the backtest date range.")
        sys.exit(0)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def fetch_rows(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    """pick_inputs JOIN outcomes — same shape as backtest_form_anchors.

    Centroids per window are pulled separately by `_load_window_centroids`
    (one query per window, keyed by (player_id, date_through)). The reason
    centroids aren't joined inline: pick_inputs only stores ONE window's
    centroid_json (the production default, 14d). The harness needs all 3
    windows for the sweep, so it reads from `batter_form_archetype` directly
    for non-default windows.

    Returns one dict per (date, batter_id) row.
    """
    sql = """
        SELECT
            pi.date,
            pi.batter_id,
            pi.recent_hr_10g,
            pi.recent_iso_30g,
            pi.ev_trend,
            pi.recent_window_days,
            pi.form_archetype_centroid_json,
            pi.form_archetype_window,
            pi.form_archetype_n_hrs,
            CASE WHEN COALESCE(o.hr_count, 0) > 0 THEN 1 ELSE 0 END AS hit_hr
        FROM pick_inputs pi
        LEFT JOIN outcomes o
               ON o.date = pi.date AND o.batter_id = pi.batter_id
        WHERE pi.date >= ? AND pi.date <= ?
        ORDER BY pi.date, pi.batter_id
    """
    cur = conn.execute(sql, (start, end))
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _load_window_centroids(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    window_days: int,
) -> dict[tuple[int, str], dict]:
    """Bulk-load centroids for one window across the date range.

    Keyed by (player_id, date_through). date_through = date - 1 day per
    the as-of convention — caller must build the key with that offset.
    """
    rows = conn.execute(
        """
        SELECT player_id, date_through, feature_centroid_json, n_hrs_used
        FROM batter_form_archetype
        WHERE window_days = ?
          AND date_through BETWEEN date(?, '-1 day') AND date(?, '-1 day')
        """,
        (int(window_days), start, end),
    ).fetchall()
    out: dict[tuple[int, str], dict] = {}
    for pid, dt, centroid_json, n_hrs in rows:
        try:
            centroid = json.loads(centroid_json)
        except (TypeError, ValueError):
            continue
        out[(int(pid), str(dt))] = {"centroid": centroid, "n_hrs": int(n_hrs or 0)}
    return out


# ---------------------------------------------------------------------------
# Variant scoring — mirrors backtest_form_anchors' structure
# ---------------------------------------------------------------------------

def _scale(x: float, lo: float, hi: float) -> float:
    """Clamp + scale into 0-100, mirroring score_batters.min_max_scale."""
    return max(0.0, min(100.0, (x - lo) / (hi - lo) * 100.0))


def _form_score(
    row: dict,
    variant: str,
    centroids_by_window: dict[int, dict[tuple[int, str], dict]],
    subsignal_weight: float = _DEFAULT_SUBSIGNAL_WEIGHT,
) -> float:
    """Compute the Form score under one variant.

    No dampener applied — this isolates the input-formula change from the
    layoff layer, matching backtest_form_anchors.

    The archetype score is the L2-similarity between TODAY's state (proxied
    by the row's persisted centroid for the SAME window, since per-day
    today-state isn't recomputed in the backtest) and the centroid for that
    (batter, prior-day, window).

    NOTE Phase 2 limitation: a clean "today vector" path (recompute the
    state from raw Statcast at the row's date) isn't wired in. The
    Phase 2 sweep is a wiring smoke test — the variant grid prints, the
    counts are non-zero, but the SCORES themselves are signal proxies
    until Phase 3 wires a separate today-state pipeline. Documented here
    so the reviewer doesn't read deceptively-strong lift numbers as the
    real Phase 3 verdict.
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
        parts = variant.replace("archetype_", "").split("_")
        w_str, n_str = parts[0], parts[1]
        window_days = int(w_str.replace("d", ""))
        min_hrs = int(n_str.replace("hr", ""))

        # date_through = row.date - 1 day (as-of convention).
        try:
            d = datetime.strptime(row["date"], "%Y-%m-%d")
            dt = (d - timedelta(days=1)).strftime("%Y-%m-%d")
        except ValueError:
            dt = None

        if dt is not None:
            cmap = centroids_by_window.get(window_days, {})
            cinfo = cmap.get((int(row["batter_id"]), dt))
            if cinfo is not None and cinfo["n_hrs"] >= min_hrs:
                today_proxy = cinfo["centroid"]  # see docstring note
                archetype_score = _compute_form_archetype_match(
                    today_proxy, cinfo["centroid"],
                )
                if archetype_score is not None:
                    pieces.append((subsignal_weight, archetype_score))

    if not pieces:
        return 50.0
    total_w = sum(w for w, _ in pieces)
    return sum(w * v for w, v in pieces) / total_w


def score_variants(
    rows: list[dict],
    centroids_by_window: dict[int, dict[tuple[int, str], dict]],
    subsignal_weight: float = _DEFAULT_SUBSIGNAL_WEIGHT,
) -> list[dict]:
    """Score every row under every variant."""
    return [
        {
            "date": r["date"],
            "batter_id": r["batter_id"],
            "hit_hr": r["hit_hr"],
            "has_hr10": r.get("recent_hr_10g") is not None,
            "form": {
                v: _form_score(r, v, centroids_by_window, subsignal_weight)
                for v in VARIANTS
            },
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

    # Variant-specific: count how many rows actually had a centroid that
    # met the min_hrs gate (i.e., this variant differs from default for
    # how many rows?). For "default" this is 0 by construction.
    if variant == "default":
        n_archetype_active = 0
    else:
        # The form score with sub-signal differs from base mean iff a
        # centroid passed the gate. We re-detect by checking if the
        # values differ from the default variant's value for the same row.
        # (Cheap approximation; exact alternative is to recompute and
        # check pieces directly. For the report grid this is enough.)
        n_archetype_active = sum(
            1 for r in rows
            if abs(r["form"][variant] - r["form"]["default"]) > 1e-6
        )

    return {
        "n": n,
        "n_hr": n_hr,
        "hr_rate": n_hr / n if n else 0.0,
        "auc": _auc(values, labels),
        "top10_lift": _top_decile_lift(values, labels),
        "quint_mono": mono,
        "quint_rates": rates,
        "avg_rank_hr": avg_rank_hr,
        "n_archetype_active": n_archetype_active,
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
           f"{'auc':>8}{'top10_lift':>12}{'quint_mono':>12}{'avg_rank_hr':>13}"
           f"{'n_active':>11}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for v in VARIANTS:
        m = results[v]
        mono = f"{m['quint_mono']}/4" if m["quint_mono"] is not None else "n/a"
        print(f"  {v:<22}{m['n']:>7d}{m['n_hr']:>7d}{_fmt(m['hr_rate'], 4):>9}"
              f"{_fmt(m['auc'], 3):>8}{_fmt(m['top10_lift'], 2):>12}"
              f"{mono:>12}{_fmt(m['avg_rank_hr'], 1):>13}"
              f"{m['n_archetype_active']:>11d}")
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


def _pick_best_variant(results: dict[str, dict]) -> str | None:
    """Pick the highest-AUC archetype variant. Tie-breaks on top-decile lift."""
    candidates = []
    for v in VARIANTS:
        if v == "default":
            continue
        m = results[v]
        if m["auc"] is None or m["n_archetype_active"] == 0:
            continue
        candidates.append((v, m["auc"], m["top10_lift"] or 0.0))
    if not candidates:
        return None
    # Sort by (AUC desc, lift desc)
    candidates.sort(key=lambda t: (-t[1], -t[2]))
    return candidates[0][0]


def print_weight_sweep(
    rows: list[dict],
    centroids_by_window: dict[int, dict[tuple[int, str], dict]],
    best_variant: str,
) -> None:
    """For the best (window, min_hrs) combo, sweep sub-signal weights."""
    print()
    print(f"=== Sub-signal weight sweep (best variant: {best_variant}) ===")
    print()
    hdr = (f"  {'weight':>8}{'auc':>10}{'top10_lift':>14}"
           f"{'quint_mono':>14}{'avg_rank_hr':>15}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for w in WEIGHT_SWEEP:
        scored = score_variants(rows, centroids_by_window, subsignal_weight=w)
        common = [s for s in scored if s["has_hr10"]]
        m = compute_metrics(common, best_variant)
        mono = f"{m['quint_mono']}/4" if m["quint_mono"] is not None else "n/a"
        print(f"  {w:>8.2f}{_fmt(m['auc'], 3):>10}"
              f"{_fmt(m['top10_lift'], 2):>14}"
              f"{mono:>14}{_fmt(m['avg_rank_hr'], 1):>15}")
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
    ap.add_argument("--weight-sweep", action="store_true",
                    help="after the 3x3 sweep, also sweep sub-signal weights "
                         "{0.25, 0.5, 0.75, 1.0} on the best variant")
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
    if not rows:
        print(f"No pick_inputs/outcomes rows in {start}..{end}", file=sys.stderr)
        conn.close()
        sys.exit(1)

    # Pre-load centroids for each window in the sweep. One query per window.
    sweep_windows = sorted({w for w, _ in ARCHETYPE_SWEEP})
    centroids_by_window: dict[int, dict[tuple[int, str], dict]] = {}
    for w in sweep_windows:
        centroids_by_window[w] = _load_window_centroids(conn, start, end, w)

    conn.close()

    scored = score_variants(rows, centroids_by_window)
    n_dates = len({s["date"] for s in scored})

    common = [s for s in scored if s["has_hr10"]]

    print()
    print(f"=== Form archetype sweep ({start} -> {end}, {n_dates} dates) ===")
    print()
    print(f"  Coverage: {len(scored)} rows total | with recent_hr_10g "
          f"(comparison set): {len(common)}")
    centroid_total = sum(len(m) for m in centroids_by_window.values())
    print(f"  Centroids loaded: {centroid_total} across "
          f"{len(centroids_by_window)} windows {sweep_windows}")
    print()
    if not common:
        print("  No rows have recent_hr_10g — nothing to compare.", file=sys.stderr)
        sys.exit(1)

    results = {v: compute_metrics(common, v) for v in VARIANTS}
    print_report(results, n_dates)

    if args.weight_sweep:
        best = _pick_best_variant(results)
        if best is None:
            print("  [weight-sweep] no archetype variant differs from default "
                  "— nothing to sweep on")
        else:
            # Pass the RAW rows (not the scored ones) so print_weight_sweep
            # can re-score under each weight setting. The common-row filter
            # is applied per-weight inside the sweep.
            common_raw = [r for r in rows if r.get("recent_hr_10g") is not None]
            print_weight_sweep(common_raw, centroids_by_window, best)


if __name__ == "__main__":
    main()
