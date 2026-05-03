#!/usr/bin/env python3
"""
backtest_flags.py — Compare model performance across flag combinations.

Two flags currently sit at default-off in score_batters.py awaiting empirical
validation:

  * USE_CAREER_PRIOR     (PR #11) — Bayesian shrinkage of per-PA rates toward
                                    career mean
  * USE_SEASON_HR_FLOOR  (PR #16) — hard floor on power_score keyed off season
                                    HR count

This harness re-scores every historical pick_input row under each of the four
flag combinations (off / career_only / floor_only / both), re-ranks within
each date, and prints per-combo metrics so we can decide which (if any) to
flip on as the new production default.

Methodology:

  * Power is re-scored from the stored Statcast inputs; matchup / park / form /
    weather / lineup are pulled directly from `daily_picks` (those factors are
    not affected by either flag).
  * Composite is recomputed using WEIGHT_CONFIGS["default"]; rows are then
    re-ranked within their date.
  * `season_hr_at_date` is derived from a correlated subquery on `outcomes`
    summing HR counts strictly *before* pi.date, so today's HRs never leak
    into today's floor. `current_pa` (used by career-prior shrinkage) is
    proxied as cumulative AB through prior dates.
  * Metrics:
      - avg_rank_hr     — mean rank of batters who actually homered
                          (lower is better; rank 1 = top of board)
      - auc             — area under ROC for hit_hr ~ composite
                          (higher is better; 0.5 = random)
      - top10_lift      — HR rate in top 10% of composite / overall HR rate
                          (higher is better; >1 means model concentrates HRs
                          in its highest-confidence picks)
      - quintile_mono   — count of monotone steps as composite rises across
                          5 quintiles (4 = perfectly monotone, 0 = inverted)

Usage:
    python diagnostics/backtest_flags.py
    python diagnostics/backtest_flags.py --days 14
    python diagnostics/backtest_flags.py --start 2026-04-15 --end 2026-05-01

Requires the production DB at <projects>/data/hr_bets.db (resolved via
etl.db.DB_PATH).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

# Resolve project root so `import score_batters` works whether invoked as a
# module or executed directly.
_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

import score_batters as sb
from etl.db import DB_PATH


# Factor scores other than power come straight from daily_picks; only power is
# affected by the two flags under test, so we keep the stored values for
# everything else and rebuild composite from them.
_NONPOWER_FACTORS = ("matchup", "park", "form", "weather", "lineup")

FLAG_COMBOS: dict[str, tuple[bool, bool]] = {
    # name              (USE_CAREER_PRIOR, USE_SEASON_HR_FLOOR)
    "off":              (False, False),
    "career_only":      (True,  False),
    "floor_only":       (False, True),
    "both":             (True,  True),
}


# ─────────────────────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────────────────────

def fetch_data(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    """Pull the joined panel of (pick_input, daily_picks factors, outcome).

    Adds two derived columns per row to support the two flags:

      * season_hr_at_date — HR count strictly *before* pi.date (no leakage)
      * career_pa_proxy   — cumulative AB before pi.date (used as current_n
                            in career-prior shrinkage)

    Both are computed in SQL via correlated subqueries against `outcomes`.
    """
    rows = conn.execute(
        """
        SELECT
            pi.date,
            pi.batter_id,
            pi.barrel_pct,
            pi.exit_velo,
            pi.hr_fb_pct,
            pi.iso,
            pi.xwoba_contact,
            pi.pull_fb_pct,
            dp.batter_name,
            dp.composite       AS stored_composite,
            dp.power_score     AS stored_power,
            dp.matchup_score   AS matchup,
            dp.park_score      AS park,
            dp.form_score      AS form,
            dp.weather_score   AS weather,
            dp.lineup_score    AS lineup,
            dp.weight_config   AS weight_config,
            COALESCE(o.hr_count, 0) AS today_hr_count,
            (SELECT COALESCE(SUM(o2.hr_count), 0)
               FROM outcomes o2
              WHERE o2.batter_id = pi.batter_id
                AND o2.date < pi.date
                AND SUBSTR(o2.date, 1, 4) = SUBSTR(pi.date, 1, 4)
            ) AS season_hr_at_date,
            (SELECT COALESCE(SUM(o3.ab), 0)
               FROM outcomes o3
              WHERE o3.batter_id = pi.batter_id
                AND o3.date < pi.date
                AND SUBSTR(o3.date, 1, 4) = SUBSTR(pi.date, 1, 4)
            ) AS career_pa_proxy
        FROM pick_inputs pi
        INNER JOIN daily_picks dp
                ON dp.date = pi.date AND dp.batter_id = pi.batter_id
        LEFT  JOIN outcomes o
                ON o.date = pi.date AND o.batter_id = pi.batter_id
        WHERE pi.date >= ? AND pi.date <= ?
        """,
        (start, end),
    ).fetchall()
    return [dict(r) for r in rows]


def load_career_lookup(conn: sqlite3.Connection) -> dict[int, dict]:
    """Load career_batting into {player_id: row_dict}. Returns {} on error."""
    try:
        rows = conn.execute(
            """
            SELECT player_id, career_pa, career_hr, career_hr_per_pa,
                   career_iso, career_woba
              FROM career_batting
             WHERE career_pa IS NOT NULL AND career_pa > 0
            """,
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {r["player_id"]: dict(r) for r in rows if r["player_id"]}


# ─────────────────────────────────────────────────────────────────────────────
# Per-row scoring
# ─────────────────────────────────────────────────────────────────────────────

def _build_batter_dict(row: dict, career_lookup: dict, apply_prior: bool) -> dict:
    """Translate a panel row into the dict shape `score_power` expects.

    When `apply_prior` is True and the batter has a career row, applies the
    same shrinkage formula generate_picks.enrich_with_career_prior uses
    (iso direct + barrel_pct via career_hr_per_pa × 200 proxy).
    """
    batter: dict[str, Any] = {
        "barrel_pct":    row["barrel_pct"],
        "exit_velo":     row["exit_velo"],
        "hr_fb_pct":     row["hr_fb_pct"],
        "iso":           row["iso"],
        "xwoba_contact": row["xwoba_contact"],
        "pull_fb_pct":   row["pull_fb_pct"],
        "season_hr":     row["season_hr_at_date"],
    }

    if not apply_prior:
        return batter

    career = career_lookup.get(row["batter_id"])
    if not career:
        return batter

    current_pa = row["career_pa_proxy"] or 0
    if current_pa <= 0:
        return batter

    k = sb.CAREER_PRIOR_K

    # Direct rate-stat shrinkage. score_power doesn't use woba directly, but
    # iso *is* a power input, so shrinking it matters here.
    cur_iso = batter.get("iso")
    cv_iso = career.get("career_iso")
    if cur_iso is not None and cur_iso > 0 and cv_iso is not None and cv_iso > 0:
        batter["iso"] = sb.shrink_to_career(cur_iso, current_pa, cv_iso, k=k)

    # Synthetic barrel% proxy: career_hr_per_pa × 200, gated at ≥ 1000 career
    # PA (mirrors enrich_with_career_prior so we're testing the same wiring
    # production would ship).
    cv_hr_per_pa = career.get("career_hr_per_pa")
    if cv_hr_per_pa and career.get("career_pa", 0) >= 1000:
        career_barrel_proxy = min(25.0, cv_hr_per_pa * 200)
        cur_barrel = batter.get("barrel_pct")
        if cur_barrel is not None and cur_barrel > 0:
            batter["barrel_pct"] = sb.shrink_to_career(
                cur_barrel, current_pa, career_barrel_proxy, k=k,
            )

    return batter


def _composite_from(row: dict, power: float) -> float:
    """Recompose composite using stored non-power factor scores + new power."""
    weights = sb.WEIGHT_CONFIGS["default"]
    total = power * weights["power"]
    for f in _NONPOWER_FACTORS:
        v = row.get(f)
        if v is None:
            v = 50.0  # neutral fallback for any null factor
        total += v * weights[f]
    return total


def rescore_with_flags(
    rows: list[dict],
    use_career: bool,
    use_floor: bool,
    career_lookup: dict[int, dict],
) -> tuple[list[dict], dict[str, int]]:
    """Re-score every row under the given flag combo and re-rank per date.

    Returns (scored, footprint) where:
      * scored is a list of dicts {date, batter_id, composite, rank, hit_hr}
        with rank assigned descending within each date (1 = best composite).
      * footprint counts how often each flag actually mutated a power score
        (separate from whether the metric needle moved). Useful sanity-check
        when downstream metrics barely budge.
    """
    # Toggle module-level flags so score_power() picks up the floor.
    sb.USE_CAREER_PRIOR = use_career
    sb.USE_SEASON_HR_FLOOR = use_floor

    footprint = {"prior_mutated": 0, "floor_lifted": 0}

    scored: list[dict] = []
    for r in rows:
        # Score once with flags off to get a baseline power for diff counting.
        baseline_batter = _build_batter_dict(r, career_lookup, apply_prior=False)
        sb.USE_SEASON_HR_FLOOR = False
        baseline_power = sb.score_power(baseline_batter)

        # Now apply the actual combo and rescore.
        sb.USE_CAREER_PRIOR = use_career
        sb.USE_SEASON_HR_FLOOR = use_floor
        batter = _build_batter_dict(r, career_lookup, apply_prior=use_career)

        # Track whether prior mutation changed inputs (independent of floor).
        if use_career:
            iso_changed = (
                batter.get("iso") is not None
                and r.get("iso") is not None
                and abs((batter["iso"] or 0) - (r["iso"] or 0)) > 1e-9
            )
            barrel_changed = (
                batter.get("barrel_pct") is not None
                and r.get("barrel_pct") is not None
                and abs((batter["barrel_pct"] or 0) - (r["barrel_pct"] or 0)) > 1e-9
            )
            if iso_changed or barrel_changed:
                footprint["prior_mutated"] += 1

        power = sb.score_power(batter)

        # Track floor lifts: power above baseline (after any prior mutation)
        # implies the floor kicked in.
        if use_floor and power > baseline_power + 1e-9:
            # Subtract any movement attributable to the prior so we count
            # only true floor lifts. The floor only ever raises scores, so
            # power > both no-floor variants is the conservative test.
            if not use_career or power > baseline_power + 1e-9:
                footprint["floor_lifted"] += 1

        composite = _composite_from(r, power)
        scored.append({
            "date":       r["date"],
            "batter_id":  r["batter_id"],
            "composite":  composite,
            "hit_hr":     1 if (r["today_hr_count"] or 0) > 0 else 0,
        })

    # Rank within date, descending composite.
    by_date: dict[str, list[dict]] = defaultdict(list)
    for s in scored:
        by_date[s["date"]].append(s)
    for date_rows in by_date.values():
        date_rows.sort(key=lambda x: x["composite"], reverse=True)
        for i, s in enumerate(date_rows, start=1):
            s["rank"] = i

    return scored, footprint


# ─────────────────────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────────────────────

def _auc(scores: list[float], labels: list[int]) -> float:
    """Mann-Whitney U–based ROC AUC. Returns 0.5 when ill-defined."""
    pos = [s for s, y in zip(scores, labels) if y == 1]
    neg = [s for s, y in zip(scores, labels) if y == 0]
    if not pos or not neg:
        return float("nan")
    # U = sum over (pos, neg) pairs of [s_p > s_n] + 0.5*[s_p == s_n]
    # AUC = U / (|pos| * |neg|). O(P*N) — fine for our row counts.
    wins = 0.0
    for p in pos:
        for n in neg:
            if p > n:
                wins += 1
            elif p == n:
                wins += 0.5
    return wins / (len(pos) * len(neg))


def _top_decile_lift(scored: list[dict]) -> float:
    """HR rate among top 10% composites / overall HR rate. NaN if no HRs."""
    n = len(scored)
    if n == 0:
        return float("nan")
    overall_rate = sum(s["hit_hr"] for s in scored) / n
    if overall_rate == 0:
        return float("nan")
    by_score = sorted(scored, key=lambda s: s["composite"], reverse=True)
    cutoff = max(1, n // 10)
    top = by_score[:cutoff]
    top_rate = sum(s["hit_hr"] for s in top) / len(top)
    return top_rate / overall_rate


def _quintile_monotonicity(scored: list[dict]) -> tuple[int, list[float]]:
    """Bin into 5 quintiles by composite; count monotone-up steps.

    Returns (steps_up, [hr_rate_q1..q5]). Perfect monotonicity = 4.
    """
    n = len(scored)
    if n < 10:
        return 0, []
    by_score = sorted(scored, key=lambda s: s["composite"])
    bin_size = n // 5
    rates: list[float] = []
    for q in range(5):
        lo = q * bin_size
        hi = (q + 1) * bin_size if q < 4 else n
        chunk = by_score[lo:hi]
        if not chunk:
            rates.append(float("nan"))
            continue
        rates.append(sum(s["hit_hr"] for s in chunk) / len(chunk))
    steps = sum(1 for i in range(4) if rates[i + 1] > rates[i])
    return steps, rates


def compute_metrics(scored: list[dict]) -> dict[str, Any]:
    """Aggregate four headline metrics for one flag combo."""
    if not scored:
        return {"n_rows": 0, "n_hr": 0}

    n = len(scored)
    n_hr = sum(s["hit_hr"] for s in scored)

    hr_ranks = [s["rank"] for s in scored if s["hit_hr"] == 1]
    avg_rank_hr = sum(hr_ranks) / len(hr_ranks) if hr_ranks else float("nan")

    composites = [s["composite"] for s in scored]
    labels = [s["hit_hr"] for s in scored]
    auc = _auc(composites, labels)

    top10_lift = _top_decile_lift(scored)
    mono_steps, quintile_rates = _quintile_monotonicity(scored)

    return {
        "n_rows":         n,
        "n_hr":           n_hr,
        "hr_rate":        n_hr / n if n else 0.0,
        "avg_rank_hr":    avg_rank_hr,
        "auc":            auc,
        "top10_lift":     top10_lift,
        "quintile_mono":  mono_steps,
        "quintile_rates": quintile_rates,
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI / formatting
# ─────────────────────────────────────────────────────────────────────────────

def _fmt(x: float, prec: int = 3) -> str:
    if x is None or (isinstance(x, float) and (x != x)):  # NaN check
        return "  n/a"
    return f"{x:.{prec}f}"


def print_table(results: dict[str, dict[str, Any]], days_label: str) -> None:
    print()
    print(f"=== Flag-combo backtest ({days_label}) ===")
    print()
    header = f"{'combo':<13} {'n_rows':>7} {'n_hr':>5} {'hr_rate':>8} {'avg_rank':>9} {'auc':>6} {'top10_lift':>10} {'quint_mono':>10}"
    print(header)
    print("-" * len(header))
    for name in ("off", "career_only", "floor_only", "both"):
        m = results[name]
        if not m.get("n_rows"):
            print(f"{name:<13}   (no data)")
            continue
        print(
            f"{name:<13} "
            f"{m['n_rows']:>7d} "
            f"{m['n_hr']:>5d} "
            f"{_fmt(m['hr_rate'], 4):>8} "
            f"{_fmt(m['avg_rank_hr'], 1):>9} "
            f"{_fmt(m['auc'], 3):>6} "
            f"{_fmt(m['top10_lift'], 2):>10} "
            f"{m['quintile_mono']:>10d}"
        )
    print()
    # Quintile rate detail (lowest -> highest composite quintile)
    print("Quintile HR rates (low -> high composite, perfect = strictly increasing):")
    for name in ("off", "career_only", "floor_only", "both"):
        m = results[name]
        if not m.get("n_rows") or not m.get("quintile_rates"):
            continue
        rates = "  ".join(_fmt(r, 4) for r in m["quintile_rates"])
        print(f"  {name:<13} {rates}")
    print()
    # Verdict: which combo wins on each metric?
    print("Best combo by metric (lower-is-better for avg_rank, higher for the rest):")
    metrics_to_rank = [
        ("avg_rank_hr",  "lower"),
        ("auc",          "higher"),
        ("top10_lift",   "higher"),
        ("quintile_mono","higher"),
    ]
    for metric, direction in metrics_to_rank:
        scored: list[tuple[str, float]] = []
        for name, m in results.items():
            v = m.get(metric)
            if v is None or (isinstance(v, float) and v != v):
                continue
            scored.append((name, v))
        if not scored:
            continue
        scored.sort(key=lambda x: x[1], reverse=(direction == "higher"))
        winner, val = scored[0]
        print(f"  {metric:<14} -> {winner:<13} ({_fmt(val, 3)})")
    print()


def print_footprints(footprints: dict[str, dict[str, int]], total_rows: int) -> None:
    """Print how often each flag actually mutated a power score.

    A flag combo with metrics close to baseline could mean either
    (a) the flag rarely fires, or (b) it fires but doesn't help. The
    footprint count tells these two cases apart.
    """
    print("Flag footprint (row counts where the flag mutated power):")
    for name in ("off", "career_only", "floor_only", "both"):
        fp = footprints.get(name, {})
        prior = fp.get("prior_mutated", 0)
        floor = fp.get("floor_lifted", 0)
        prior_pct = (100.0 * prior / total_rows) if total_rows else 0.0
        floor_pct = (100.0 * floor / total_rows) if total_rows else 0.0
        print(
            f"  {name:<13} prior_mutated={prior:>5d} ({prior_pct:>5.1f}%)   "
            f"floor_lifted={floor:>5d} ({floor_pct:>5.1f}%)"
        )
    print()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--days", type=int, default=30,
                   help="Look-back window ending at the latest pick date (default 30)")
    p.add_argument("--start", type=str,
                   help="Explicit start date (YYYY-MM-DD); overrides --days")
    p.add_argument("--end", type=str,
                   help="Explicit end date (YYYY-MM-DD); defaults to latest in DB")
    p.add_argument("--db", type=str, default=str(DB_PATH),
                   help=f"DB path (default: {DB_PATH})")
    args = p.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # Resolve date window.
    latest = conn.execute("SELECT MAX(date) FROM pick_inputs").fetchone()[0]
    if not latest:
        print(f"No rows in pick_inputs at {args.db}", file=sys.stderr)
        sys.exit(1)
    end_date = args.end or latest
    if args.start:
        start_date = args.start
    else:
        ed = datetime.strptime(end_date, "%Y-%m-%d").date()
        start_date = (ed - timedelta(days=args.days - 1)).isoformat()

    # Pull data once (the panel is the same across combos; only scoring changes).
    rows = fetch_data(conn, start_date, end_date)
    career_lookup = load_career_lookup(conn)
    conn.close()

    if not rows:
        print(f"No rows in window {start_date}..{end_date}", file=sys.stderr)
        sys.exit(1)

    distinct_dates = len({r["date"] for r in rows})
    print(f"Loaded {len(rows)} rows across {distinct_dates} dates "
          f"({start_date} -> {end_date}); career_lookup size = {len(career_lookup)}")

    # Run all four combos and collect metrics.
    results: dict[str, dict[str, Any]] = {}
    footprints: dict[str, dict[str, int]] = {}
    for name, (uc, uf) in FLAG_COMBOS.items():
        scored, footprint = rescore_with_flags(rows, uc, uf, career_lookup)
        results[name] = compute_metrics(scored)
        footprints[name] = footprint

    # Restore defaults so we don't leak state if anything else imports sb later
    # in the same process.
    sb.USE_CAREER_PRIOR = False
    sb.USE_SEASON_HR_FLOOR = False

    print_table(results, days_label=f"{start_date} -> {end_date}, {distinct_dates} dates")
    print_footprints(footprints, total_rows=len(rows))


if __name__ == "__main__":
    main()
