#!/usr/bin/env python3
"""
score_batters.py — Composite 0-100 scoring engine for Daily HR Bet.

Scores each batter across 5 factors (power, matchup, park, form, weather)
and produces a ranked list. Supports multiple weight configurations.

Usage:
    python score_batters.py --date 2024-07-15
    python score_batters.py --date 2024-07-15 --config power_heavy
"""

import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Weight Configurations
# ---------------------------------------------------------------------------

WEIGHT_CONFIGS = {
    # Diagnostic-tuned default — weights reflect measured factor correlations
    # (form +0.248, power +0.239, matchup +0.145, weather +0.052, park +0.021)
    # Lineup is new: batting order position = AB opportunity.
    "default":       {"power": 0.25, "matchup": 0.20, "park": 0.08, "form": 0.25, "weather": 0.07, "lineup": 0.15},
    "matchup_heavy": {"power": 0.20, "matchup": 0.30, "park": 0.07, "form": 0.20, "weather": 0.05, "lineup": 0.18},
    "power_heavy":   {"power": 0.35, "matchup": 0.18, "park": 0.07, "form": 0.18, "weather": 0.05, "lineup": 0.17},
    "form_heavy":    {"power": 0.20, "matchup": 0.18, "park": 0.07, "form": 0.35, "weather": 0.05, "lineup": 0.15},
    "no_weather":    {"power": 0.27, "matchup": 0.22, "park": 0.08, "form": 0.27, "weather": 0.00, "lineup": 0.16},
}

# Legacy compat: configs without "lineup" key get it defaulted to 0
for _cfg in WEIGHT_CONFIGS.values():
    _cfg.setdefault("lineup", 0.0)


# ---------------------------------------------------------------------------
# Park orientation data — compass bearing from home plate to center field.
# Used to compute whether wind is blowing out, in, or crosswind.
# Bearings are approximate degrees from true north.
# ---------------------------------------------------------------------------

PARK_CF_BEARING = {
    "Angel Stadium":                50,
    "Busch Stadium":                180,
    "Chase Field":                  15,
    "Citi Field":                   112,
    "Citizens Bank Park":           68,
    "Comerica Park":                110,
    "Coors Field":                  68,
    "Dodger Stadium":               0,
    "Fenway Park":                  68,
    "Globe Life Field":             18,
    "Great American Ball Park":     0,
    "Guaranteed Rate Field":        335,
    "Kauffman Stadium":             10,
    "loanDepot park":               0,
    "Minute Maid Park":             352,
    "Nationals Park":               340,
    "Oakland Coliseum":             308,
    "Oracle Park":                  225,
    "Oriole Park at Camden Yards":  352,
    "Petco Park":                   340,
    "PNC Park":                     45,
    "Progressive Field":            10,
    "Rogers Centre":                0,
    "T-Mobile Park":                0,
    "Target Field":                 345,
    "Tropicana Field":              0,    # dome — bearing doesn't matter
    "Truist Park":                  2,
    "Wrigley Field":                68,
    "Yankee Stadium":               18,
    "American Family Field":        0,    # retractable — bearing rarely matters
}


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def min_max_scale(value: float, min_val: float, max_val: float) -> float:
    """Scale a value to 0-100 range."""
    if max_val == min_val:
        return 50.0
    return max(0, min(100, (value - min_val) / (max_val - min_val) * 100))


def percentile_rank(value: float, values: list) -> float:
    """Return percentile rank (0-100) of value within a list."""
    if not values:
        return 50.0
    arr = np.array(values)
    return float(np.searchsorted(np.sort(arr), value) / len(arr) * 100)


# ---------------------------------------------------------------------------
# Factor scoring functions
# ---------------------------------------------------------------------------

