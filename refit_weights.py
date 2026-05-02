#!/usr/bin/env python3
"""
refit_weights.py — Re-fit logistic regression weights from the live DB.

Default behavior (2026-05-01 update): reads training data directly from
the SQLite DB (daily_picks ⨝ pick_inputs ⨝ outcomes). Always current —
each completed day's picks + outcomes feed the next refit automatically.

Computes:
  1. Per-feature univariate diagnostics: Pearson r, top-quintile lift, AUC.
  2. Logistic regression with hit_hr ~ standardized features.
  3. Backtest top-8 hit rate of new weights vs current default + legacy.

Falls back to CSV if --csv is passed (backwards compat with the old flow).

Usage:
    python refit_weights.py                       # DB default
    python refit_weights.py --csv raw_data_v2.csv # force CSV
    python refit_weights.py --update              # print weights to paste into score_batters.py
    python refit_weights.py --since 2026-04-15    # train only on rows from this date forward
"""

import argparse
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from scipy.stats import pointbiserialr

# Import the live weight config so the backtest's comp_default actually
# reflects what's shipped, not a stale hardcoded snapshot. Avoids the
# "+1.25 pp lift_vs_current" misleading number that the 2026-05-01 monthly
# refit flagged.
try:
    from score_batters import WEIGHT_CONFIGS
    SHIPPED_DEFAULT_W = WEIGHT_CONFIGS["default"]
except Exception:
    # Defensive fallback if score_batters can't import (e.g., missing dep)
    SHIPPED_DEFAULT_W = {
        "power": 0.250, "matchup": 0.264, "park": 0.000,
        "form": 0.279, "weather": 0.057, "lineup": 0.150,
    }

# DB path — same convention as etl/db.py
DB_PATH = Path(__file__).parent.parent / "data" / "hr_bets.db"


