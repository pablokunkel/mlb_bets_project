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
    # Default — learned via logistic regression on enriched 20-day backfill
    # (2026-03-27 -> 2026-04-15, 5,196 batter-game rows, hit_hr as target,
    # 7 features: 5 original + xwoba_contact + fb_pct_allowed).
    # Standardized coefficients: form 0.496, matchup 0.468, power 0.346,
    # weather 0.102, xwoba 0.097, fb_pct_allowed -0.013, park -0.011 (drop).
    # Power bucket includes xwoba_contact + pull_fb_pct sub-scores.
    # Matchup bucket includes fb_pct_allowed + Vegas implied_total when avail.
    # Backtested top-8 hit rate progression: 36.04% (legacy) -> 38.75% (v1
    # learned + percentile rerank) -> 40.00% (this config, v2 enriched).
    "default":       {"power": 0.250, "matchup": 0.264, "park": 0.000, "form": 0.279, "weather": 0.057, "lineup": 0.150},
    # v1 weights — kept for ablation comparison.
    "v1_learned":    {"power": 0.217, "matchup": 0.270, "park": 0.000, "form": 0.304, "weather": 0.060, "lineup": 0.150},
    # Legacy fixed-anchor default (kept for ablation comparison).
    "legacy":        {"power": 0.25,  "matchup": 0.20,  "park": 0.08,  "form": 0.25,  "weather": 0.07,  "lineup": 0.15},
    "matchup_heavy": {"power": 0.20,  "matchup": 0.30,  "park": 0.00,  "form": 0.27,  "weather": 0.05,  "lineup": 0.18},
    "power_heavy":   {"power": 0.35,  "matchup": 0.20,  "park": 0.00,  "form": 0.23,  "weather": 0.05,  "lineup": 0.17},
    "form_heavy":    {"power": 0.20,  "matchup": 0.20,  "park": 0.00,  "form": 0.40,  "weather": 0.05,  "lineup": 0.15},
    "no_weather":    {"power": 0.23,  "matchup": 0.29,  "park": 0.00,  "form": 0.32,  "weather": 0.00,  "lineup": 0.16},
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
    "Tropicana Field":              0,    # dome
    "Truist Park":                  2,
    "Wrigley Field":                68,
    "Yankee Stadium":               18,
    "American Family Field":        0,    # retractable
    "Sutter Health Park":           340,  # Sacramento A's temp home (2025-26); CF roughly NNW — verify
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


def percentile_rank_dict(values_by_key: dict) -> dict:
    """
    Convert {key: raw_value} -> {key: percentile_rank_0_to_100}.

    Used by compute_slate_context to spread within-slate values across the
    full 0-100 range so park/weather/matchup don't compress into a thin band.

    Ties get the average rank (mid-rank), so two venues with identical
    park factors both end up at 50 rather than splitting 25/75.

    If only one key is present, returns 50 (neutral) for that key.
    """
    if not values_by_key:
        return {}
    keys = list(values_by_key.keys())
    vals = [values_by_key[k] for k in keys]
    n = len(vals)
    if n == 1:
        return {keys[0]: 50.0}

    sorted_vals = sorted(vals)
    out = {}
    for k, v in values_by_key.items():
        # Mid-rank: average of (count_less, count_less_or_equal)
        lt = sum(1 for x in sorted_vals if x < v)
        lte = sum(1 for x in sorted_vals if x <= v)
        mid_rank = (lt + lte) / 2.0
        # Map to 0-100; mid_rank ranges 0.5..n-0.5, scale to 0..100
        out[k] = (mid_rank / n) * 100.0
    return out


# ---------------------------------------------------------------------------
# Slate context — pre-computed within-slate percentile rankings
# ---------------------------------------------------------------------------