def score_power(batter: dict) -> float:
    """
    Factor 1: Power Profile (barrel%, exit velo, HR/FB, ISO as xHR proxy).
    Returns 0-100.
    """
    scores = []

    # Barrel%: 0-25% → 0-100
    barrel = batter.get("barrel_pct", 0)
    if barrel is not None:
        scores.append(min_max_scale(barrel, 0, 25))

    # Exit Velocity: 80-100 mph → 0-100
    ev = batter.get("exit_velo", 0)
    if ev and ev > 0:
        scores.append(min_max_scale(ev, 80, 100))

    # HR/FB%: 0-30% → 0-100
    hr_fb = batter.get("hr_fb_pct", 0)
    if hr_fb is not None:
        # Handle both decimal (0.15) and percentage (15.0) formats
        if hr_fb < 1:
            hr_fb *= 100
        scores.append(min_max_scale(hr_fb, 0, 30))

    # ISO as power proxy: .100-.350 → 0-100
    iso = batter.get("iso", 0)
    if iso and iso > 0:
        scores.append(min_max_scale(iso, 0.100, 0.350))

    return float(np.mean(scores)) if scores else 50.0


def score_matchup(batter: dict, pitcher: dict) -> float:
    """
    Factor 2: Matchup quality (pitcher HR/9, hard-hit% allowed, batter vs hand).
    Returns 0-100.
    """
    scores = []

    # Pitcher HR/9: 0-3.0 → 0-100 (higher = better for batter)
    hr9 = pitcher.get("hr_per_9", 1.2)
    if hr9 is not None:
        scores.append(min_max_scale(hr9, 0, 3.0))

    # Pitcher hard-hit% allowed: 25-50% → 0-100
    hh = pitcher.get("hard_hit_pct_allowed", 35)
    if hh is not None:
        scores.append(min_max_scale(hh, 25, 50))

    # Batter wOBA vs hand: .250-.450 → 0-100
    woba = batter.get("woba_vs_hand", batter.get("woba", 0.320))
    if woba:
        scores.append(min_max_scale(woba, 0.250, 0.450))

    # Platoon advantage bonus: +10 if batter has platoon advantage
    batter_hand = batter.get("bats", "R")
    pitcher_hand = pitcher.get("throws", "R")
    platoon_bonus = 10 if batter_hand != pitcher_hand else 0

    base = float(np.mean(scores)) if scores else 50.0
    return min(100, base + platoon_bonus)


def score_park(batter: dict, venue: str, park_factors: pd.DataFrame) -> float:
    """
    Factor 3: Park Factor for HRs.
    Returns 0-100.

    Uses split L/R factors (`hr_pf_lhb` / `hr_pf_rhb`) if they're in the
    DataFrame; falls back to the legacy `hr_park_factor` column if not,
    which preserves backward compatibility with older DataFrames.

    Switch-hitters (bats == 'S') are scored as a 50/50 average of LHB and
    RHB factors — most switch-hitters face RHP more often but over a
    season that balances out in the aggregate.
    """
    pf = 100.0  # neutral default

    if not park_factors.empty and venue:
        match = park_factors[park_factors["venue"] == venue]
        if not match.empty:
            row = match.iloc[0]
            bats = batter.get("bats", "R") or "R"

            # Prefer split factors if the columns exist
            if "hr_pf_lhb" in row.index and "hr_pf_rhb" in row.index:
                lhb = float(row["hr_pf_lhb"])
                rhb = float(row["hr_pf_rhb"])
                if bats == "L":
                    pf = lhb
                elif bats == "R":
                    pf = rhb
                elif bats == "S":
                    pf = (lhb + rhb) / 2.0
                else:
                    pf = float(row.get("hr_pf_overall", (lhb + rhb) / 2.0))
            else:
                # Legacy path: only overall factor available
                pf = float(row.get("hr_park_factor", row.get("hr_pf_overall", 100.0)))

    # Scale: 70-130 → 0-100
    return min_max_scale(pf, 70, 130)


def score_form(batter: dict) -> float:
    """
    Factor 4: Recent form (last 14 days).
    Returns 0-100.
    """
    scores = []

    # HRs in last 14 days: 0-5+ → 0-100
    recent_hr = batter.get("recent_hr_14d", 0)
    scores.append(min_max_scale(recent_hr, 0, 5))

    # Recent barrel%: 0-25% → 0-100
    recent_barrel = batter.get("recent_barrel_pct_14d", 0)
    if recent_barrel is not None:
        scores.append(min_max_scale(recent_barrel, 0, 25))

    # Exit velo trend (recent - season avg): -5 to +5 → 0-100
    ev_trend = batter.get("ev_trend_14d", 0)
    scores.append(min_max_scale(ev_trend, -5, 5))

    return float(np.mean(scores)) if scores else 50.0


