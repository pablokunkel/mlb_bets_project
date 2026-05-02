#!/usr/bin/env python3
"""
factor_diagnostics.py — Retroactive factor-level analysis for Daily HR Bet.

Pulls real game data from the MLB Stats API for a date range, computes all
5 factor scores for every batter who had at-bats, joins against actual HR
outcomes, and runs per-factor correlation analysis.

This answers: "Which of our 5 scoring factors actually predict home runs?"

Outputs:
  - Per-factor point-biserial correlation with HR (binary)
  - ROC AUC per factor (how well does each score separate HR hitters?)
  - Hit rate by score quartile per factor
  - Composite calibration (do high-composite batters HR more often?)
  - Summary report printed to console + saved as JSON

Usage:
    # Analyze the full 2026 season so far
    python factor_diagnostics.py --start 2026-03-27 --end 2026-04-15

    # Just last week
    python factor_diagnostics.py --start 2026-04-08 --end 2026-04-15

    # Save detailed results
    python factor_diagnostics.py --start 2026-03-27 --end 2026-04-15 --output diagnostics.json

Requirements:
    pip install requests numpy pandas scipy scikit-learn
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from score_batters import (
    score_power, score_matchup, score_park, score_form, score_weather,
    score_temperature, min_max_scale, WEIGHT_CONFIGS,
)
from fetch_daily_data import (
    get_hardcoded_park_factors, DOME_STADIUMS, VENUE_COORDS,
)
from mlb_2025_tiers import get_all_batters_lookup

import requests as req

MLB_API = "https://statsapi.mlb.com/api/v1"

# Open-Meteo historical weather API
OPEN_METEO_HIST = "https://archive-api.open-meteo.com/v1/archive"


# ═══════════════════════════════════════════════════════════════════════════
# 1. DATA COLLECTION — pull real games, lineups, box scores, weather
# ═══════════════════════════════════════════════════════════════════════════

def fetch_schedule(date_str: str) -> list[dict]:
    """Fetch all completed MLB games for a date."""
    url = f"{MLB_API}/schedule"
    params = {"sportId": 1, "date": date_str, "hydrate": "team,venue,probablePitcher,linescore"}
    try:
        resp = req.get(url, params=params, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"    WARN: schedule fetch failed for {date_str}: {e}")
        return []

    games = []
    for d in resp.json().get("dates", []):
        for g in d.get("games", []):
            status = g["status"]["detailedState"]
            if status not in ("Final", "Game Over", "Completed Early"):
                continue

            venue_name = g.get("venue", {}).get("name", "Unknown")

            # Probable pitchers
            home_pitcher = g.get("teams", {}).get("home", {}).get("probablePitcher", {})
            away_pitcher = g.get("teams", {}).get("away", {}).get("probablePitcher", {})

            games.append({
                "game_pk": g["gamePk"],
                "date": date_str,
                "home_team": g["teams"]["home"]["team"]["name"],
                "away_team": g["teams"]["away"]["team"]["name"],
                "venue": venue_name,
                "home_pitcher_id": home_pitcher.get("id"),
                "home_pitcher_name": home_pitcher.get("fullName", "Unknown"),
                "away_pitcher_id": away_pitcher.get("id"),
                "away_pitcher_name": away_pitcher.get("fullName", "Unknown"),
            })
    return games


def fetch_boxscore_batters(game_pk: int, game_info: dict) -> list[dict]:
    """
    Fetch the boxscore and extract every batter who had at-bats.
    Returns list of dicts with batter info + actual HR count.
    """
    try:
        url = f"{MLB_API}/game/{game_pk}/boxscore"
        resp = req.get(url, timeout=15)
        resp.raise_for_status()
        box = resp.json()
    except Exception as e:
        print(f"    WARN: boxscore fetch failed for game {game_pk}: {e}")
        return []

    batters = []
    for side in ["home", "away"]:
        team_data = box.get("teams", {}).get(side, {})
        team_name = game_info[f"{side}_team"]
        opp_team = game_info[f"{'away' if side == 'home' else 'home'}_team"]
        players = team_data.get("players", {})

        # Determine opposing pitcher
        if side == "home":
            opp_pitcher_id = game_info.get("away_pitcher_id")
            opp_pitcher_name = game_info.get("away_pitcher_name", "Unknown")
        else:
            opp_pitcher_id = game_info.get("home_pitcher_id")
            opp_pitcher_name = game_info.get("home_pitcher_name", "Unknown")

        for player_key, player_data in players.items():
            person = player_data.get("person", {})
            stats = player_data.get("stats", {})
            batting = stats.get("batting", {})
            ab = batting.get("atBats", 0)
            if ab == 0:
                continue

            bats = person.get("batSide", {}).get("code", "R")

            batters.append({
                "player_id": person.get("id", 0),
                "name": person.get("fullName", "Unknown"),
                "team": team_name,
                "opp_team": opp_team,
                "bats": bats,
                "side": side,
                "game_pk": game_pk,
                "date": game_info["date"],
                "venue": game_info["venue"],
                "ab": ab,
                "hits": batting.get("hits", 0),
                "hr_count": batting.get("homeRuns", 0),
                "rbi": batting.get("rbi", 0),
                "opp_pitcher_id": opp_pitcher_id,
                "opp_pitcher_name": opp_pitcher_name,
            })
    return batters


def fetch_pitcher_stats(pitcher_id: int, season: int) -> dict:
    """Fetch pitcher season stats from MLB API."""
    if not pitcher_id or pitcher_id < 1000:
        return {}
    try:
        url = f"{MLB_API}/people/{pitcher_id}"
        params = {"hydrate": f"stats(group=[pitching],type=[season],season={season})"}
        resp = req.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {}

    people = data.get("people", [])
    if not people:
        return {}
    person = people[0]
    throws = person.get("pitchHand", {}).get("code", "R")

    stat = {}
    for group in person.get("stats", []):
        splits = group.get("splits", [])
        if splits:
            stat = splits[0].get("stat", {})
            break

    if not stat:
        return {}

    ip = float(stat.get("inningsPitched", "0") or "0")
    hr_allowed = int(stat.get("homeRuns", 0))
    k = int(stat.get("strikeOuts", 0))
    era = float(stat.get("era", "4.50") or "4.50")

    hr_per_9 = (hr_allowed / ip * 9) if ip > 0 else 1.2
    k_per_9 = (k / ip * 9) if ip > 0 else 8.0

    return {
        "name": person.get("fullName", ""),
        "throws": throws,
        "hr_per_9": round(hr_per_9, 2),
        "era": round(era, 2),
        "hard_hit_pct_allowed": 35,  # not available from basic API — use neutral
        "k_per_9": round(k_per_9, 2),
        "ip": ip,
    }


def fetch_batter_season_stats(player_id: int, season: int) -> dict:
    """Fetch batter season stats from MLB API for power scoring."""
    if not player_id or player_id < 1000:
        return {}
    try:
        url = f"{MLB_API}/people/{player_id}"
        params = {"hydrate": f"stats(group=[hitting],type=[season],season={season})"}
        resp = req.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {}

    people = data.get("people", [])
    if not people:
        return {}
    person = people[0]

    stat = {}
    for group in person.get("stats", []):
        splits = group.get("splits", [])
        if splits:
            stat = splits[0].get("stat", {})
            break

    if not stat:
        return {}

    avg = float(stat.get("avg", ".000") or ".000")
    slg = float(stat.get("slg", ".000") or ".000")
    obp = float(stat.get("obp", ".000") or ".000")
    hr = int(stat.get("homeRuns", 0))
    pa = int(stat.get("plateAppearances", 0))

    return {
        "avg": avg,
        "slg": slg,
        "obp": obp,
        "iso": round(slg - avg, 3),
        "hr": hr,
        "pa": pa,
        "hr_per_pa": round(hr / pa, 4) if pa > 0 else 0,
        # These aren't available from basic API — use reasonable defaults
        # that won't bias the power score
        "barrel_pct": None,
        "exit_velo": None,
        "hr_fb_pct": None,
    }


def fetch_game_log_form(player_id: int, season: int) -> dict:
    """Fetch recent game log for form scoring."""
    if not player_id or player_id < 1000:
        return {}
    try:
        url = f"{MLB_API}/people/{player_id}/stats"
        params = {
            "stats": "gameLog",
            "group": "hitting",
            "season": season,
            "gameType": "R",
        }
        resp = req.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {}

    stats_list = data.get("stats", [])
    if not stats_list:
        return {}

    splits = stats_list[0].get("splits", [])
    if not splits:
        return {}

    # Take last 10 games
    recent = splits[-10:]
    recent_hr = sum(int(g.get("stat", {}).get("homeRuns", 0)) for g in recent)
    recent_slg_vals = []
    for g in recent:
        s = g.get("stat", {})
        slg_str = s.get("slg", "0")
        try:
            recent_slg_vals.append(float(slg_str))
        except (ValueError, TypeError):
            pass

    recent_slg = np.mean(recent_slg_vals) if recent_slg_vals else 0.400

    # All-season averages for trend
    all_slg_vals = []
    for g in splits:
        s = g.get("stat", {})
        try:
            all_slg_vals.append(float(s.get("slg", "0")))
        except (ValueError, TypeError):
            pass
    season_slg = np.mean(all_slg_vals) if all_slg_vals else 0.400

    # Approximate form metrics from game logs
    recent_iso_est = max(0, recent_slg - 0.250)
    recent_barrel_est = min(25, recent_iso_est * 100)
    ev_proxy = (recent_slg - season_slg) * 30  # trend

    return {
        "recent_hr_14d": recent_hr,
        "recent_barrel_pct_14d": recent_barrel_est,
        "ev_trend_14d": ev_proxy,
        "n_recent_games": len(recent),
    }


def fetch_historical_weather(venue: str, date_str: str) -> dict:
    """
    Fetch historical weather for a venue on a specific date.
    Uses Open-Meteo archive API.
    """
    coords = VENUE_COORDS.get(venue)
    if not coords:
        # Try partial match
        for v, c in VENUE_COORDS.items():
            if v.lower() in venue.lower() or venue.lower() in v.lower():
                coords = c
                break
    if not coords:
        return {"temperature_f": 72, "wind_mph": 5, "wind_direction_deg": 0, "dome": False}

    # Check dome stadiums
    is_dome = venue in DOME_STADIUMS
    if is_dome:
        return {"temperature_f": 72, "wind_mph": 0, "wind_direction_deg": 0, "dome": True}

    try:
        params = {
            "latitude": coords[0],
            "longitude": coords[1],
            "start_date": date_str,
            "end_date": date_str,
            "hourly": "temperature_2m,wind_speed_10m,wind_direction_10m",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "timezone": "America/New_York",
        }
        resp = req.get(OPEN_METEO_HIST, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        hourly = data.get("hourly", {})
        temps = hourly.get("temperature_2m", [])
        winds = hourly.get("wind_speed_10m", [])
        wind_dirs = hourly.get("wind_direction_10m", [])

        # Use ~7 PM local (index 19) as game-time estimate
        idx = 19 if len(temps) > 19 else len(temps) // 2
        return {
            "temperature_f": temps[idx] if idx < len(temps) else 72,
            "wind_mph": winds[idx] if idx < len(winds) else 5,
            "wind_direction_deg": wind_dirs[idx] if idx < len(wind_dirs) else 0,
            "dome": False,
        }
    except Exception:
        return {"temperature_f": 72, "wind_mph": 5, "wind_direction_deg": 0, "dome": False}


# ═══════════════════════════════════════════════════════════════════════════
# 2. SCORING — compute all 5 factors for each batter
# ═══════════════════════════════════════════════════════════════════════════

def score_batter_for_diagnostics(
    batter_info: dict,
    batter_season: dict,
    pitcher_stats: dict,
    weather: dict,
    park_factors: pd.DataFrame,
) -> dict:
    """
    Compute all 5 factor scores for a batter, returning them individually
    plus the composite. Does NOT use archetype matching (v2) since we can't
    efficiently backfill Statcast data — uses v1 matchup only.
    """
    # Build the batter dict expected by the scorers
    batter = {
        "name": batter_info["name"],
        "team": batter_info["team"],
        "bats": batter_info.get("bats", "R"),
        "barrel_pct": batter_season.get("barrel_pct"),
        "exit_velo": batter_season.get("exit_velo"),
        "hr_fb_pct": batter_season.get("hr_fb_pct"),
        "iso": batter_season.get("iso", 0.150),
        "woba": batter_season.get("woba"),
        "hr": batter_season.get("hr", 0),
        "recent_hr_14d": batter_info.get("recent_hr_14d", 0),
        "recent_barrel_pct_14d": batter_info.get("recent_barrel_pct_14d", 0),
        "ev_trend_14d": batter_info.get("ev_trend_14d", 0),
    }

    pitcher = pitcher_stats if pitcher_stats else {
        "hr_per_9": 1.2, "hard_hit_pct_allowed": 35, "throws": "R", "k_per_9": 8.0,
    }

    venue = batter_info.get("venue", "")

    power = score_power(batter)
    matchup = score_matchup(batter, pitcher)
    park = score_park(batter, venue, park_factors)
    form = score_form(batter)
    weather_sc = score_weather(weather)

    weights = WEIGHT_CONFIGS["default"]
    composite = (
        weights["power"] * power
        + weights["matchup"] * matchup
        + weights["park"] * park
        + weights["form"] * form
        + weights["weather"] * weather_sc
    )

    return {
        "power_score": round(power, 1),
        "matchup_score": round(matchup, 1),
        "park_score": round(park, 1),
        "form_score": round(form, 1),
        "weather_score": round(weather_sc, 1),
        "composite": round(composite, 1),
        "temp_f": weather.get("temperature_f", 72),
        "wind_mph": weather.get("wind_mph", 5),
        "wind_dir": weather.get("wind_direction_deg", 0),
        "dome": weather.get("dome", False),
    }


# ═══════════════════════════════════════════════════════════════════════════
# 3. MAIN COLLECTION LOOP — iterate over dates
# ═══════════════════════════════════════════════════════════════════════════

def collect_diagnostic_data(start_date: str, end_date: str) -> pd.DataFrame:
    """
    For each date in range, fetch games, score all batters, record outcomes.
    Returns a DataFrame with one row per batter-game, including all factor
    scores and whether they hit a HR.
    """
    pf = get_hardcoded_park_factors()
    season = int(start_date[:4])

    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    all_rows = []

    # Cache pitcher stats to avoid redundant API calls
    pitcher_cache = {}
    batter_season_cache = {}
    batter_form_cache = {}

    current = start
    while current <= end:
        date_str = current.strftime("%Y-%m-%d")
        print(f"\n{'='*60}")
        print(f"  Processing {date_str}")
        print(f"{'='*60}")

        games = fetch_schedule(date_str)
        if not games:
            print(f"    No completed games on {date_str}")
            current += timedelta(days=1)
            continue

        print(f"    {len(games)} completed games")

        # Fetch weather for each venue (batch by unique venues)
        venue_weather = {}
        unique_venues = set(g["venue"] for g in games)
        for v in unique_venues:
            if v not in venue_weather:
                venue_weather[v] = fetch_historical_weather(v, date_str)
        time.sleep(0.3)  # Be nice to Open-Meteo

        # Process each game
        for game in games:
            gpk = game["game_pk"]
            batters = fetch_boxscore_batters(gpk, game)
            if not batters:
                continue

            weather = venue_weather.get(game["venue"], {
                "temperature_f": 72, "wind_mph": 5, "wind_direction_deg": 0, "dome": False,
            })

            for b in batters:
                pid = b["player_id"]

                # Fetch/cache pitcher stats
                opp_pid = b.get("opp_pitcher_id")
                if opp_pid and opp_pid not in pitcher_cache:
                    pitcher_cache[opp_pid] = fetch_pitcher_stats(opp_pid, season)
                pitcher_stats = pitcher_cache.get(opp_pid, {})

                # Fetch/cache batter season stats
                if pid not in batter_season_cache:
                    batter_season_cache[pid] = fetch_batter_season_stats(pid, season)
                batter_season = batter_season_cache.get(pid, {})

                # Fetch/cache batter form (game log)
                if pid not in batter_form_cache:
                    batter_form_cache[pid] = fetch_game_log_form(pid, season)
                form_data = batter_form_cache.get(pid, {})

                # Merge form data into batter info
                b_enriched = {**b}
                if form_data:
                    b_enriched["recent_hr_14d"] = form_data.get("recent_hr_14d", 0)
                    b_enriched["recent_barrel_pct_14d"] = form_data.get("recent_barrel_pct_14d", 0)
                    b_enriched["ev_trend_14d"] = form_data.get("ev_trend_14d", 0)

                # Score
                scores = score_batter_for_diagnostics(
                    b_enriched, batter_season, pitcher_stats, weather, pf,
                )

                # Build output row
                row = {
                    "date": date_str,
                    "game_pk": gpk,
                    "player_id": pid,
                    "name": b["name"],
                    "team": b["team"],
                    "bats": b["bats"],
                    "venue": game["venue"],
                    "opp_pitcher": b.get("opp_pitcher_name", ""),
                    "opp_pitcher_id": opp_pid,
                    "ab": b["ab"],
                    "hr_count": b["hr_count"],
                    "hit_hr": 1 if b["hr_count"] > 0 else 0,
                    **scores,
                }
                all_rows.append(row)

            time.sleep(0.15)  # Rate-limit MLB API

        current += timedelta(days=1)

    df = pd.DataFrame(all_rows)
    print(f"\n\n  Collected {len(df)} batter-game observations across "
          f"{df['date'].nunique() if len(df) > 0 else 0} game days")
    return df


# ═══════════════════════════════════════════════════════════════════════════
# 4. ANALYSIS — correlations, AUC, quartile hit rates
# ═══════════════════════════════════════════════════════════════════════════

def analyze_factors(df: pd.DataFrame) -> dict:
    """
    Run full diagnostic analysis on the collected data.
    Returns a dict of results for each factor.
    """
    if df.empty:
        return {"error": "No data collected"}

    from scipy.stats import pointbiserialr
    try:
        from sklearn.metrics import roc_auc_score
        has_sklearn = True
    except ImportError:
        has_sklearn = False
        print("  NOTE: scikit-learn not installed — skipping AUC calculation")

    factors = ["power_score", "matchup_score", "park_score", "form_score", "weather_score", "composite"]
    target = "hit_hr"

    results = {
        "summary": {
            "total_observations": len(df),
            "total_hr": int(df[target].sum()),
            "hr_rate": round(df[target].mean() * 100, 2),
            "unique_dates": int(df["date"].nunique()),
            "unique_batters": int(df["player_id"].nunique()),
            "unique_games": int(df["game_pk"].nunique()),
            "date_range": f"{df['date'].min()} to {df['date'].max()}",
        },
        "factors": {},
    }

    print(f"\n{'='*70}")
    print(f"  FACTOR DIAGNOSTIC REPORT")
    print(f"  {results['summary']['total_observations']} observations | "
          f"{results['summary']['total_hr']} HRs | "
          f"{results['summary']['hr_rate']}% HR rate | "
          f"{results['summary']['unique_dates']} days")
    print(f"{'='*70}")

    for factor in factors:
        if factor not in df.columns:
            continue

        col = df[factor].dropna()
        target_col = df.loc[col.index, target]

        # Point-biserial correlation
        try:
            corr, pval = pointbiserialr(target_col, col)
        except Exception:
            corr, pval = 0.0, 1.0

        # ROC AUC
        auc = None
        if has_sklearn and target_col.nunique() > 1:
            try:
                auc = roc_auc_score(target_col, col)
            except Exception:
                auc = None

        # Quartile analysis
        try:
            df_temp = pd.DataFrame({"score": col, "hr": target_col})
            df_temp["quartile"] = pd.qcut(df_temp["score"], q=4, labels=["Q1 (low)", "Q2", "Q3", "Q4 (high)"], duplicates="drop")
            quartile_stats = df_temp.groupby("quartile", observed=True).agg(
                n=("hr", "count"),
                hrs=("hr", "sum"),
                hr_rate=("hr", "mean"),
                avg_score=("score", "mean"),
            ).to_dict("index")
            # Convert to serializable format
            quartiles = {}
            for q, stats in quartile_stats.items():
                quartiles[str(q)] = {
                    "n": int(stats["n"]),
                    "hrs": int(stats["hrs"]),
                    "hr_rate": round(stats["hr_rate"] * 100, 2),
                    "avg_score": round(stats["avg_score"], 1),
                }
        except Exception:
            quartiles = {}

        # Mean score for HR hitters vs non-HR hitters
        hr_mean = float(col[target_col == 1].mean()) if target_col.sum() > 0 else 0
        no_hr_mean = float(col[target_col == 0].mean()) if (target_col == 0).sum() > 0 else 0
        separation = hr_mean - no_hr_mean

        factor_result = {
            "correlation": round(corr, 4),
            "p_value": round(pval, 6),
            "significant": pval < 0.05,
            "auc": round(auc, 4) if auc is not None else None,
            "mean_hr_hitters": round(hr_mean, 1),
            "mean_non_hr": round(no_hr_mean, 1),
            "separation": round(separation, 1),
            "quartiles": quartiles,
            "score_range": [round(float(col.min()), 1), round(float(col.max()), 1)],
            "score_std": round(float(col.std()), 1),
        }

        results["factors"][factor] = factor_result

        # Print factor summary
        sig_marker = " ***" if pval < 0.01 else " **" if pval < 0.05 else " *" if pval < 0.10 else ""
        auc_str = f"AUC={auc:.3f}" if auc else "AUC=N/A"
        print(f"\n  {factor.upper().replace('_', ' ')}")
        print(f"    Correlation: {corr:+.4f}  (p={pval:.4f}){sig_marker}")
        print(f"    {auc_str}")
        print(f"    HR hitters avg: {hr_mean:.1f}  |  Non-HR avg: {no_hr_mean:.1f}  |  Gap: {separation:+.1f}")
        if quartiles:
            print(f"    Quartile HR rates:")
            for q in sorted(quartiles.keys()):
                s = quartiles[q]
                bar = "█" * int(s["hr_rate"] / 2)
                print(f"      {q:<12}  {s['hr_rate']:>5.1f}%  ({s['hrs']}/{s['n']})  avg_score={s['avg_score']:.0f}  {bar}")

    # ── Weather sub-analysis ──────────────────────────────────────────────
    print(f"\n{'─'*70}")
    print(f"  WEATHER SUB-ANALYSIS")
    print(f"{'─'*70}")

    # Temperature bands
    if "temp_f" in df.columns:
        temp_bands = pd.cut(df["temp_f"], bins=[0, 50, 60, 70, 80, 90, 120],
                           labels=["<50°F", "50-60°F", "60-70°F", "70-80°F", "80-90°F", "90°F+"])
        temp_analysis = df.groupby(temp_bands, observed=True).agg(
            n=("hit_hr", "count"),
            hrs=("hit_hr", "sum"),
            hr_rate=("hit_hr", "mean"),
        )
        print(f"\n    Temperature vs HR rate:")
        results["weather_detail"] = {"temperature_bands": {}, "wind_detail": {}}
        for band, row in temp_analysis.iterrows():
            rate = row["hr_rate"] * 100
            bar = "█" * int(rate / 2)
            print(f"      {str(band):<10}  {rate:>5.1f}%  ({int(row['hrs'])}/{int(row['n'])})  {bar}")
            results["weather_detail"]["temperature_bands"][str(band)] = {
                "n": int(row["n"]), "hrs": int(row["hrs"]), "hr_rate": round(rate, 2),
            }

    # Wind analysis (non-dome only)
    outdoor = df[df["dome"] == False]
    if len(outdoor) > 0 and "wind_dir" in outdoor.columns:
        def wind_cat(row):
            d = row["wind_dir"]
            mph = row["wind_mph"]
            if mph < 2:
                return "Calm"
            if d >= 315 or d <= 45:
                return "Blowing OUT"
            if 135 <= d <= 225:
                return "Blowing IN"
            return "Crosswind"

        outdoor = outdoor.copy()
        outdoor["wind_cat"] = outdoor.apply(wind_cat, axis=1)
        wind_analysis = outdoor.groupby("wind_cat").agg(
            n=("hit_hr", "count"),
            hrs=("hit_hr", "sum"),
            hr_rate=("hit_hr", "mean"),
        )
        print(f"\n    Wind direction vs HR rate (outdoor games only, n={len(outdoor)}):")
        for cat, row in wind_analysis.iterrows():
            rate = row["hr_rate"] * 100
            bar = "█" * int(rate / 2)
            print(f"      {cat:<15}  {rate:>5.1f}%  ({int(row['hrs'])}/{int(row['n'])})  {bar}")
            results["weather_detail"]["wind_detail"][cat] = {
                "n": int(row["n"]), "hrs": int(row["hrs"]), "hr_rate": round(rate, 2),
            }

    # ── Dome vs Outdoor ──────────────────────────────────────────────
    if "dome" in df.columns:
        dome_analysis = df.groupby("dome").agg(
            n=("hit_hr", "count"),
            hrs=("hit_hr", "sum"),
            hr_rate=("hit_hr", "mean"),
        )
        print(f"\n    Dome vs Outdoor:")
        for is_dome, row in dome_analysis.iterrows():
            label = "Dome" if is_dome else "Outdoor"
            rate = row["hr_rate"] * 100
            print(f"      {label:<10}  {rate:>5.1f}%  ({int(row['hrs'])}/{int(row['n'])})")

    # ── Park factor analysis ──────────────────────────────────────────
    print(f"\n{'─'*70}")
    print(f"  PARK FACTOR ANALYSIS")
    print(f"{'─'*70}")

    venue_stats = df.groupby("venue").agg(
        n=("hit_hr", "count"),
        hrs=("hit_hr", "sum"),
        hr_rate=("hit_hr", "mean"),
        avg_park_score=("park_score", "mean"),
    ).sort_values("hr_rate", ascending=False)

    print(f"\n    {'Venue':<35} {'HR%':>6} {'HRs':>5} {'N':>5} {'ParkSc':>7}")
    for venue, row in venue_stats.iterrows():
        if row["n"] >= 10:  # Only venues with enough data
            print(f"    {str(venue)[:35]:<35} {row['hr_rate']*100:>5.1f}% {int(row['hrs']):>5} "
                  f"{int(row['n']):>5} {row['avg_park_score']:>6.1f}")

    results["venue_stats"] = {
        str(v): {"n": int(r["n"]), "hrs": int(r["hrs"]),
                 "hr_rate": round(r["hr_rate"]*100, 2),
                 "avg_park_score": round(r["avg_park_score"], 1)}
        for v, r in venue_stats.iterrows() if r["n"] >= 10
    }

    # ── Factor ranking summary ────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  FACTOR RANKING (by predictive power)")
    print(f"{'='*70}")
    ranked = sorted(results["factors"].items(),
                    key=lambda x: abs(x[1]["correlation"]), reverse=True)
    print(f"\n    {'Factor':<20} {'Correl':>8} {'AUC':>7} {'Gap':>6} {'Verdict'}")
    print(f"    {'─'*55}")
    for name, r in ranked:
        auc_str = f"{r['auc']:.3f}" if r['auc'] else "  N/A"
        # Verdict
        if r["auc"] and r["auc"] > 0.55:
            verdict = "✓ USEFUL"
        elif r["auc"] and r["auc"] > 0.52:
            verdict = "~ MARGINAL"
        elif r["correlation"] > 0:
            verdict = "? WEAK"
        else:
            verdict = "✗ NOT HELPING"
        print(f"    {name:<20} {r['correlation']:>+.4f} {auc_str:>7} {r['separation']:>+5.1f}  {verdict}")

    return results


# ═══════════════════════════════════════════════════════════════════════════
# 5. MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Factor-level diagnostic analysis for HR Bet model")
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--output", default=None, help="Save detailed JSON results to this path")
    parser.add_argument("--csv", default=None, help="Save raw observation data as CSV")
    args = parser.parse_args()

    print(f"\n{'═'*70}")
    print(f"  HR MODEL FACTOR DIAGNOSTICS")
    print(f"  Analyzing {args.start} through {args.end}")
    print(f"{'═'*70}")

    # Install dependencies if needed
    try:
        from scipy.stats import pointbiserialr
    except ImportError:
        print("  Installing scipy...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "scipy", "--break-system-packages", "-q"])

    try:
        from sklearn.metrics import roc_auc_score
    except ImportError:
        print("  Installing scikit-learn...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "scikit-learn", "--break-system-packages", "-q"])

    # Collect data
    df = collect_diagnostic_data(args.start, args.end)

    if df.empty:
        print("\n  No data collected! Check your date range — are there completed games?")
        return

    # Save raw data if requested
    if args.csv:
        df.to_csv(args.csv, index=False)
        print(f"\n  Raw data saved to {args.csv}")

    # Run analysis
    results = analyze_factors(df)

    # Save detailed results
    if args.output:
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\n  Detailed results saved to {args.output}")

    print(f"\n{'═'*70}")
    print(f"  DONE — {len(df)} observations analyzed")
    print(f"{'═'*70}\n")


if __name__ == "__main__":
    main()