def compute_slate_context(
    games: list,
    weather_by_gpk: dict,
    pitcher_stats_by_name: dict,
    park_factors: pd.DataFrame,
    implied_totals_by_team: dict | None = None,
) -> dict:
    """
    Pre-compute within-slate percentile rankings for park, weather,
    pitcher vulnerability, and (optionally) Vegas implied team totals.

    Called once per day in score_live_slate; the returned context is then
    passed into compute_composite so each batter's park/weather/vulnerability/
    game-environment score reflects rank-within-today rather than absolute
    fixed-anchor scaling.

    Returns:
        {
            "park_pct":      {venue:        0-100},
            "weather_pct":   {game_pk:      0-100},
            "pitcher_pct":   {pitcher_name: 0-100},
            "team_total_pct":{team_abbrev:  0-100},   # may be empty
            "active": True
        }
    """
    # Park: percentile rank of overall HR park factor
    venue_pf = {}
    if park_factors is not None and not park_factors.empty:
        for g in games:
            venue = g.get("venue", "")
            if not venue or venue in venue_pf:
                continue
            match = park_factors[park_factors["venue"] == venue]
            if match.empty:
                continue
            row = match.iloc[0]
            if "hr_pf_overall" in row.index:
                venue_pf[venue] = float(row["hr_pf_overall"])
            elif "hr_park_factor" in row.index:
                venue_pf[venue] = float(row["hr_park_factor"])
            else:
                lhb = float(row.get("hr_pf_lhb", 100))
                rhb = float(row.get("hr_pf_rhb", 100))
                venue_pf[venue] = (lhb + rhb) / 2.0
    park_pct = percentile_rank_dict(venue_pf)

    # Weather: base quality per game (temp + wind speed + humidity proxy).
    # Direction/handedness is per-batter, so we percentile the base
    # "is this a HR-conducive ballpark conditions day?" only.
    #
    # 2026-05-02 fix: was using `or 70`/`or 0`/`or 50` league-mean fill on
    # missing components. Two problems with the old version: (a) `or 0`
    # on wind triggered on real calm-day readings (0 mph is a valid value),
    # (b) silent fill made it impossible to tell a Missing-data game from
    # a real-league-average game. Now we require all 3 components — Open-
    # Meteo returns all three for non-dome venues, so missing values here
    # almost always mean the fetch hit the etl_morning fallback path
    # (temp=68, wind=5, dome=0) and that path correctly populates them.
    game_weather_q = {}
    for gpk, w in (weather_by_gpk or {}).items():
        if not w:
            continue
        if w.get("dome", False):
            game_weather_q[gpk] = 50.0
            continue
        temp = w.get("temperature_f")
        wind = w.get("wind_mph")
        humidity = w.get("humidity_pct")
        if temp is None or wind is None or humidity is None:
            # Skip — game falls through to neutral in score_weather.
            # Don't pollute the percentile rank with a partial signal.
            continue
        game_weather_q[gpk] = temp + wind * 0.5 + humidity * 0.05
    weather_pct = percentile_rank_dict(game_weather_q)

    # Pitcher vulnerability: HR/9-driven raw vulnerability, no caps.
    # Now includes fly-ball% allowed: FB pitchers give up more HRs at the
    # same HR/9 (deeper carry on fly balls means more leave the yard).
    # League-average FB% allowed ~35; elite GB pitchers ~25; FB pitchers ~45.
    #
    # 2026-05-02 fix (audit HIGH #3): was using `or 1.2`/`or 4.0`/`or 35`
    # league-mean fill. That made a pitcher with a partial MLB-API response
    # (everything fetched but fb_pct_allowed missing) score as if that
    # signal had been measured at league mean. Worse, the `or X` after
    # `.get(..., X)` re-applied the default whenever the value was 0
    # (truthy-tested), so a real GB pitcher with low FB% got bumped up to
    # league avg. There was no provenance flag distinguishing measured 1.2
    # HR/9 from a missing-data fallback. Now: skip-on-missing per input;
    # build raw from only what's measured; pitchers with <2 signals are
    # skipped from pitcher_pct entirely so score_matchup falls through to
    # the v1 path (which is also fixed below).
    pitcher_vuln_raw = {}
    for pname, p in (pitcher_stats_by_name or {}).items():
        if not p:
            continue

        components = []
        hr9 = p.get("hr_per_9")
        if hr9 is not None and hr9 > 0:
            components.append(hr9 * 30.0)
        era = p.get("era")
        if era is not None and era > 0:
            components.append(era * 5.0)
        hh = p.get("hard_hit_pct_allowed")
        if hh is not None and hh > 0:
            components.append(hh * 0.6)
        k9 = p.get("k_per_9")
        if k9 is not None and k9 > 0:
            components.append(-k9 * 2.0)
        fb_pct_allowed = p.get("fb_pct_allowed")
        if fb_pct_allowed is not None:
            # FB% is centered at 35 (league avg). Real 0% is impossible
            # for a starter, so None-only check is sufficient here.
            components.append((fb_pct_allowed - 35) * 0.8)

        if len(components) < 2:
            continue   # not enough signal — let v1 fallback handle this pitcher

        raw = sum(components)

        # Low-IP pull-toward-neutral when IP is genuinely measured + low.
        # Use a league baseline computed from the SAME components measured
        # (apples-to-apples) rather than the original all-component baseline.
        ip = p.get("ip")
        if ip is not None and 0 < ip < 10:
            league = 0.0
            if hr9 is not None and hr9 > 0:               league += 1.2 * 30.0
            if era is not None and era > 0:               league += 4.0 * 5.0
            if hh is not None and hh > 0:                 league += 35 * 0.6
            if k9 is not None and k9 > 0:                 league += -8.0 * 2.0
            # fb_pct contribution at league mean = 0 (centered at 35), no add
            raw = (raw + league * 2) / 3.0

        pitcher_vuln_raw[pname] = raw
    pitcher_pct = percentile_rank_dict(pitcher_vuln_raw)

    # Vegas implied team totals: percentile within today's slate.
    # If no totals are provided (no API key, or feed unavailable), this
    # dict is empty and score_matchup falls back to neutral (50) for the
    # game-environment signal.
    team_total_pct = {}
    if implied_totals_by_team:
        team_total_pct = percentile_rank_dict(
            {t: float(v) for t, v in implied_totals_by_team.items() if v is not None}
        )

    return {
        "park_pct": park_pct,
        "weather_pct": weather_pct,
        "pitcher_pct": pitcher_pct,
        "team_total_pct": team_total_pct,
        "active": True,
    }