def score_lineup_position(batting_order) -> float:
    """
    Factor 6: Lineup position (AB opportunity).
    Returns 0-100.

    Based on real MLB AB-per-game averages by batting order position:
      1: 4.7 AB/G   2: 4.6   3: 4.5   4: 4.4   5: 4.2
      6: 4.0   7: 3.7   8: 3.4   9: 3.2
      Bench: ~0.4 expected AB (pinch-hit or DNP)

    Confirmed starters get positioned scores; bench/roster-only bats get
    a heavy penalty since they may not play at all.
    """
    SCORES = {
        1: 85,    # leadoff — most ABs
        2: 82,    # 2-hole — high OBP + power in modern MLB
        3: 78,    # 3-hole — traditional best hitter
        4: 75,    # cleanup — power spot
        5: 65,    # 5-hole — solid but fewer ABs
        6: 58,    # bottom third starts here
        7: 48,    # significantly fewer ABs
        8: 42,    # low leverage
        9: 38,    # fewest regular ABs
    }

    if batting_order is None:
        return 35.0          # unknown — assume worst starter
    if isinstance(batting_order, int) and 1 <= batting_order <= 9:
        return float(SCORES[batting_order])
    if str(batting_order) in ("bench", "roster_only"):
        return 15.0          # may not play at all
    return 35.0


def score_temperature(temp_f: float) -> float:
    """
    Piecewise temperature → 0-100 score.

    Gentler curve than v1 — compressed range so temperature doesn't
    dominate the weather factor. The real effect of temperature on HR
    rate is ~1-2% per 10°F, which is meaningful but modest.

    Anchor points (gentler than v1):
        40°F →  25   (cold, suppressed — was 10)
        50°F →  35   (cool — was 25)
        60°F →  44   (playable)
        68°F →  50   (league-neutral)
        75°F →  55   (warm, mild boost)
        85°F →  63   (hot, moderate boost — was 72)
        95°F →  72   (very hot — was 88)
       100°F+ → 78   (capped — was 95)
    """
    anchors = [
        (40, 25),
        (50, 35),
        (60, 44),
        (68, 50),
        (75, 55),
        (85, 63),
        (95, 72),
        (100, 78),
    ]
    if temp_f <= anchors[0][0]:
        return float(anchors[0][1])
    if temp_f >= anchors[-1][0]:
        return float(anchors[-1][1])
    for (t0, s0), (t1, s1) in zip(anchors, anchors[1:]):
        if t0 <= temp_f <= t1:
            frac = (temp_f - t0) / (t1 - t0)
            return s0 + frac * (s1 - s0)
    return 50.0


def _angular_diff(a: float, b: float) -> float:
    """Smallest signed angle between two bearings (degrees). Result in -180..180."""
    d = (b - a) % 360
    return d if d <= 180 else d - 360


