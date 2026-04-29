#!/usr/bin/env python3
"""
run_2025_sweep.py — Full config sweep + blended card sim using 2025 data.

Replaces 2024 imports with 2025 data, runs same analysis pipeline.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from score_batters import WEIGHT_CONFIGS, compute_composite, select_top_picks
from fetch_daily_data import DOME_STADIUMS, get_hardcoded_park_factors
from mlb_2025_tiers import ALL_TIERS, get_tier_for_batter, PITCHERS_2025

RESULTS_DIR = Path(__file__).parent.parent / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

TIER_POINTS = {1: 1, 2: 3, 3: 9}

BLEND_COMBOS = [(4, 2, 2), (3, 2, 3), (2, 3, 3), (3, 3, 2)]

VENUES = [
    ("NYY", "Yankee Stadium"), ("BOS", "Fenway Park"), ("TOR", "Rogers Centre"),
    ("BAL", "Oriole Park at Camden Yards"), ("TB", "Tropicana Field"),
    ("CLE", "Progressive Field"), ("MIN", "Target Field"), ("KC", "Kauffman Stadium"),
    ("DET", "Comerica Park"), ("CWS", "Guaranteed Rate Field"),
    ("HOU", "Minute Maid Park"), ("TEX", "Globe Life Field"),
    ("SEA", "T-Mobile Park"), ("LAA", "Angel Stadium"), ("OAK", "Oakland Coliseum"),
    ("ATL", "Truist Park"), ("PHI", "Citizens Bank Park"), ("NYM", "Citi Field"),
    ("MIA", "loanDepot park"), ("WSH", "Nationals Park"),
    ("MIL", "American Family Field"), ("CHC", "Wrigley Field"),
    ("STL", "Busch Stadium"), ("CIN", "Great American Ball Park"),
    ("PIT", "PNC Park"), ("LAD", "Dodger Stadium"), ("SD", "Petco Park"),
    ("SF", "Oracle Park"), ("ARI", "Chase Field"), ("COL", "Coors Field"),
]


def simulate_game_day(tier_batters, config_name, rng, park_factors):
    pitcher_lookup = {p["name"]: p for p in PITCHERS_2025}
    pitcher_names = list(pitcher_lookup.keys())
    n_games = rng.integers(12, 16)
    venues = list(VENUES)
    rng.shuffle(venues)
    all_scored = []
    hr_names = set()

    for g_idx in range(min(n_games, 15)):
        _, venue = venues[g_idx % len(venues)]
        hp = rng.choice(pitcher_names)
        ap = rng.choice(pitcher_names)
        gpk = int(rng.integers(1_000_000, 9_999_999))
        is_dome = venue in DOME_STADIUMS
        weather = {
            "temperature_f": 72.0 if is_dome else float(rng.normal(78, 10)),
            "wind_mph": 0.0 if is_dome else float(max(0, rng.normal(7, 4))),
            "wind_direction_deg": 0 if is_dome else int(rng.integers(0, 360)),
            "dome": is_dome,
        }
        available = list(tier_batters)
        rng.shuffle(available)
        home = available[:min(5, len(available))]
        away = available[min(5, len(available)):min(10, len(available))]

        for lineup, opp_name in [(away, hp), (home, ap)]:
            opp = pitcher_lookup.get(opp_name,
                {"hr_per_9": 1.2, "hard_hit_pct_allowed": 35, "throws": "R"})
            for b in lineup:
                entry = {
                    **b,
                    "player_id": hash(b["name"]) % 1_000_000,
                    "recent_hr_14d": float(min(5, max(0, b["hr"]/25 + rng.normal(0, 0.8)))),
                    "recent_barrel_pct_14d": float(max(0, b.get("barrel_pct", 8) + rng.normal(0, 3))),
                    "ev_trend_14d": float(rng.normal(0, 1.5)),
                }
                result = compute_composite(entry, opp, venue, weather, park_factors, config_name)
                result["player_id"] = entry["player_id"]
                result["game_pk"] = gpk
                all_scored.append(result)

                # HR outcome
                rate = b["hr"] / max(b["pa"], 1)
                prob = 1 - (1 - rate) ** 4.2
                prob *= opp.get("hr_per_9", 1.2) / 1.2
                pf = park_factors[park_factors["venue"] == venue]
                if not pf.empty:
                    prob *= pf.iloc[0]["hr_park_factor"] / 100
                prob = min(prob, 0.35)
                if rng.random() < prob:
                    hr_names.add(b["name"])

    all_scored.sort(key=lambda x: x["composite"], reverse=True)
    return {"scored": all_scored, "hr_names": hr_names}


def sweep_tier(tier, n_days, seeds):
    pf = get_hardcoded_park_factors()
    batters = ALL_TIERS[tier]
    results = []

    for config_name in WEIGHT_CONFIGS:
        seed_rates, seed_avg, seed_zero, seed_g1, seed_g2, seed_g3, seed_a8, seed_std = \
            [], [], [], [], [], [], [], []
        for seed in seeds:
            rng = np.random.default_rng(seed)
            day_hits = []
            for _ in range(n_days):
                sim = simulate_game_day(batters, config_name, rng, pf)
                picks = select_top_picks(sim["scored"], n=8, max_per_game=2)
                hits = sum(1 for p in picks if p["name"] in sim["hr_names"])
                day_hits.append(hits)
            n = len(day_hits)
            th = sum(day_hits)
            seed_rates.append(th / (n*8) * 100)
            seed_avg.append(th / n)
            seed_zero.append(sum(1 for h in day_hits if h == 0) / n * 100)
            seed_g1.append(sum(1 for h in day_hits if h >= 1) / n * 100)
            seed_g2.append(sum(1 for h in day_hits if h >= 2) / n * 100)
            seed_g3.append(sum(1 for h in day_hits if h >= 3) / n * 100)
            seed_a8.append(sum(1 for h in day_hits if h == 8) / n * 100)
            seed_std.append(float(np.std([h/8*100 for h in day_hits])))

        results.append({
            "config": config_name,
            "tier": tier,
            "mean_hit_rate": round(np.mean(seed_rates), 2),
            "std_hit_rate": round(np.std(seed_rates), 2),
            "mean_avg_hits": round(np.mean(seed_avg), 2),
            "mean_zero_rate": round(np.mean(seed_zero), 1),
            "mean_gte1": round(np.mean(seed_g1), 1),
            "mean_gte2": round(np.mean(seed_g2), 1),
            "mean_gte3": round(np.mean(seed_g3), 1),
            "mean_all8": round(np.mean(seed_a8), 1),
            "mean_daily_std": round(np.mean(seed_std), 2),
            "weights": WEIGHT_CONFIGS[config_name],
        })
    results.sort(key=lambda x: x["mean_hit_rate"], reverse=True)
    return results


def blend_backtest(combo, tier_configs, n_days, seeds):
    pf = get_hardcoded_park_factors()
    label = f"{combo[0]}/{combo[1]}/{combo[2]}"
    all_pts, all_hits, all_zero, all_g1, all_cov, all_tcov = [], [], [], [], [], []
    all_t1, all_t2, all_t3, all_best, all_worst, all_pstd = [], [], [], [], [], []

    for seed in seeds:
        rng = np.random.default_rng(seed)
        dp, dh, dc, dtc, dt1, dt2, dt3 = [], [], [], [], [], [], []
        for _ in range(n_days):
            card = []
            for tier, count in [(1, combo[0]), (2, combo[1]), (3, combo[2])]:
                if count == 0: continue
                sim = simulate_game_day(ALL_TIERS[tier], tier_configs[tier], rng, pf)
                picks = select_top_picks(sim["scored"], n=count, max_per_game=2)
                for p in picks:
                    card.append({"name": p["name"], "tier": tier,
                                 "composite": p["composite"],
                                 "hit_hr": p["name"] in sim["hr_names"]})
            hits = sum(1 for c in card if c["hit_hr"])
            th = {t: sum(1 for c in card if c["tier"]==t and c["hit_hr"]) for t in [1,2,3]}
            tc = {t: sum(1 for c in card if c["tier"]==t) for t in [1,2,3]}
            pts = sum(TIER_POINTS[c["tier"]] for c in card if c["hit_hr"])
            active = [t for t in [1,2,3] if tc[t] > 0]
            cov = sum(1 for t in active if th[t] >= 1)
            dp.append(pts); dh.append(hits); dc.append(cov == len(active)); dtc.append(cov)
            if combo[0] > 0: dt1.append(th[1] / max(tc[1], 1))
            if combo[1] > 0: dt2.append(th[2] / max(tc[2], 1))
            if combo[2] > 0: dt3.append(th[3] / max(tc[3], 1))
        n = len(dp)
        all_pts.append(np.mean(dp)); all_hits.append(np.mean(dh))
        all_zero.append(sum(1 for h in dh if h == 0) / n * 100)
        all_g1.append(sum(1 for h in dh if h >= 1) / n * 100)
        all_cov.append(sum(dc) / n * 100); all_tcov.append(np.mean(dtc))
        if dt1: all_t1.append(np.mean(dt1) * 100)
        if dt2: all_t2.append(np.mean(dt2) * 100)
        if dt3: all_t3.append(np.mean(dt3) * 100)
        all_best.append(max(dp)); all_worst.append(min(dp)); all_pstd.append(np.std(dp))

    return {
        "combo": label, "combo_tuple": list(combo),
        "tier_configs": {str(k): v for k, v in tier_configs.items()},
        "mean_avg_points": round(np.mean(all_pts), 2),
        "mean_points_std": round(np.mean(all_pstd), 2),
        "mean_hit_rate": round(np.mean(all_hits) / 8 * 100, 2),
        "mean_avg_hits": round(np.mean(all_hits), 2),
        "mean_zero_rate": round(np.mean(all_zero), 1),
        "mean_gte1_rate": round(np.mean(all_g1), 1),
        "mean_all_tiers_covered": round(np.mean(all_cov), 1),
        "mean_tiers_covered": round(np.mean(all_tcov), 2),
        "mean_t1_hit": round(np.mean(all_t1), 1) if all_t1 else 0,
        "mean_t2_hit": round(np.mean(all_t2), 1) if all_t2 else 0,
        "mean_t3_hit": round(np.mean(all_t3), 1) if all_t3 else 0,
        "mean_best_day": round(np.mean(all_best), 1),
        "mean_worst_day": round(np.mean(all_worst), 1),
    }


def main():
    seeds = [42, 43, 44]
    n_days = 25

    # ========== PHASE 1: CONFIG SWEEP PER TIER ==========
    print(f"\n{'#'*70}")
    print(f"  2025 SEASON — CONFIG SWEEP: 6 configs × 3 tiers × {len(seeds)} seeds")
    print(f"  {n_days} days per seed")
    print(f"{'#'*70}")

    tier_labels = {1: "TIER 1 — Chalk Locks", 2: "TIER 2 — Mid-Range", 3: "TIER 3 — Longshots"}
    all_tier_results = {}
    best_configs = {}

    for tier in [1, 2, 3]:
        print(f"\n  Running {tier_labels[tier]}...")
        results = sweep_tier(tier, n_days, seeds)
        all_tier_results[tier] = results
        best_configs[tier] = results[0]["config"]

        print(f"\n  {tier_labels[tier]}")
        print(f"  {'Config':<16} {'Hit%':>7} {'±':>6} {'Avg/Day':>8} {'0/8':>6} "
              f"{'≥1':>6} {'≥2':>6} {'≥3':>6} {'8/8':>6} {'DayStd':>7}")
        print(f"  {'-'*82}")
        for i, r in enumerate(results):
            star = " ★" if i == 0 else ""
            print(f"  {r['config']:<16} {r['mean_hit_rate']:>6.1f}% {r['std_hit_rate']:>5.2f} "
                  f"{r['mean_avg_hits']:>7.2f} {r['mean_zero_rate']:>5.1f}% "
                  f"{r['mean_gte1']:>5.1f}% {r['mean_gte2']:>5.1f}% "
                  f"{r['mean_gte3']:>5.1f}% {r['mean_all8']:>5.1f}% "
                  f"{r['mean_daily_std']:>6.2f}{star}")

    print(f"\n{'='*60}")
    print(f"  2025 BEST CONFIG PER TIER:")
    for tier in [1, 2, 3]:
        r = all_tier_results[tier][0]
        print(f"  Tier {tier}: {r['config']:<16} ({r['mean_hit_rate']:.1f}% hit rate)")
    print(f"{'='*60}")

    # ========== PHASE 2: BLENDED SIMS ==========
    print(f"\n{'#'*70}")
    print(f"  PHASE 2: BLENDED CARD — UNIFORM vs OPTIMIZED")
    print(f"{'#'*70}")

    uniform = {1: "power_heavy", 2: "power_heavy", 3: "power_heavy"}

    print("\n  UNIFORM (power_heavy everywhere)")
    uni_results = []
    for combo in BLEND_COMBOS:
        r = blend_backtest(combo, uniform, n_days, seeds)
        uni_results.append(r)
        print(f"  {r['combo']}: {r['mean_avg_points']:>6.1f} pts | {r['mean_hit_rate']:>5.1f}% | "
              f"0/8: {r['mean_zero_rate']:>4.1f}% | AllTier: {r['mean_all_tiers_covered']:>5.1f}%")

    print(f"\n  OPTIMIZED (T1={best_configs[1]}, T2={best_configs[2]}, T3={best_configs[3]})")
    opt_results = []
    for combo in BLEND_COMBOS:
        r = blend_backtest(combo, best_configs, n_days, seeds)
        opt_results.append(r)
        print(f"  {r['combo']}: {r['mean_avg_points']:>6.1f} pts | {r['mean_hit_rate']:>5.1f}% | "
              f"0/8: {r['mean_zero_rate']:>4.1f}% | AllTier: {r['mean_all_tiers_covered']:>5.1f}%")

    # ========== COMPARISON TABLE ==========
    print(f"\n{'='*95}")
    print(f"  2025 SIDE-BY-SIDE: UNIFORM vs OPTIMIZED")
    print(f"  Optimal: T1={best_configs[1]}, T2={best_configs[2]}, T3={best_configs[3]}")
    print(f"{'='*95}")
    print()
    print(f"  {'Combo':<10} │ {'UNIFORM':^30} │ {'OPTIMIZED':^30} │ {'Δ Pts':>6} {'Δ AllT':>7}")
    print(f"  {'':10} │ {'Pts':>7} {'Hit%':>7} {'0/8':>6} {'AllT%':>7} │ "
          f"{'Pts':>7} {'Hit%':>7} {'0/8':>6} {'AllT%':>7} │")
    print(f"  {'-'*92}")

    for u, o in zip(uni_results, opt_results):
        dp = o['mean_avg_points'] - u['mean_avg_points']
        da = o['mean_all_tiers_covered'] - u['mean_all_tiers_covered']
        star = " ★" if dp > 0.05 else ""
        print(f"  {u['combo']:<10} │ {u['mean_avg_points']:>6.1f} {u['mean_hit_rate']:>6.1f}% "
              f"{u['mean_zero_rate']:>5.1f}% {u['mean_all_tiers_covered']:>6.1f}% │ "
              f"{o['mean_avg_points']:>6.1f} {o['mean_hit_rate']:>6.1f}% "
              f"{o['mean_zero_rate']:>5.1f}% {o['mean_all_tiers_covered']:>6.1f}% │ "
              f"{dp:>+5.1f} {da:>+6.1f}%{star}")

    # Save
    output = {
        "season": 2025,
        "best_configs": best_configs,
        "tier_sweep": {
            str(t): [{k: v for k, v in r.items()} for r in results]
            for t, results in all_tier_results.items()
        },
        "uniform_blend": uni_results,
        "optimized_blend": opt_results,
    }
    out_path = str(RESULTS_DIR / "config_sweep_2025.json")
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  Results saved to {out_path}")


if __name__ == "__main__":
    main()