# ---------------------------------------------------------------------------
# Factor scoring functions
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# League-average pitcher fallback
# ---------------------------------------------------------------------------
# Audit MED fix: was copy-pasted across 4 sites in generate_picks.py and
# 1 in diagnostics/factor_diagnostics.py with no provenance flag — a
# pitcher whose MLB-API call returned an empty stat block was scored
# identically to a real league-average pitcher.
#
# This single dict is the source of truth. Each consumer uses
# `dict(LEAGUE_AVG_PITCHER, name=pname)` to copy + override the name —
# the dict() constructor returns a fresh copy so callers can mutate
# without polluting other uses.
#
# `_source` field lets downstream consumers (refit_weights, dashboard
# diagnostics, etc.) filter out league-mean rows. NOTE: the audit's
# fix for HIGH #3 already made compute_slate_context skip-on-missing
# at the input level — these defaults now only matter when the
# pitcher dict is fed directly to the v1 fallback paths.
#
# Drift watch: 2026 league averages per Savant aggregates (refresh
# annually). Real 2026 HR/9 is closer to 1.27, hard-hit% closer to 39%.
# Current values match what was in the old inline copies for diff
# minimization; bump after a refit cycle when we want to update.
LEAGUE_AVG_PITCHER = {
    "name": "league_avg",
    "hr_per_9": 1.2,
    "era": 4.0,
    "hard_hit_pct_allowed": 35,
    "k_per_9": 8.0,
    "fb_pct_allowed": 35,
    "throws": "R",
    "_source": "league_avg_default",
}