def score_wind(
    wind_mph: float,
    wind_dir_from: float | None,
    venue: str,
    batter_hand: str = "R",
) -> float:
    """
    Wind scoring relative to park orientation and batter handedness.

    wind_dir_from: compass direction the wind is COMING FROM (meteorological).
    venue: canonical venue name → looks up CF bearing from PARK_CF_BEARING.
    batter_hand: "L", "R", or "S" (switch).

    Logic:
      - Compute wind_to = (wind_from + 180) % 360  (direction wind blows TOWARD)
      - Park has a CF bearing. RF ≈ CF+45°, LF ≈ CF-45°.
      - LHB pull to RF → wind blowing toward RF helps them
      - RHB pull to LF → wind blowing toward LF helps them
      - Cosine similarity between wind_to and the relevant field sector
        gives a -1..+1 alignment score.
      - Scale by wind speed (calm wind = neutral regardless of direction).
    """
    if wind_dir_from is None or wind_mph < 2:
        return 50.0  # calm or unknown → neutral

    cf_bearing = PARK_CF_BEARING.get(venue, 0)
    wind_to = (wind_dir_from + 180) % 360

    # Determine target bearing based on handedness
    #   LHB pulls to RF → RF_BEARING ≈ CF + 45
    #   RHB pulls to LF → LF_BEARING ≈ CF - 45
    #   Switch → average of both
    if batter_hand == "L":
        target = (cf_bearing + 45) % 360
    elif batter_hand == "R":
        target = (cf_bearing - 45) % 360
    else:
        # Switch: average alignment to both RF and LF
        rf_target = (cf_bearing + 45) % 360
        lf_target = (cf_bearing - 45) % 360
        rf_align = np.cos(np.radians(_angular_diff(wind_to, rf_target)))
        lf_align = np.cos(np.radians(_angular_diff(wind_to, lf_target)))
        alignment = (rf_align + lf_align) / 2
        # Scale: alignment -1..+1 → 0..100, modulated by wind speed
        speed_factor = min(1.0, wind_mph / 15)  # caps at 15 mph
        return 50 + alignment * 25 * speed_factor

    # Cosine of angle between wind direction and target sector
    angle_diff = _angular_diff(wind_to, target)
    alignment = np.cos(np.radians(angle_diff))  # -1 (blowing in) to +1 (blowing out)

    # Scale by wind speed — stronger wind = bigger effect
    # Cap at 15 mph (beyond that, diminishing returns)
    speed_factor = min(1.0, wind_mph / 15)

    # alignment * speed_factor gives -1..+1, map to 0..100 centered on 50
    return 50 + alignment * 25 * speed_factor


def score_humidity(humidity_pct: float | None) -> float:
    """
    Humidity → 0-100 score.

    Humid air is LESS dense than dry air (water vapor MW=18 vs N2=28, O2=32),
    so baseballs carry farther in humid conditions. The effect is real but
    small: ~1-2% on ball carry across the full 0-100% humidity range.

    We give a mild linear boost: 20% RH → 42, 50% → 50, 80% → 58, 100% → 65.
    """
    if humidity_pct is None:
        return 50.0
    # Clamp 0-100
    h = max(0, min(100, humidity_pct))
    # Linear: 0% → 35, 50% → 50, 100% → 65
    return 35 + h * 0.30


def score_weather(
    weather: dict,
    venue: str = "",
    batter_hand: str = "R",
) -> float:
    """
    Factor 5: Weather conditions (temperature + wind + humidity).
    Returns 0-100.

    Now uses park-relative wind direction and batter handedness.
    """
    if weather.get("dome", False):
        return 50.0  # Neutral for dome stadiums

    temp = weather.get("temperature_f", 68)
    temp_score = score_temperature(temp)

    wind_mph = weather.get("wind_mph", 5) or 0
    wind_dir = weather.get("wind_direction_deg", None)
    wind_score = score_wind(wind_mph, wind_dir, venue, batter_hand)

    humidity = weather.get("humidity_pct", None)
    humidity_score = score_humidity(humidity)

    # Weighted blend: temp is most impactful, wind next, humidity mild
    return temp_score * 0.45 + wind_score * 0.35 + humidity_score * 0.20


# ---------------------------------------------------------------------------
# Composite scoring
# ---------------------------------------------------------------------------