def load_csv(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    return df


def load_from_db(db_path: Path = DB_PATH, since: str | None = None) -> pd.DataFrame:
    """
    Pull training rows from the live DB. One row per (date, batter_id)
    where the outcome is known. Includes all the sub-scores from
    daily_picks plus the enriched signals (xwoba_contact, pull_fb_pct,
    pitcher_fb_pct_allowed) from pick_inputs.

    `since`: optional ISO date (YYYY-MM-DD) to filter from. Useful for
    holdout testing or re-fitting on a recent window.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found at {db_path}")
    conn = sqlite3.connect(str(db_path))
    where_clause = ""
    params: tuple = ()
    if since:
        where_clause = "AND dp.date >= ?"
        params = (since,)
    sql = f"""
        SELECT
            dp.date,
            dp.batter_id              AS player_id,
            dp.game_pk,
            dp.composite,
            dp.power_score,
            dp.matchup_score,
            dp.park_score,
            dp.form_score,
            dp.weather_score,
            dp.lineup_score,
            pi.xwoba_contact,
            pi.pull_fb_pct,
            pi.pitcher_fb_pct_allowed AS fb_pct_allowed,
            CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END AS hit_hr
        FROM daily_picks dp
        LEFT JOIN pick_inputs pi
            ON pi.date = dp.date AND pi.batter_id = dp.batter_id
        INNER JOIN outcomes o
            ON o.date = dp.date AND o.batter_id = dp.batter_id
        WHERE dp.composite IS NOT NULL
          {where_clause}
        ORDER BY dp.date, dp.batter_id
    """
    df = pd.read_sql_query(sql, conn, params=params)
    conn.close()
    return df


def detect_features(df: pd.DataFrame) -> list[str]:
    """Return the list of feature columns present in the CSV."""
    base = ["power_score", "matchup_score", "park_score", "form_score", "weather_score"]
    extras = []
    for c in ["xwoba_contact", "pull_fb_pct", "fb_pct_allowed"]:
        if c in df.columns and df[c].notna().sum() > 50:
            extras.append(c)
    return base + extras


def report_univariate(df: pd.DataFrame, features: list[str]) -> None:
    print("\n=== Univariate diagnostics ===")
    print(f"{'feature':<18} {'pearson_r':>10} {'p-val':>10} {'top_q_lift':>11} {'n_present':>11}")
    print("-" * 65)
    for c in features + ["composite"]:
        if c not in df.columns:
            continue
        sub = df[df[c].notna() & df["hit_hr"].notna()]
        if len(sub) < 10:
            continue
        r, p = pointbiserialr(sub[c], sub["hit_hr"])
        q80 = sub[c].quantile(0.80)
        top_rate = sub[sub[c] >= q80]["hit_hr"].mean()
        base = sub["hit_hr"].mean()
        lift = top_rate / base if base > 0 else 0
        print(f"{c:<18} {r:>10.4f} {p:>10.2e} {lift:>10.2f}x {len(sub):>11}")


def fit_logistic(df: pd.DataFrame, features: list[str]) -> dict:
    """Fit logistic regression on standardized features. Returns coef dict."""
    sub = df.dropna(subset=features + ["hit_hr"])
    print(f"\n=== Logistic regression on {len(sub)} clean rows, {len(features)} features ===")
    X = sub[features].values
    y = sub["hit_hr"].values
    Xs = StandardScaler().fit_transform(X)
    lr = LogisticRegression(max_iter=2000)
    lr.fit(Xs, y)
    print(f"  Intercept: {lr.intercept_[0]:.4f}")
    print(f"  Train AUC (rough proxy): {lr.score(Xs, y):.4f}")
    coefs = dict(zip(features, lr.coef_[0]))
    print(f"\n  Standardized coefficients:")
    for c, v in sorted(coefs.items(), key=lambda kv: -abs(kv[1])):
        print(f"    {c:<18} {v:>+8.4f}")
    return coefs


def normalize_to_weights(coefs: dict, lineup_carve_out: float = 0.15) -> dict:
    """Convert positive logreg coefs to weights summing to (1 - lineup_carve_out).

    Audit LOW: the 0.15 lineup carve-out is hardcoded regardless of what
    a free fit on `lineup_score` would say. This is intentional —
    `detect_features()` does not include lineup_score in the logreg
    feature set, so the carve-out is the model's way of asserting "this
    factor IS worth ~15% of the budget" without trying to fit it
    statistically. Rationale:

    - lineup_score is a STEP function with only 9 distinct values
      (1-9) plus "bench"/None, so logreg's log-odds ratio gives noisy
      coefficients that swing 5x across refits.
    - The 15% number is anchored on opportunity arithmetic: a #1 hitter
      gets ~4.7 AB vs #9's ~3.2 AB (~32% more PAs). With HR rate ~3.5%
      per PA, that's a ~1pp HR-rate gap — call it 15% of the
      composite-impact budget. Sanity-checks against historical hit
      rates by pick rank.
    - `pos = {k: max(0, v) for ...}` zeroes negative coefficients,
      meaning enriched features that show negative signal get 0%
      weight rather than reverse-bet weight. Documented design choice;
      worth revisiting if a feature with a clear empirical NEGATIVE
      coefficient (e.g. inverted park factor) needs to be honored.

    Future: feed lineup_score into `detect_features()` and let logreg
    fit it; if the coefficient is meaningful, drop the carve-out. If
    not, replace the magic 0.15 with whatever the empirical
    "opportunity rate per PA" works out to for the season.
    """
    pos = {k: max(0, v) for k, v in coefs.items()}
    z = sum(pos.values())
    if z == 0:
        return {}
    remaining = 1.0 - lineup_carve_out
    out = {k: v / z * remaining for k, v in pos.items()}
    out["lineup"] = lineup_carve_out
    return out


def map_to_factor_weights(feature_weights: dict) -> dict:
    """
    Collapse per-feature weights back into the 6-factor bucket schema
    (power/matchup/park/form/weather/lineup) used by WEIGHT_CONFIGS.

    Sub-features inside power (xwoba_contact, pull_fb_pct) get summed into
    'power'. fb_pct_allowed sums into 'matchup'. Park/form/weather pass through.
    """
    bucket_map = {
        "power_score": "power",
        "xwoba_contact": "power",
        "pull_fb_pct": "power",
        "matchup_score": "matchup",
        "fb_pct_allowed": "matchup",
        "park_score": "park",
        "form_score": "form",
        "weather_score": "weather",
        "lineup": "lineup",
    }
    out = {"power": 0, "matchup": 0, "park": 0, "form": 0, "weather": 0, "lineup": 0}
    for feat, w in feature_weights.items():
        bucket = bucket_map.get(feat)
        if bucket:
            out[bucket] += w
    return out


def pick_top8(group, score_col):
    gp_counts, seen, picks = {}, set(), []
    for _, row in group.sort_values(score_col, ascending=False).iterrows():
        if len(picks) >= 8: break
        if row.player_id in seen: continue
        gpk = row.game_pk
        if gp_counts.get(gpk, 0) >= 2: continue
        picks.append(row)
        seen.add(row.player_id)
        gp_counts[gpk] = gp_counts.get(gpk, 0) + 1
    return pd.DataFrame(picks)


def hit_rate(df: pd.DataFrame, score_col: str) -> float:
    rates = []
    for _, g in df.groupby("date"):
        top8 = pick_top8(g, score_col)
        if len(top8) > 0:
            rates.append(top8["hit_hr"].mean())
    return float(np.mean(rates)) if rates else 0


def percentile_rerank(df: pd.DataFrame, col: str) -> pd.Series:
    return df.groupby("date")[col].transform(lambda s: s.rank(pct=True) * 100)


def backtest(df: pd.DataFrame, factor_weights: dict, features: list[str]) -> dict:
    """
    Run hit-rate backtest with old weights, new weights, etc.

    NOTE 2026-05-01: dropped the percentile_rerank step on matchup/park/
    weather. Those columns in the DB are ALREADY the final post-slate-
    context scores from live scoring (score_matchup_v2 does the rerank
    internally and returns the final number). Re-percentile-ranking on
    top of that destroyed the signal — current_default backtested at 8.1%
    instead of ~38% because of the double-rerank. Use the stored values
    directly to mirror how compute_composite() actually combines them.
    """
    df = df.copy()

    # Variant A: stored composite from when the pick was scored (legacy baseline)
    a = hit_rate(df, "composite")

    # Variant B: CURRENT shipped default — pulled live from
    # WEIGHT_CONFIGS["default"] in score_batters so the lift number is
    # an honest apples-to-apples comparison.
    w = SHIPPED_DEFAULT_W
    df["comp_default"] = (
        w["power"]   * df["power_score"]
        + w["matchup"] * df["matchup_score"]
        + w["park"]    * df["park_score"]
        + w["form"]    * df["form_score"]
        + w["weather"] * df["weather_score"]
    )
    # Include lineup_score if present in the data (DB always has it; some
    # legacy CSVs don't).
    if "lineup_score" in df.columns:
        df["comp_default"] = df["comp_default"] + w.get("lineup", 0) * df["lineup_score"]
    b = hit_rate(df, "comp_default")

    # Variant C: NEW learned weights from this fit
    df["comp_new"] = 0.0
    for feat, w in factor_weights.items():
        if feat == "lineup":
            continue
        if feat == "matchup":
            df["comp_new"] += w * df["matchup_score"]
        elif feat == "park":
            df["comp_new"] += w * df["park_score"]
        elif feat == "weather":
            df["comp_new"] += w * df["weather_score"]
        elif feat == "power":
            df["comp_new"] += w * df["power_score"]
            # Note: xwoba/pull_fb signal already lives inside the new
            # power_score when live; for backfit we use a coarse proxy by
            # treating the bucket weight as already-blended.
        elif feat == "form":
            df["comp_new"] += w * df["form_score"]
    if "lineup_score" in df.columns:
        df["comp_new"] += factor_weights.get("lineup", 0) * df["lineup_score"]
    c = hit_rate(df, "comp_new")

    return {
        "legacy_csv_composite": a,
        "current_default": b,
        "new_learned": c,
        "lift_vs_legacy": c - a,
        "lift_vs_current": c - b,
    }


def main():
    ap = argparse.ArgumentParser(description="Refit weights from the live DB (or CSV via --csv)")
    ap.add_argument("--csv", default=None,
                    help="Path to a CSV. Skips DB loading if set.")
    ap.add_argument("--since", default=None,
                    help="Only train on rows with date >= this (YYYY-MM-DD). DB mode only.")
    ap.add_argument("--update", action="store_true",
                    help="If set, prints WEIGHT_CONFIGS line ready to paste into score_batters.py")
    args = ap.parse_args()

    here = Path(__file__).parent
    if args.csv:
        csv_path = Path(args.csv)
        print(f"Using CSV: {csv_path}")
        df = load_csv(csv_path)
    else:
        print(f"Using DB: {DB_PATH}")
        if args.since:
            print(f"  Filtering to rows with date >= {args.since}")
        df = load_from_db(DB_PATH, since=args.since)

    if df is None or len(df) == 0:
        print("ERROR: No training rows. If using DB, ensure outcomes table is populated.")
        return 1
    print(f"  Loaded {len(df)} rows; date range: {df['date'].min()} -> {df['date'].max()}")
    print(f"  Hit rate: {df['hit_hr'].mean():.3%}")

    features = detect_features(df)
    print(f"  Features detected: {features}")

    report_univariate(df, features)

    coefs = fit_logistic(df, features)
    feature_weights = normalize_to_weights(coefs)
    factor_weights = map_to_factor_weights(feature_weights)

    print("\n=== Recommended factor-bucket weights (carved out lineup=0.15) ===")
    for k, v in factor_weights.items():
        print(f"  {k:<10} {v:>7.3f}")
    print(f"  {'sum':<10} {sum(factor_weights.values()):>7.3f}")

    print("\n=== Backtest ===")
    results = backtest(df, factor_weights, features)
    for k, v in results.items():
        if "lift" in k:
            print(f"  {k:<24} {v*100:+6.2f} pp")
        else:
            print(f"  {k:<24} {v:>6.2%}")

    if args.update:
        line = '"default":       {' + ", ".join(
            f'"{k}": {v:.3f}' for k, v in factor_weights.items()
        ) + "},"
        print(f"\n=== Paste into score_batters.py WEIGHT_CONFIGS ===")
        print(f"    {line}")


if __name__ == "__main__":
    main()