def score_power(batter: dict) -> float:
    """
    Factor 1: Power Profile (barrel%, exit velo, HR/FB, ISO as xHR proxy).
    Returns 0-100.

    Critical: every metric uses `is not None and > 0` — a missing or zero
    value is SKIPPED, not scored as 0. Previously `barrel_pct` and
    `hr_fb_pct` accepted 0, so a player whose live tier estimate
    happened to land at zero (or who was renormalized down to zero in
    fetch_daily_data._splits_to_batters) had their power score dragged
    to ~13 even with elite real Statcast inputs (Buxton 5/1, refit notes).
    """
    scores = []

    barrel = batter.get("barrel_pct")
    if barrel is not None and barrel > 0:
        scores.append(min_max_scale(barrel, 0, 25))

    ev = batter.get("exit_velo")
    if ev is not None and ev > 0:
        scores.append(min_max_scale(ev, 80, 100))

    hr_fb = batter.get("hr_fb_pct")
    if hr_fb is not None and hr_fb > 0:
        if hr_fb < 1:
            hr_fb *= 100
        scores.append(min_max_scale(hr_fb, 0, 30))

    iso = batter.get("iso")
    if iso is not None and iso > 0:
        scores.append(min_max_scale(iso, 0.100, 0.350))

    # xwOBA on contact: .280 (poor contact) -> .500 (elite). One of the
    # most HR-predictive Statcast metrics. Defaults missing.
    xwoba_contact = batter.get("xwoba_contact")
    if xwoba_contact is not None and xwoba_contact > 0:
        scores.append(min_max_scale(xwoba_contact, 0.280, 0.500))

    # Pull-FB%: percentage of contact that is pulled fly balls. HRs come
    # almost exclusively from pulled fly balls. League avg ~12%, elite ~22%.
    pull_fb = batter.get("pull_fb_pct")
    if pull_fb is not None and pull_fb > 0:
        if pull_fb < 1:
            pull_fb *= 100
        scores.append(min_max_scale(pull_fb, 5, 25))

    return float(np.mean(scores)) if scores else 50.0


