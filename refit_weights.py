#!/usr/bin/env python3
"""
refit_weights.py — Re-fit logistic regression weights from the live DB.

A1 (2026-05-26): rebuilt to refit against the 188-date 2025 backfill in
pick_inputs ⨝ outcomes (post-B11 score_form). The previous version
(2026-05-01) joined daily_picks ⨝ pick_inputs ⨝ outcomes, which only
exposed ~8 selected-pick rows per day instead of the full ~220-row slate.
Refitting on selected picks alone biases the regression toward what
production THINKS is a HR candidate — circular. The new flow:

  1. Pull every pick_inputs row joined with outcomes (full slate, post-
     as_of_date semantics from the backfill).
  2. Re-score each row using the CURRENT score_* functions (so score_form
     reflects B11's dropped recent_avg_30g; score_power reflects the
     2026-05-03 anchor re-tune; etc.). Persisted daily_picks scores are
     pre-B11 and would not honor the change.
  3. Fit logistic regression hit_hr ~ {power, matchup, park, form,
     weather, lineup} on factor scores normalized to [0, 1].
  4. Normalize positive coefficients to sum to 1.0 (matches
     WEIGHT_CONFIGS["default"]'s convention).
  5. Backtest candidate vs current_default vs stored composite. Metrics:
     top-decile lift, AUC, quintile monotonicity, average HR rank, top-8
     hit rate.

Decision rule (A1): ship the new weights IF top-decile lift improves by
> +1.0 pp AND AUC does not regress by more than 0.005. Otherwise hold.

Usage:
    python refit_weights.py                       # full 2025 backfill
    python refit_weights.py --since 2025-04-01    # custom start date
    python refit_weights.py --end 2025-09-30      # custom end date
    python refit_weights.py --update              # print WEIGHT_CONFIGS line
"""

import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from scipy.stats import pointbiserialr

# Make score_batters importable when run from project root or diagnostics/.
_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent))

from score_batters import (
    WEIGHT_CONFIGS,
    score_power,
    score_matchup,
    score_park,
    score_form,
    score_weather,
    score_lineup_position,
)

# Import the live weight config so the backtest's current_default actually
# reflects what's shipped — no more stale hardcoded snapshot. Refreshed
# 2026-05-26 alongside the A1 refit. Mirrors WEIGHT_CONFIGS["default"] in
# score_batters.py:
#   power 0.250, matchup 0.264, park 0.000, form 0.279, weather 0.057,
#   lineup 0.150
# Post-B11 the same dict applies; B11 only changes score_form's INTERNAL
# computation (drops recent_avg_30g), not the bucket weight.
try:
    SHIPPED_DEFAULT_W = dict(WEIGHT_CONFIGS["default"])
except Exception:
    SHIPPED_DEFAULT_W = {
        "power": 0.250, "matchup": 0.264, "park": 0.000,
        "form": 0.279, "weather": 0.057, "lineup": 0.150,
    }

# DB path — the single canonical anchor from etl.db (HR_BETS_DB-aware; B26).
from etl.db import DB_PATH