def compute_composite(
    batter: dict,
    pitcher: dict,
    venue: str,
    weather: dict,
    park_factors: pd.DataFrame,
    config_name: str = "default",
    victim_profile: dict | None = None,
    pitcher_profile: dict | None = None,
    batting_order: int | str | None = None,
) -> dict:
    """
    Compute composite score for a single batter.
    Returns dict with factor scores and weighted composite.

    If *victim_profile* and *pitcher_profile* are provided, uses the
    two-signal matchup v2 scoring (archetype similarity + vulnerability).
    Otherwise falls back to the original score_matchup().

    *batting_order* — int 1-9 for confirmed starters, "bench"/"roster_only"
    for non-starters, or None if unknown.
    """
    weights = WEIGHT_CONFIGS[config_name]

    power = score_power(batter)

    # Use v2 matchup scoring if archetype profiles are available
    if victim_profile is not None and pitcher_profile is not None:
        from pitcher_profile import score_matchup_v2
        matchup = score_matchup_v2(batter, pitcher, victim_profile, pitcher_profile)
        matchup_version = "v2"
    else:
        matchup = score_matchup(batter, pitcher)
        matchup_version = "v1"

    park = score_park(batter, venue, park_factors)
    form = score_form(batter)

    batter_hand = batter.get("bats", "R") or "R"
    weather_score = score_weather(weather, venue=venue, batter_hand=batter_hand)

    lineup = score_lineup_position(batting_order)

    composite = (
        weights["power"] * power
        + weights["matchup"] * matchup
        + weights["park"] * park
        + weights["form"] * form
        + weights["weather"] * weather_score
        + weights.get("lineup", 0) * lineup
    )

    return {
        "name": batter.get("name", "Unknown"),
        "team": batter.get("team", ""),
        "bats": batter_hand,
        "venue": venue,
        "power_score": round(power, 1),
        "matchup_score": round(matchup, 1),
        "park_score": round(park, 1),
        "form_score": round(form, 1),
        "weather_score": round(weather_score, 1),
        "lineup_score": round(lineup, 1),
        "batting_order": batting_order,
        "composite": round(composite, 1),
        "config": config_name,
        "matchup_version": matchup_version,
    }


def score_all_batters(
    batters: list[dict],
    pitchers: dict,  # keyed by game_pk or team
    games: list[dict],
    weather_data: dict,
    park_factors: pd.DataFrame,
    config_name: str = "default",
) -> list[dict]:
    """
    Score all batters for a given day.
    Returns sorted list (highest composite first).
    """
    results = []
    for batter in batters:
        game_pk = batter.get("game_pk")
        venue = batter.get("venue", "")
        pitcher = pitchers.get(batter.get("opposing_pitcher_id"), {})
        weather = weather_data.get(game_pk, {})

        score = compute_composite(batter, pitcher, venue, weather, park_factors, config_name)
        score["game_pk"] = game_pk
        score["player_id"] = batter.get("player_id")
        results.append(score)

    # Sort by composite score descending
    results.sort(key=lambda x: x["composite"], reverse=True)
    return results


def select_top_picks(scored: list[dict], n: int = 8, max_per_game: int = 2, min_score: float = 0) -> list[dict]:
    """
    Select top N picks with diversification constraint.
    Max 2 batters from same game.
    """
    picks = []
    game_counts = {}
    seen_names = set()

    for batter in scored:
        if len(picks) >= n:
            break
        if batter["composite"] < min_score:
            continue

        name = batter.get("name", "")
        if name in seen_names:
            continue

        gp = batter.get("game_pk")
        if game_counts.get(gp, 0) >= max_per_game:
            continue

        picks.append(batter)
        seen_names.add(name)
        game_counts[gp] = game_counts.get(gp, 0) + 1

    return picks


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Score batters for HR parlay")
    parser.add_argument("--date", required=True, help="Date (YYYY-MM-DD)")
    parser.add_argument("--config", default="default", choices=WEIGHT_CONFIGS.keys())
    parser.add_argument("--top", type=int, default=8, help="Number of picks")
    parser.add_argument("--output", default=None, help="Output JSON path")
    args = parser.parse_args()

    data_dir = Path(__file__).parent.parent / "data"
    data_file = data_dir / f"daily_{args.date}.json"

    if not data_file.exists():
        print(f"No data file found for {args.date}. Run fetch_daily_data.py first.")
        sys.exit(1)

    with open(data_file) as f:
        data = json.load(f)

    print(f"Scoring batters for {args.date} with config '{args.config}'")
    # In live mode, this would use the full fetched data
    # For now, print summary
    print(f"Games: {len(data['games'])}")
    print(f"Config weights: {WEIGHT_CONFIGS[args.config]}")


if __name__ == "__main__":
    main()