def score_matchup(
    batter: dict,
    pitcher: dict,
    slate_ctx: dict | None = None,
    batter_team: str | None = None,
) -> float:
    """
    Factor 2: Matchup quality.
    Returns 0-100.

    With slate_ctx active, the pitcher-vulnerability portion is replaced by
    the within-slate percentile rank — fixes the HR/9 cap compression where
    the 5 worst HR-allowing pitchers all clustered at score ~70.

    If slate_ctx contains a non-empty team_total_pct map and *batter_team*
    is provided, the Vegas implied team total percentile is added as a
    third equal-weighted signal (game environment).
    """
    scores = []

    pname = pitcher.get("name", "")
    if slate_ctx and slate_ctx.get("active") and pname in slate_ctx.get("pitcher_pct", {}):
        scores.append(slate_ctx["pitcher_pct"][pname])
    else:
        # v1 fallback. 2026-05-02 fix (audit HIGH #3 + MED): was using
        # `pitcher.get("hr_per_9", 1.2)` and `pitcher.get("hard_hit_pct_allowed", 35)`
        # which silently injected league means whenever the key was missing.
        # The `if hr9 is not None` guard never tripped because the .get
        # default already coerced None to 1.2. Now: drop the .get default
        # so the guard works, and require >0 to also guard against the
        # truthy-test bug (real 0 should also skip, since 0 HR/9 means
        # the pitcher hasn't pitched yet).
        hr9 = pitcher.get("hr_per_9")
        if hr9 is not None and hr9 > 0:
            scores.append(min_max_scale(hr9, 0, 4.5))

        hh = pitcher.get("hard_hit_pct_allowed")
        if hh is not None and hh > 0:
            scores.append(min_max_scale(hh, 25, 50))

    # 2026-05-02 fix (audit HIGH #4): was using
    # `batter.get("woba_vs_hand", batter.get("woba", 0.320))` which silently
    # filled missing-everywhere woba with 0.320 (league mean) and the
    # `if woba:` guard never triggered for that case. Result: a batter
    # with no measured woba would score min_max_scale(0.320, 0.290, 0.395)
    # ≈ 28.6 — i.e., scored as a below-average matchup at league-mean
    # contact, which is the wrong default. Now: skip-on-missing.
    #
    # Anchor history: 2026-05-01 tightened to (0.290, 0.395) from
    # (0.280, 0.420) because woba was SIGNAL_NOT_CAPTURED — empirical HR
    # rate climbed 4.5x across quintiles but the wider range only shifted
    # matchup score 2 points. The 105pt woba range now maps to 100 score,
    # steepening the gradient where the actual HR signal lives.
    woba = batter.get("woba_vs_hand")
    if woba is None:
        woba = batter.get("woba")
    if woba is not None and woba > 0:
        scores.append(min_max_scale(woba, 0.290, 0.395))

    # Vegas implied team total — game environment signal. Bundles park,
    # weather, lineup quality, and opposing pitcher into a market-blessed
    # number. Only applied if slate has the data (silently skipped otherwise).
    if (
        slate_ctx
        and batter_team
        and slate_ctx.get("team_total_pct")
        and batter_team in slate_ctx["team_total_pct"]
    ):
        scores.append(slate_ctx["team_total_pct"][batter_team])

    batter_hand = batter.get("bats", "R")
    pitcher_hand = pitcher.get("throws", "R")
    platoon_bonus = 10 if batter_hand != pitcher_hand else 0

    base = float(np.mean(scores)) if scores else 50.0
    return min(100, base + platoon_bonus)


def score_park(
    batter: dict,
    venue: str,
    park_factors: pd.DataFrame,
    slate_ctx: dict | None = None,
) -> float:
    """
    Factor 3: Park Factor for HRs.
    Returns 0-100.

    With slate_ctx active, returns within-slate percentile rank for this
    venue (with small L/R adjustment). Without slate_ctx, falls back to
    fixed-anchor 70-130 -> 0-100 scaling.

    Note: park_score is currently weighted 0 in the default config (the
    20-day backfit found near-zero predictive coefficient). Function is
    retained so it can be brought back if future seasons show signal.
    """
    if not venue:
        return 50.0

    # Slate-relative path
    if slate_ctx and slate_ctx.get("active") and venue in slate_ctx.get("park_pct", {}):
        base_pct = slate_ctx["park_pct"][venue]
        if park_factors is not None and not park_factors.empty:
            match = park_factors[park_factors["venue"] == venue]
            if not match.empty:
                row = match.iloc[0]
                if "hr_pf_lhb" in row.index and "hr_pf_rhb" in row.index:
                    bats = batter.get("bats", "R") or "R"
                    lhb = float(row["hr_pf_lhb"])
                    rhb = float(row["hr_pf_rhb"])
                    overall = float(row.get("hr_pf_overall", (lhb + rhb) / 2.0))
                    if overall > 0:
                        if bats == "L":
                            adj = (lhb - overall) / overall
                        elif bats == "R":
                            adj = (rhb - overall) / overall
                        else:
                            adj = 0.0
                        base_pct = max(0, min(100, base_pct + adj * 50))
        return base_pct

    # Fallback: fixed-anchor scaling (legacy)
    pf = 100.0
    if park_factors is not None and not park_factors.empty:
        match = park_factors[park_factors["venue"] == venue]
        if not match.empty:
            row = match.iloc[0]
            bats = batter.get("bats", "R") or "R"
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
                pf = float(row.get("hr_park_factor", row.get("hr_pf_overall", 100.0)))

    return min_max_scale(pf, 70, 130)