FACTORS = ["power", "matchup", "park", "form", "weather", "lineup"]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_from_db(
    db_path: Path = DB_PATH,
    since: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """
    Pull pick_inputs ⨝ outcomes (full slate, not just selected picks).
    Joined with daily_picks for game_pk/team/batting_order and daily_slate
    for venue. as_of_date semantics are inherited from how the rows were
    written into pick_inputs (the 2025 backfill threads as_of_date=D
    through every fetch — see etl/backfill_2025.py).
    """
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found at {db_path}")
    conn = sqlite3.connect(str(db_path))
    where = []
    params: list = []
    if since:
        where.append("pi.date >= ?")
        params.append(since)
    if end:
        where.append("pi.date <= ?")
        params.append(end)
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""

    # B16 (2026-05-27, bundles B19): pi.throws now read alongside pi.bats so
    # rescore_row gets actual handedness on both sides; the three slate_*_pct
    # columns let the rescore pass production-day slate percentiles to the
    # score_* kwargs, making rescored composite byte-identical to what
    # production composited on rows from 2026-05-27+ (NULL on older rows ->
    # rescore falls through to the legacy anchored path).
    sql = f"""
        SELECT
            pi.date,
            pi.batter_id                  AS player_id,
            pi.barrel_pct, pi.exit_velo, pi.hr_fb_pct, pi.iso,
            pi.xwoba_contact, pi.pull_fb_pct,
            pi.recent_hr_10g, pi.recent_iso_30g, pi.recent_avg_30g,
            pi.recent_window_days, pi.ev_trend,
            pi.recent_barrel_real_14d, pi.recent_xwoba_contact_14d, pi.recent_iso_14d,
            pi.pitcher_hr_per_9, pi.pitcher_era, pi.pitcher_hh_pct,
            pi.pitcher_k_per_9, pi.pitcher_fb_pct_allowed,
            pi.pitcher_recent_hr9_21d, pi.pitcher_recent_starts_21d,
            pi.pitcher_recent_era_21d, pi.pitcher_recent_k9_21d,
            pi.woba_vs_hand,
            pi.vegas_team_total_pct,
            pi.hr_park_factor,
            pi.temperature_f, pi.wind_mph, pi.wind_direction_deg,
            pi.humidity_pct, pi.is_dome,
            pi.batting_order,
            pi.season_hr,
            pi.bats, pi.throws,
            pi.slate_park_pct, pi.slate_weather_pct,
            pi.slate_pitcher_vulnerability_pct,
            dp.game_pk, dp.team AS batter_team,
            dp.power_score    AS persisted_power,
            dp.matchup_score  AS persisted_matchup,
            dp.park_score     AS persisted_park,
            dp.form_score     AS persisted_form,
            dp.weather_score  AS persisted_weather,
            dp.lineup_score   AS persisted_lineup,
            dp.composite      AS persisted_composite,
            ds.venue          AS game_venue,
            CASE WHEN COALESCE(o.hr_count, 0) > 0 THEN 1 ELSE 0 END AS hit_hr
        FROM pick_inputs pi
        INNER JOIN outcomes o
            ON o.date = pi.date AND o.batter_id = pi.batter_id
        LEFT JOIN daily_picks dp
            ON dp.date = pi.date AND dp.batter_id = pi.batter_id
        LEFT JOIN daily_slate ds
            ON ds.game_pk = dp.game_pk AND ds.date = dp.date
        {where_clause}
        ORDER BY pi.date, pi.batter_id
    """
    df = pd.read_sql_query(sql, conn, params=params)
    conn.close()
    return df


# ---------------------------------------------------------------------------
# Re-scoring (mirrors backtest_factors.rescore_row, adapted for the wider
# join here)
# ---------------------------------------------------------------------------

def rescore_row(row: pd.Series) -> dict:
    """
    Reconstruct batter/pitcher/weather dicts from a pick_inputs row and
    re-compute each factor with the CURRENT score_* functions. Post-B11
    score_form drops recent_avg_30g automatically.

    B16/B19 (2026-05-27): both `bats` and `throws` are now stored and read
    from pick_inputs — backtest no longer hardcodes "R" / "R" — so park's
    L/R adjustment and v1 matchup platoon bonus reflect actual handedness.
    The three slate_*_pct columns capture the within-slate percentile
    values that fed score_park / score_weather / score_matchup at
    production time; when non-NULL they're passed as kwargs to bypass the
    v1 fallback path and reproduce production composites byte-for-byte.
    NULL on pre-B16 rows -> rescore falls through to the legacy anchored
    path (matching pre-B16 backtest behavior; old rows stay comparable).
    """
    bats = row.get("bats") or "R"
    throws = row.get("throws") or "R"
    batter = {
        "barrel_pct": row.get("barrel_pct"),
        "exit_velo": row.get("exit_velo"),
        "hr_fb_pct": row.get("hr_fb_pct"),
        "iso": row.get("iso"),
        "xwoba_contact": row.get("xwoba_contact"),
        "pull_fb_pct": row.get("pull_fb_pct"),
        "recent_hr_10g": row.get("recent_hr_10g"),
        "recent_iso_30g": row.get("recent_iso_30g"),
        # recent_avg_30g column still loaded so old/new score_form versions
        # can both be exercised. Post-B11 score_form simply ignores it.
        "recent_avg_30g": row.get("recent_avg_30g"),
        "recent_window_days": row.get("recent_window_days"),
        "ev_trend": row.get("ev_trend"),
        "recent_barrel_real_14d": row.get("recent_barrel_real_14d"),
        "recent_xwoba_contact_14d": row.get("recent_xwoba_contact_14d"),
        "recent_iso_14d": row.get("recent_iso_14d"),
        "woba_vs_hand": row.get("woba_vs_hand"),
        "bats": bats,
        "season_hr": row.get("season_hr"),
    }
    pitcher = {
        "hr_per_9": row.get("pitcher_hr_per_9"),
        "era": row.get("pitcher_era"),
        "k_per_9": row.get("pitcher_k_per_9"),
        "hard_hit_pct_allowed": row.get("pitcher_hh_pct"),
        "fb_pct_allowed": row.get("pitcher_fb_pct_allowed"),
        "recent_hr9_21d": row.get("pitcher_recent_hr9_21d"),
        "recent_starts_21d": row.get("pitcher_recent_starts_21d"),
        "recent_era_21d": row.get("pitcher_recent_era_21d"),
        "recent_k9_21d": row.get("pitcher_recent_k9_21d"),
        "throws": throws,  # B19: now stored in pick_inputs
    }
    weather = {
        "temperature_f": row.get("temperature_f", 68),
        "wind_mph": row.get("wind_mph", 5),
        "wind_direction_deg": row.get("wind_direction_deg"),
        "humidity_pct": row.get("humidity_pct"),
        "dome": bool(row.get("is_dome", 0)),
    }
    venue = row.get("game_venue", "") or ""
    pf_df = pd.DataFrame()  # slate-relative path off; kwargs below handle it

    bo_raw = row.get("batting_order")
    try:
        bo = int(bo_raw) if bo_raw is not None and str(bo_raw).strip() else None
    except (ValueError, TypeError):
        bo = None

    # B16: persisted slate percentiles. When non-NULL, kwargs short-circuit
    # the slate_ctx lookup AND the v1 anchored fallback inside the score_*
    # functions, giving byte-identical results to production scoring.
    spp_raw = row.get("slate_park_pct")
    swp_raw = row.get("slate_weather_pct")
    spv_raw = row.get("slate_pitcher_vulnerability_pct")
    slate_park_pct = float(spp_raw) if spp_raw is not None and not pd.isna(spp_raw) else None
    slate_weather_pct = float(swp_raw) if swp_raw is not None and not pd.isna(swp_raw) else None
    slate_pitcher_vulnerability_pct = (
        float(spv_raw) if spv_raw is not None and not pd.isna(spv_raw) else None
    )

    # B16 follow-up (2026-05-27): rebuild a minimal slate_ctx so
    # score_matchup picks up the Vegas team_total_pct signal (the third
    # equal-weighted matchup term). vegas_team_total_pct is persisted in
    # pick_inputs since 2026-05-03 and batter_team comes off daily_picks.
    # Without this, score_matchup silently averages only [pitcher_pct,
    # woba] in the rescore path while production averaged
    # [pitcher_pct, woba, team_total_pct] — a -0.83 forward-parity gap on
    # the matchup factor and -0.24 on the composite. Pre-2026-05-03 rows
    # (vegas NULL) or rows missing batter_team fall through to slate_ctx
    # = None, matching the legacy rescore behavior on those rows.
    team_total_raw = row.get("vegas_team_total_pct")
    batter_team = row.get("batter_team")
    synthetic_slate_ctx = None
    if (
        team_total_raw is not None
        and not pd.isna(team_total_raw)
        and batter_team
    ):
        synthetic_slate_ctx = {
            "active": True,
            "team_total_pct": {batter_team: float(team_total_raw)},
        }

    return {
        "power": score_power(batter),
        "matchup": score_matchup(
            batter, pitcher,
            slate_ctx=synthetic_slate_ctx,
            batter_team=batter_team,
            slate_pitcher_vulnerability_pct=slate_pitcher_vulnerability_pct,
        ),
        "park": score_park(batter, venue, pf_df, slate_park_pct=slate_park_pct),
        "form": score_form(batter),
        "weather": score_weather(
            weather, venue=venue, batter_hand=bats,
            slate_weather_pct=slate_weather_pct,
        ),
        "lineup": score_lineup_position(bo),
    }


def rescore_all(df: pd.DataFrame) -> pd.DataFrame:
    """Apply rescore_row to every row, append new_<factor> columns."""
    print(f"  Re-scoring {len(df)} rows with current model (post-B11 score_form)...")
    rescored = df.apply(rescore_row, axis=1, result_type="expand")
    rescored.columns = [f"new_{c}" for c in rescored.columns]
    return pd.concat(
        [df.reset_index(drop=True), rescored.reset_index(drop=True)],
        axis=1,
    )


# ---------------------------------------------------------------------------
# Univariate diagnostics
# ---------------------------------------------------------------------------

def report_univariate(df: pd.DataFrame) -> None:
    print("\n=== Univariate factor diagnostics (re-scored) ===")
    print(f"{'factor':<10} {'pearson_r':>10} {'p-val':>10} {'top_q_lift':>11} {'n_present':>11}")
    print("-" * 56)
    for c in FACTORS:
        col = f"new_{c}"
        sub = df[df[col].notna() & df["hit_hr"].notna()]
        if len(sub) < 10:
            continue
        r, p = pointbiserialr(sub[col], sub["hit_hr"])
        q80 = sub[col].quantile(0.80)
        top_rate = sub[sub[col] >= q80]["hit_hr"].mean()
        base = sub["hit_hr"].mean()
        lift = top_rate / base if base > 0 else 0
        print(f"{c:<10} {r:>10.4f} {p:>10.2e} {lift:>10.2f}x {len(sub):>11}")


# ---------------------------------------------------------------------------
# Logistic regression
# ---------------------------------------------------------------------------

def fit_logistic(df: pd.DataFrame) -> dict:
    """
    Fit logistic regression hit_hr ~ {power, matchup, park, form, weather,
    lineup} on factor scores normalized to [0, 1] (raw scores are 0-100).
    Returns raw coefficient dict.

    L2 regularization at C=1.0 — mild shrinkage to prevent any single
    factor from dominating on small per-bin HR counts. Justified by:
    park_score is 0 in current_default precisely because an earlier refit
    saw near-zero coefficient; this refit needs to honor that prior unless
    park's coefficient is wildly positive.
    """
    cols = [f"new_{c}" for c in FACTORS]
    sub = df.dropna(subset=cols + ["hit_hr"]).copy()
    print(f"\n=== Logistic regression on {len(sub)} clean rows ===")
    # Normalize 0-100 -> 0-1; matches the task spec.
    X = sub[cols].values / 100.0
    y = sub["hit_hr"].values
    lr = LogisticRegression(C=1.0, max_iter=2000, penalty="l2")
    lr.fit(X, y)
    print(f"  Intercept: {lr.intercept_[0]:+.4f}")
    coefs = {f: float(lr.coef_[0][i]) for i, f in enumerate(FACTORS)}
    print(f"  Raw logistic coefficients (on 0-1 scale):")
    for c in FACTORS:
        print(f"    {c:<10} {coefs[c]:>+8.4f}")
    return coefs


def normalize_to_weights(coefs: dict) -> dict:
    """
    Convert raw logreg coefficients to weights summing to 1.0.

    Procedure: clip negatives to 0 (so a factor with negative signal gets
    0 weight, not reverse-bet weight), then divide by the sum. Mirrors
    WEIGHT_CONFIGS["default"]'s convention — all six weights non-negative,
    sum to 1.0.

    Audit note: zeroing negative coefficients is a deliberate design
    choice. If a factor (e.g. inverted park factor) ever has a coherent
    empirical NEGATIVE coefficient, the prior reasoning needs to be
    revisited before honoring the sign. For this refit park's prior
    coefficient was ~0 — if the new refit shows a clearly positive
    coefficient, that's worth promoting; clearly negative still means 0
    weight (consistent with the historical pattern).
    """
    pos = {k: max(0.0, v) for k, v in coefs.items()}
    z = sum(pos.values())
    if z == 0:
        return {k: 0.0 for k in FACTORS}
    return {k: pos[k] / z for k in FACTORS}


def normalize_with_lineup_carveout(
    coefs: dict, lineup_carve_out: float = 0.15, park_pin_zero: bool = True,
) -> dict:
    """
    Variant of `normalize_to_weights` that preserves structural priors:

      * Lineup gets a 0.15 floor regardless of its logreg coefficient.
        Rationale: lineup_score is a step function (9 values + None) so the
        logreg coefficient is noisy across refits; the 0.15 number is
        anchored on opportunity arithmetic (#1 hitter ~4.7 AB vs #9 ~3.2
        AB; HR-rate-per-PA * extra PAs ~= 15% of composite-impact budget).
      * Park gets pinned to 0 unless its coefficient is wildly positive
        (handled by the caller — here we just zero it if `park_pin_zero`
        is True). Rationale: park has been weighted 0 since the v1 refit
        because batters play their home park ~50% of games so the signal
        washes; the +0.05*park additive bonus (2026-05-03) handles
        within-slate park signal outside the weighted average.

    The remaining (1 - 0.15) = 0.85 is allocated proportionally to the
    POSITIVE-coefficient factors among {power, matchup, park, form,
    weather}, after the park-zero step.
    """
    # Step 1: park clamp
    coefs2 = dict(coefs)
    if park_pin_zero:
        coefs2["park"] = 0.0
    # Step 2: clip negatives among the non-lineup factors
    free_factors = ["power", "matchup", "park", "form", "weather"]
    pos = {k: max(0.0, coefs2[k]) for k in free_factors}
    z = sum(pos.values())
    if z == 0:
        out = {k: 0.0 for k in free_factors}
    else:
        remaining = 1.0 - lineup_carve_out
        out = {k: pos[k] / z * remaining for k in free_factors}
    out["lineup"] = lineup_carve_out
    return out


# ---------------------------------------------------------------------------
# Backtest metrics
# ---------------------------------------------------------------------------

def _auc(values: np.ndarray, labels: np.ndarray) -> float | None:
    """ROC-AUC via Mann-Whitney U, tie-averaged ranks."""
    n_pos = float(labels.sum())
    n_neg = float(len(labels) - n_pos)
    if n_pos == 0 or n_neg == 0:
        return None
    order = np.argsort(values, kind="mergesort")
    sv = values[order]
    ranks = np.empty(len(values), dtype=float)
    i = 0
    n = len(values)
    while i < n:
        j = i
        while j < n and sv[j] == sv[i]:
            j += 1
        ranks[order[i:j]] = (i + j - 1) / 2.0 + 1.0
        i = j
    rank_pos = ranks[labels == 1].sum()
    u = rank_pos - n_pos * (n_pos + 1) / 2.0
    return u / (n_pos * n_neg)


def _top_decile_lift_pp(values: np.ndarray, labels: np.ndarray) -> tuple[float, float, float]:
    """
    Top-decile lift expressed as a percentage-point gap above the overall
    HR rate. Returns (top10_rate, base_rate, lift_pp).
    """
    n = len(values)
    if n == 0:
        return (0.0, 0.0, 0.0)
    base = float(labels.mean())
    paired = sorted(zip(values, labels), key=lambda t: t[0], reverse=True)
    cut = max(1, n // 10)
    top = paired[:cut]
    top_rate = sum(y for _, y in top) / len(top)
    return (top_rate, base, top_rate - base)


def _quintile_rates(values: np.ndarray, labels: np.ndarray) -> list[float]:
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


def _avg_hr_rank(df: pd.DataFrame, score_col: str) -> float | None:
    """Average within-date rank of HR hitters (1 = best). Lower is better."""
    ranks = []
    for _, g in df.groupby("date"):
        ordered = g.sort_values(score_col, ascending=False)
        for r, (_, row) in enumerate(ordered.iterrows(), start=1):
            if row["hit_hr"] == 1:
                ranks.append(r)
    return float(np.mean(ranks)) if ranks else None


def _top8_hit_rate(df: pd.DataFrame, score_col: str) -> float:
    """
    What fraction of days have >=1 HR among the top-8 picks (capped 2 per
    game, no batter duplicates)? Mirrors generate_picks' top-8 selection.
    """
    days_with_hit = 0
    days_with_picks = 0
    for date_str, g in df.groupby("date"):
        if g[score_col].isna().all():
            continue
        gp_counts: dict = {}
        seen: set = set()
        picks: list = []
        ordered = g.sort_values(score_col, ascending=False)
        for _, row in ordered.iterrows():
            if len(picks) >= 8:
                break
            pid = row["player_id"]
            if pid in seen:
                continue
            gpk = row.get("game_pk")
            if gpk is not None and gp_counts.get(gpk, 0) >= 2:
                continue
            picks.append(row)
            seen.add(pid)
            if gpk is not None:
                gp_counts[gpk] = gp_counts.get(gpk, 0) + 1
        if picks:
            days_with_picks += 1
            if any(p["hit_hr"] == 1 for p in picks):
                days_with_hit += 1
    return days_with_hit / days_with_picks if days_with_picks else 0.0


def compute_composite_from_weights(df: pd.DataFrame, weights: dict) -> np.ndarray:
    """Apply weight dict to the re-scored new_<factor> columns."""
    out = np.zeros(len(df), dtype=float)
    for f in FACTORS:
        col = f"new_{f}"
        v = df[col].fillna(50.0).values  # missing factor -> neutral 50
        out += weights.get(f, 0.0) * v
    return out


def evaluate_composite(df: pd.DataFrame, label: str, score: np.ndarray) -> dict:
    """Compute the full metric set for one composite series."""
    labels = df["hit_hr"].values.astype(float)
    auc = _auc(score, labels)
    top10_rate, base_rate, lift_pp = _top_decile_lift_pp(score, labels)
    rates = _quintile_rates(score, labels)
    mono_steps = (
        sum(1 for i in range(len(rates) - 1) if rates[i + 1] > rates[i])
        if rates else 0
    )
    mono = mono_steps == 4
    # avg_rank_hr / top8_hit_rate need the date column → use a temp frame
    tmp = df[["date", "player_id", "game_pk", "hit_hr"]].copy()
    tmp["_s"] = score
    avg_rank = _avg_hr_rank(tmp, "_s")
    top8 = _top8_hit_rate(tmp, "_s")
    return {
        "label": label,
        "n": int(len(score)),
        "auc": auc,
        "top10_rate": top10_rate,
        "base_rate": base_rate,
        "top10_lift_pp": lift_pp,
        "quintile_rates": rates,
        "monotonic": mono,
        "mono_steps": mono_steps,
        "avg_rank_hr": avg_rank,
        "top8_hit_rate": top8,
    }


def print_eval(e: dict) -> None:
    mono_tag = f"{e['mono_steps']}/4{' (strict)' if e['monotonic'] else ''}"
    auc = f"{e['auc']:.4f}" if e["auc"] is not None else "n/a"
    avg_rank = f"{e['avg_rank_hr']:.1f}" if e["avg_rank_hr"] is not None else "n/a"
    print(f"  {e['label']:<28} n={e['n']}")
    print(f"    auc:              {auc}")
    print(f"    top-decile rate:  {e['top10_rate']:.4f}  (base {e['base_rate']:.4f})")
    print(f"    top-decile lift:  {e['top10_lift_pp']*100:+.2f} pp")
    print(f"    quintile rates:   "
          + "  ".join(f"{r:.4f}" for r in e["quintile_rates"]))
    print(f"    quintile mono:    {mono_tag}")
    print(f"    avg HR rank:      {avg_rank}")
    print(f"    top-8 hit rate:   {e['top8_hit_rate']:.4f}")


# ---------------------------------------------------------------------------
# Decision rule
# ---------------------------------------------------------------------------

def decision(
    cur: dict, cand: dict, lift_threshold_pp: float = 1.0,
    auc_regression_cap: float = 0.005,
) -> tuple[bool, str]:
    """
    Ship-or-hold rule from the A1 spec:
      Ship IF top-decile lift improves by > +1.0 pp
      AND AUC does not regress by more than 0.005.
    """
    if cur["auc"] is None or cand["auc"] is None:
        return (False, "AUC unavailable for one side; cannot decide.")
    d_lift = cand["top10_lift_pp"] - cur["top10_lift_pp"]
    d_auc = cand["auc"] - cur["auc"]
    ship = (d_lift > lift_threshold_pp / 100.0) and (d_auc > -auc_regression_cap)
    rationale = (
        f"delta top-decile lift = {d_lift*100:+.2f} pp "
        f"(threshold > +{lift_threshold_pp:.2f} pp); "
        f"delta AUC = {d_auc:+.4f} "
        f"(must be > -{auc_regression_cap:.4f}). "
        + ("SHIP." if ship else "HOLD.")
    )
    return (ship, rationale)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    ap.add_argument("--since", default=None,
                    help="Earliest training date YYYY-MM-DD (inclusive).")
    ap.add_argument("--end", default=None,
                    help="Latest training date YYYY-MM-DD (inclusive).")
    ap.add_argument("--db", default=str(DB_PATH),
                    help=f"DB path (default: {DB_PATH})")
    ap.add_argument("--update", action="store_true",
                    help="Print WEIGHT_CONFIGS line ready to paste.")
    ap.add_argument("--holdout-frac", type=float, default=0.3,
                    help="Fraction of LATEST dates to hold out for OOS test. "
                         "Default 0.3 (chronological 70/30 split).")
    ap.add_argument("--custom", default=None,
                    help='Evaluate an arbitrary weight vector on the same OOS '
                         'holdout. Format: "power=0.27,matchup=0.46,park=0,'
                         'form=0.075,weather=0.119,lineup=0.0774". Sums need '
                         'not equal 1.0 (we renormalize); missing factors '
                         'default to 0. Useful for testing manual blends '
                         'between FREE and PINNED.')
    args = ap.parse_args()

    # Parse --custom early so we can fail fast on bad input.
    custom_weights = None
    if args.custom:
        custom_weights = {f: 0.0 for f in FACTORS}
        for kv in args.custom.split(","):
            k, _, v = kv.partition("=")
            k = k.strip()
            if k not in FACTORS:
                print(f"ERROR: unknown factor '{k}' in --custom. "
                      f"Valid: {FACTORS}", file=sys.stderr)
                return 2
            custom_weights[k] = float(v.strip())
        s = sum(custom_weights.values())
        if s <= 0:
            print(f"ERROR: --custom weights sum to {s}; need positive total.",
                  file=sys.stderr)
            return 2
        if abs(s - 1.0) > 1e-6:
            print(f"  [--custom] weights sum to {s:.6f}; renormalizing to 1.0")
            custom_weights = {k: v / s for k, v in custom_weights.items()}

    db_path = Path(args.db)
    print(f"Using DB: {db_path}")
    print(f"  Date range: {args.since or '(earliest)'} -> {args.end or '(latest)'}")

    df = load_from_db(db_path, since=args.since, end=args.end)
    if df is None or len(df) == 0:
        print("ERROR: no rows. Confirm pick_inputs JOIN outcomes covers the window.",
              file=sys.stderr)
        return 1
    n_dates = df["date"].nunique()
    print(f"  Loaded {len(df)} rows over {n_dates} dates "
          f"({df['date'].min()} -> {df['date'].max()})")
    print(f"  Overall HR rate: {df['hit_hr'].mean():.4%}")

    # Re-score every row with the CURRENT scoring functions (post-B11).
    df = rescore_all(df)

    # Sanity check: confirm the re-scored form_score differs from the
    # persisted form_score (because B11 changed score_form). If they
    # match exactly, something's wrong with the rescore path.
    if "persisted_form" in df.columns:
        delta = (df["new_form"] - df["persisted_form"]).abs()
        n_changed = int((delta > 0.01).sum())
        print(f"  Sanity: {n_changed}/{len(df)} rows have new_form != persisted_form "
              f"(B11 effect; expected high for rows where recent_avg_30g was present)")

    report_univariate(df)

    # Chronological train/test split: hold out the LATEST `holdout_frac` of
    # dates as out-of-sample. The point of a refit is to find weights that
    # generalize forward — in-sample lift on the same 188 dates that
    # generated the coefficients is circular. The OOS numbers are what
    # should drive the ship-or-hold call.
    all_dates = sorted(df["date"].unique())
    n_dates = len(all_dates)
    n_holdout = max(1, int(round(n_dates * args.holdout_frac)))
    train_dates = set(all_dates[: n_dates - n_holdout])
    test_dates = set(all_dates[n_dates - n_holdout:])
    train_df = df[df["date"].isin(train_dates)].reset_index(drop=True)
    test_df = df[df["date"].isin(test_dates)].reset_index(drop=True)
    print(f"\n  Chronological split: train {len(train_df)} rows "
          f"({len(train_dates)} dates, {min(train_dates)} -> {max(train_dates)}); "
          f"holdout {len(test_df)} rows ({len(test_dates)} dates, "
          f"{min(test_dates)} -> {max(test_dates)})")

    # --- IN-SAMPLE fit on training portion only ---
    coefs = fit_logistic(train_df)
    candidate_free = normalize_to_weights(coefs)
    candidate_pinned = normalize_with_lineup_carveout(coefs)

    # Also fit on the full sample for reference / `--update` line.
    coefs_full = fit_logistic(df)
    candidate_free_full = normalize_to_weights(coefs_full)
    candidate_pinned_full = normalize_with_lineup_carveout(coefs_full)

    def round_and_renorm(w: dict) -> dict:
        r = {k: round(v, 3) for k, v in w.items()}
        s = sum(r.values())
        if s > 0 and abs(s - 1.0) > 1e-9:
            biggest = max(r, key=lambda k: r[k])
            r[biggest] += round(1.0 - s, 3)
        return r

    free_train = round_and_renorm(candidate_free)
    free_full = round_and_renorm(candidate_free_full)
    pinned_train = round_and_renorm(candidate_pinned)
    pinned_full = round_and_renorm(candidate_pinned_full)

    def print_weight_table(label: str, w: dict) -> None:
        print(f"\n=== Candidate weights -- {label} ===")
        for k in FACTORS:
            print(f"  {k:<10} {w[k]:.3f}    (current: {SHIPPED_DEFAULT_W[k]:.3f}, "
                  f"delta {w[k] - SHIPPED_DEFAULT_W[k]:+.3f})")
        print(f"  {'sum':<10} {sum(w.values()):.3f}")

    print_weight_table("FREE (train-only): raw logreg, zero negatives, sum=1", free_train)
    print_weight_table("PINNED (train-only): lineup=0.15, park=0, scaled free", pinned_train)
    print_weight_table("FREE (full-sample) -- reference", free_full)
    print_weight_table("PINNED (full-sample) -- reference", pinned_full)

    # --- Backtest on the HOLDOUT (out-of-sample) ---
    print("\n=== Backtest -- OUT-OF-SAMPLE (holdout dates) ===")
    cur_score_oos = compute_composite_from_weights(test_df, SHIPPED_DEFAULT_W)
    free_score_oos = compute_composite_from_weights(test_df, free_train)
    pinned_score_oos = compute_composite_from_weights(test_df, pinned_train)
    stored_oos = test_df["persisted_composite"].fillna(0.0).values

    e_stored_oos = evaluate_composite(test_df, "persisted (live, OOS)", stored_oos)
    e_cur_oos = evaluate_composite(test_df, "current_default (OOS)", cur_score_oos)
    e_free_oos = evaluate_composite(test_df, "candidate FREE (OOS)", free_score_oos)
    e_pinned_oos = evaluate_composite(test_df, "candidate PINNED (OOS)", pinned_score_oos)
    e_custom_oos = None
    if custom_weights is not None:
        custom_score_oos = compute_composite_from_weights(test_df, custom_weights)
        e_custom_oos = evaluate_composite(
            test_df, "candidate CUSTOM (OOS)", custom_score_oos
        )
    print()
    print_eval(e_stored_oos)
    print()
    print_eval(e_cur_oos)
    print()
    print_eval(e_free_oos)
    print()
    print_eval(e_pinned_oos)
    if e_custom_oos is not None:
        print()
        print_eval(e_custom_oos)
        print()
        print(f"  CUSTOM weights used:")
        for k in FACTORS:
            cur_val = SHIPPED_DEFAULT_W[k]
            cust_val = custom_weights[k]
            free_val = free_train[k]
            print(f"    {k:<10} {cust_val:.4f}    "
                  f"(current: {cur_val:.3f}, FREE: {free_val:.3f}, "
                  f"delta-vs-current: {cust_val - cur_val:+.4f})")

    # --- In-sample backtest on the FULL window, for reference only ---
    print("\n=== Backtest -- IN-SAMPLE (full window, reference only -- overfit risk) ===")
    cur_score_full = compute_composite_from_weights(df, SHIPPED_DEFAULT_W)
    free_score_full = compute_composite_from_weights(df, free_full)
    pinned_score_full = compute_composite_from_weights(df, pinned_full)
    e_cur_full = evaluate_composite(df, "current_default (IS)", cur_score_full)
    e_free_full = evaluate_composite(df, "candidate FREE (IS)", free_score_full)
    e_pinned_full = evaluate_composite(df, "candidate PINNED (IS)", pinned_score_full)
    print()
    print_eval(e_cur_full)
    print()
    print_eval(e_free_full)
    print()
    print_eval(e_pinned_full)

    # --- Verdict driven by OOS numbers ---
    print("\n=== Verdict (driven by OOS holdout, not in-sample) ===")
    ship_free, rationale_free = decision(e_cur_oos, e_free_oos)
    ship_pinned, rationale_pinned = decision(e_cur_oos, e_pinned_oos)
    print(f"  FREE:   {rationale_free}")
    print(f"  PINNED: {rationale_pinned}")
    ship_custom = False
    if e_custom_oos is not None:
        ship_custom, rationale_custom = decision(e_cur_oos, e_custom_oos)
        print(f"  CUSTOM: {rationale_custom}")
    any_ship = ship_free or ship_pinned or ship_custom
    if any_ship:
        print(f"  -> At least one variant MEETS the shipping threshold on OOS. "
              f"User decision: present in WEIGHT_REFIT_LOG.md, decide which "
              f"variant to flip into WEIGHT_CONFIGS['default'].")
    else:
        print(f"  -> No variant meets the OOS threshold. HOLD; document "
              f"and ship NOTHING.")

    if args.update:
        # Use full-sample fits for the paste lines — once we ship, the full
        # 188-date training set is the right thing to fit on. The train-
        # only weights were just for OOS validation.
        free_line = '"default":       {' + ", ".join(
            f'"{k}": {free_full[k]:.3f}' for k in FACTORS
        ) + "},   # FREE: pure logreg"
        pinned_line = '"default":       {' + ", ".join(
            f'"{k}": {pinned_full[k]:.3f}' for k in FACTORS
        ) + "},   # PINNED: lineup=0.15 floor, park=0 pin"
        print(f"\n=== Paste lines for score_batters.py WEIGHT_CONFIGS ===")
        print(f"  FREE   ->  {free_line}")
        print(f"  PINNED ->  {pinned_line}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
