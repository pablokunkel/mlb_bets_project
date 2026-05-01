#!/usr/bin/env python3
"""
refit_weights.py — Re-fit logistic regression weights on (optionally) enriched CSV.

Computes:
  1. Per-feature univariate diagnostics: Pearson r, top-quintile lift, AUC.
  2. Logistic regression with hit_hr ~ standardized features.
  3. Backtest top-8 hit rate of new weights vs current default + legacy.

If raw_data_v2.csv exists (output of backfill_features_v2.py), uses the
enriched feature set: composite, power, matchup, park, form, weather +
xwoba_contact, pull_fb_pct, fb_pct_allowed.

Otherwise falls back to the original 5 sub-scores.

Usage:
    python refit_weights.py                     # uses raw_data_v2.csv if present
    python refit_weights.py --csv raw_data.csv  # force original
    python refit_weights.py --update            # write learned weights into score_batters.py
"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from scipy.stats import pointbiserialr


def load_csv(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
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
    """Convert positive logreg coefs to weights summing to (1 - lineup_carve_out)."""
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
    """Run hit-rate backtest with old weights, new weights, percentile-reranked, etc."""
    df = df.copy()

    # Percentile rerank for the within-slate factors (matchup/park/weather)
    df["matchup_pct"] = percentile_rerank(df, "matchup_score")
    df["park_pct_rr"] = percentile_rerank(df, "park_score")
    df["weather_pct_rr"] = percentile_rerank(df, "weather_score")

    # Variant A: existing CSV composite (legacy baseline)
    a = hit_rate(df, "composite")

    # Variant B: current shipped default (learned w/ percentile)
    df["comp_default"] = (
        0.217 * df["power_score"]
        + 0.270 * df["matchup_pct"]
        + 0.000 * df["park_pct_rr"]
        + 0.304 * df["form_score"]
        + 0.060 * df["weather_pct_rr"]
    )
    b = hit_rate(df, "comp_default")

    # Variant C: NEW learned weights from this fit
    df["comp_new"] = 0.0
    for feat, w in factor_weights.items():
        if feat == "lineup":
            continue
        if feat == "matchup":
            # Apply over within-slate matchup percentile to mirror live scoring
            df["comp_new"] += w * df["matchup_pct"]
        elif feat == "park":
            df["comp_new"] += w * df["park_pct_rr"]
        elif feat == "weather":
            df["comp_new"] += w * df["weather_pct_rr"]
        elif feat == "power":
            df["comp_new"] += w * df["power_score"]
            # Note: xwoba/pull_fb signal already lives inside the new
            # power_score when live; for backfit we use a coarse proxy by
            # treating the bucket weight as already-blended.
        elif feat == "form":
            df["comp_new"] += w * df["form_score"]
    c = hit_rate(df, "comp_new")

    return {
        "legacy_csv_composite": a,
        "current_default": b,
        "new_learned": c,
        "lift_vs_legacy": c - a,
        "lift_vs_current": c - b,
    }


def main():
    ap = argparse.ArgumentParser(description="Refit weights on (optionally enriched) CSV")
    ap.add_argument("--csv", default=None,
                    help="Path to CSV (default: raw_data_v2.csv if present, else raw_data.csv)")
    ap.add_argument("--update", action="store_true",
                    help="If set, prints WEIGHT_CONFIGS line ready to paste into score_batters.py")
    args = ap.parse_args()

    here = Path(__file__).parent
    if args.csv:
        csv_path = Path(args.csv)
    else:
        v2 = here / "raw_data_v2.csv"
        v1 = here / "raw_data.csv"
        csv_path = v2 if v2.exists() else v1

    print(f"Using CSV: {csv_path}")
    df = load_csv(csv_path)
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