def score_form(batter: dict) -> float:
    """
    Factor 4: Recent form (last 14 days).
    Returns 0-100.

    None vs 0 distinction:
      - None  → no game-log data available; signal is SKIPPED (not scored 0)
      - 0     → real measurement: player did hit 0 HRs / had no EV trend.
                Scored honestly (low form).
    Previously every metric defaulted to 0 and was always scored, so a
    player without 14d game logs got a low form score even if they had
    a fine season. Now we only score what we actually measured.
    """
    scores = []

    recent_hr = batter.get("recent_hr_14d")
    if recent_hr is not None:
        scores.append(min_max_scale(recent_hr, 0, 5))

    recent_barrel = batter.get("recent_barrel_pct_14d")
    if recent_barrel is not None and recent_barrel > 0:
        scores.append(min_max_scale(recent_barrel, 0, 25))

    ev_trend = batter.get("ev_trend_14d")
    if ev_trend is not None:
        scores.append(min_max_scale(ev_trend, -5, 5))

    return float(np.mean(scores)) if scores else 50.0


def score_lineup_position(batting_order) -> float:
    """
    Factor 6: Lineup position (AB opportunity).
    Returns 0-100.

    Based on real MLB AB-per-game averages by batting order position:
      1: 4.7 AB/G   2: 4.6   3: 4.5   4: 4.4   5: 4.2
      6: 4.0   7: 3.7   8: 3.4   9: 3.2
    """
    SCORES = {
        1: 85, 2: 82, 3: 78, 4: 75, 5: 65,
        6: 58, 7: 48, 8: 42, 9: 38,
    }

    if batting_order is None:
        return 35.0
    if isinstance(batting_order, int) and 1 <= batting_order <= 9:
        return float(SCORES[batting_order])
    if str(batting_order) in ("bench", "roster_only"):
        return 15.0
    return 35.0


def score_temperature(temp_f: float) -> float:
    """
    Piecewise temperature -> 0-100 score.
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
    venue: canonical venue name -> looks up CF bearing from PARK_CF_BEARING.
    batter_hand: "L", "R", or "S" (switch).

    LHB pull to RF; RHB pull to LF; switch averages both.
    """
    if wind_dir_from is None or wind_mph < 2:
        return 50.0

    cf_bearing = PARK_CF_BEARING.get(venue, 0)
    wind_to = (wind_dir_from + 180) % 360

    if batter_hand == "L":
        target = (cf_bearing + 45) % 360
    elif batter_hand == "R":
        target = (cf_bearing - 45) % 360
    else:
        rf_target = (cf_bearing + 45) % 360
        lf_target = (cf_bearing - 45) % 360
        rf_align = np.cos(np.radians(_angular_diff(wind_to, rf_target)))
        lf_align = np.cos(np.radians(_angular_diff(wind_to, lf_target)))
        alignment = (rf_align + lf_align) / 2
        speed_factor = min(1.0, wind_mph / 15)
        return 50 + alignment * 25 * speed_factor

    angle_diff = _angular_diff(wind_to, target)
    alignment = np.cos(np.radians(angle_diff))
    speed_factor = min(1.0, wind_mph / 15)

    return 50 + alignment * 25 * speed_factor


def score_humidity(humidity_pct: float | None) -> float:
    """
    Humidity -> 0-100 score.
    Humid air carries baseballs farther; mild linear boost.
    """
    if humidity_pct is None:
        return 50.0
    h = max(0, min(100, humidity_pct))
    return 35 + h * 0.30


def score_weather(
    weather: dict,
    venue: str = "",
    batter_hand: str = "R",
    slate_ctx: dict | None = None,
    game_pk: int | None = None,
) -> float:
    """
    Factor 5: Weather conditions (temperature + wind + humidity).
    Returns 0-100.

    With slate_ctx active and a game_pk match, blends:
      - 60% within-slate base-quality percentile (temp + speed + humidity)
      - 40% per-batter wind-alignment score (handedness-specific)

    Without slate_ctx, falls back to fixed-anchor blend.
    """
    if weather.get("dome", False):
        return 50.0

    wind_mph = weather.get("wind_mph", 5) or 0
    wind_dir = weather.get("wind_direction_deg", None)
    wind_score = score_wind(wind_mph, wind_dir, venue, batter_hand)

    if (
        slate_ctx
        and slate_ctx.get("active")
        and game_pk is not None
        and game_pk in slate_ctx.get("weather_pct", {})
    ):
        base_pct = slate_ctx["weather_pct"][game_pk]
        return base_pct * 0.60 + wind_score * 0.40

    # Fallback: fixed-anchor blend
    temp = weather.get("temperature_f", 68)
    temp_score = score_temperature(temp)

    humidity = weather.get("humidity_pct", None)
    humidity_score = score_humidity(humidity)

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
    slate_ctx: dict | None = None,
    game_pk: int | None = None,
) -> dict:
    """
    Compute composite score for a single batter.
    Returns dict with factor scores and weighted composite.

    *slate_ctx* — optional pre-computed slate context. When provided,
    park/weather/matchup-vulnerability scoring uses within-slate percentile
    rankings instead of fixed-anchor scaling.
    """
    weights = WEIGHT_CONFIGS[config_name]

    power = score_power(batter)

    batter_team = batter.get("team", "")

    if victim_profile is not None and pitcher_profile is not None:
        from pitcher_profile import score_matchup_v2
        matchup = score_matchup_v2(
            batter, pitcher, victim_profile, pitcher_profile,
            slate_ctx=slate_ctx,
            batter_team=batter_team,
        )
        matchup_version = "v2"
    else:
        matchup = score_matchup(
            batter, pitcher,
            slate_ctx=slate_ctx,
            batter_team=batter_team,
        )
        matchup_version = "v1"

    # Audit HIGH #5: stamp which optional signals were active for this pick.
    # Cross-day stratification matters because availability varies — e.g.,
    # a day without VEGAS_ODDS_API_KEY produces a 2/3-signal blend while a
    # day with it produces 3/4. Backtest_factors / refit_weights training
    # over a mixed window otherwise treats them as the same composite scale.
    #
    # NOTE: this duplicates the availability checks inside score_matchup
    # / score_matchup_v2. If the signal-blend logic in those functions
    # changes, update this list to match. Keeping it here (rather than
    # making the score functions return a tuple) preserves the existing
    # call signatures used by backtest_factors, factor_diagnostics, and
    # the smoke tests.
    matchup_signals_used = ["vuln"]
    if matchup_version == "v2":
        matchup_signals_used.append("sim")
    if (slate_ctx and batter_team
            and slate_ctx.get("team_total_pct")
            and batter_team in slate_ctx["team_total_pct"]):
        matchup_signals_used.append("total")
    woba_for_check = batter.get("woba_vs_hand")
    if woba_for_check is None:
        woba_for_check = batter.get("woba")
    if woba_for_check is not None and woba_for_check > 0:
        matchup_signals_used.append("woba")

    park = score_park(batter, venue, park_factors, slate_ctx=slate_ctx)
    form = score_form(batter)

    batter_hand = batter.get("bats", "R") or "R"
    weather_score = score_weather(
        weather, venue=venue, batter_hand=batter_hand,
        slate_ctx=slate_ctx, game_pk=game_pk,
    )

    lineup = score_lineup_position(batting_order)

    composite = (
        weights["power"] * power
        + weights["matchup"] * matchup
        + weights["park"] * park
        + weights["form"] * form
        + weights["weather"] * weather_score
        + weights.get("lineup", 0) * lineup
    )

    # Snapshot all raw inputs that fed each factor — used by load_picks_to_db
    # to populate pick_inputs so the dashboard's per-factor decomposition
    # charts can compare HR hitters vs misses on each underlying signal.
    pf_overall = None
    if park_factors is not None and not park_factors.empty and venue:
        pf_match = park_factors[park_factors["venue"] == venue]
        if not pf_match.empty:
            row = pf_match.iloc[0]
            pf_overall = float(row.get("hr_pf_overall", row.get("hr_park_factor", 100)))

    archetype_sim = None
    if victim_profile is not None and pitcher_profile is not None:
        try:
            from pitcher_profile import archetype_similarity
            archetype_sim = archetype_similarity(victim_profile, pitcher_profile)
        except Exception:
            archetype_sim = None

    vegas_total = None
    if slate_ctx and batter_team:
        # Note: slate_ctx stores percentile, not raw — but the raw is in
        # the pitcher dict if we ever emit it. For now persist the percentile,
        # which is what's actually feeding the score.
        vegas_total = slate_ctx.get("team_total_pct", {}).get(batter_team)

    inputs_snapshot = {
        # Power
        "barrel_pct":              batter.get("barrel_pct"),
        "exit_velo":               batter.get("exit_velo"),
        "hr_fb_pct":               batter.get("hr_fb_pct"),
        "iso":                     batter.get("iso"),
        "xwoba_contact":           batter.get("xwoba_contact"),
        "pull_fb_pct":             batter.get("pull_fb_pct"),
        # Form
        "recent_hr_14d":           batter.get("recent_hr_14d"),
        "recent_barrel_pct_14d":   batter.get("recent_barrel_pct_14d"),
        "ev_trend_14d":            batter.get("ev_trend_14d"),
        # Matchup — pitcher
        "pitcher_hr_per_9":        pitcher.get("hr_per_9"),
        "pitcher_era":             pitcher.get("era"),
        "pitcher_hh_pct":          pitcher.get("hard_hit_pct_allowed"),
        "pitcher_k_per_9":         pitcher.get("k_per_9"),
        "pitcher_fb_pct_allowed":  pitcher.get("fb_pct_allowed"),
        # Matchup — batter / game
        "woba_vs_hand":            batter.get("woba_vs_hand", batter.get("woba")),
        "archetype_similarity":    archetype_sim,
        "vegas_implied_total":     vegas_total,
        "platoon_advantage":       1 if batter_hand != pitcher.get("throws", "R") else 0,
        # Park
        "hr_park_factor":          pf_overall,
        # Weather
        "temperature_f":           weather.get("temperature_f"),
        "wind_mph":                weather.get("wind_mph"),
        "wind_direction_deg":      weather.get("wind_direction_deg"),
        "humidity_pct":            weather.get("humidity_pct"),
        "is_dome":                 1 if weather.get("dome") else 0,
        # Lineup
        "batting_order":           batting_order if isinstance(batting_order, int) else None,
    }

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
        # Audit HIGH #5: list of which matchup signals were available for
        # this pick. ["vuln"] = pure HR/9-style vulnerability only;
        # adds "sim" (archetype) when v2; "total" when Vegas data live;
        # "woba" when batter's wOBA vs hand is measurable. Backtest /
        # refit can stratify training/eval on this to keep cross-day
        # comparisons honest. Future: persist to pick_inputs as a TEXT
        # column once we want refit_weights to filter on it directly.
        "matchup_signals_used": matchup_signals_used,
        "inputs": inputs_snapshot,
    }


def score_all_batters(batters, pitchers, games, weather_data, park_factors, config_name="default"):
    """Score all batters for a given day. Returns sorted list (highest composite first)."""
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
    results.sort(key=lambda x: x["composite"], reverse=True)
    return results


def select_top_picks(scored, n=8, max_per_game=2, min_score=0):
    """Select top N picks with diversification constraint. Max 2 batters from same game."""
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
    print(f"Games: {len(data['games'])}")
    print(f"Config weights: {WEIGHT_CONFIGS[args.config]}")


if __name__ == "__main__":
    main()
