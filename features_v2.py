#!/usr/bin/env python3
"""
features_v2.py — Fetchers for the second-wave HR scoring features.

Three batter/pitcher Statcast features (free) + one game-environment feed
that requires an API key:

  - Batter xwOBA on contact + pull-FB%   (Statcast / pybaseball)
  - Pitcher fly-ball% allowed            (Statcast / pybaseball)
  - Vegas implied team totals            (the-odds-api.com, free tier)

All functions are defensive: any failure returns None / empty so the
scoring path can fall back to the neutral default.

Caching:
  - All Statcast pulls: 24h TTL, JSON in data/cache/features_v2/
  - Vegas pulls: 1h TTL (odds move during the day)
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests


# ---------------------------------------------------------------------------
# .env auto-loader (no python-dotenv dependency; we just parse KEY=VALUE)
# Loads any KEY=VALUE pairs from <project>/.env into os.environ at import.
# Existing env vars take precedence over file values.
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except Exception:
        pass


_load_dotenv()


# ---------------------------------------------------------------------------
# Cache helpers (mirror pitcher_profile.py style)
# ---------------------------------------------------------------------------

CACHE_DIR = Path(__file__).parent.parent / "data" / "cache" / "features_v2"
TTL_BATTER_ADV = 86400
TTL_PITCHER_BB = 86400
TTL_VEGAS = 3600


def _cache_path(namespace: str, key: str) -> Path:
    ns_dir = CACHE_DIR / namespace
    ns_dir.mkdir(parents=True, exist_ok=True)
    return ns_dir / f"{key}.json"


def _cache_get(namespace: str, key: str, ttl: int):
    path = _cache_path(namespace, key)
    if not path.exists():
        return None
    try:
        if time.time() - path.stat().st_mtime > ttl:
            return None
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _cache_set(namespace: str, key: str, data: Any) -> None:
    try:
        with open(_cache_path(namespace, key), "w") as f:
            json.dump(data, f)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Batter advanced stats: xwOBA on contact + pull-FB%
# ---------------------------------------------------------------------------

def fetch_batter_advanced_stats(
    player_id: int,
    season: int,
    end_date: str | None = None,
) -> dict:
    """
    Pull season-to-date Statcast batted-ball data for a batter and compute:
      - xwoba_contact: mean estimated_woba_using_speedangle on contact events
      - pull_fb_pct:   pulled fly balls / total batted balls (decimal 0-1)

    Returns {} on failure. Cache key: (player_id, season, end_date or 'today').
    """
    if not player_id or player_id < 1000:
        return {}

    end = end_date or datetime.now().strftime("%Y-%m-%d")
    cache_key = f"{player_id}_{season}_{end}"
    cached = _cache_get("batter_adv", cache_key, TTL_BATTER_ADV)
    if cached is not None:
        return cached

    out: dict = {}
    try:
        from pybaseball import statcast_batter
        start = f"{season}-03-20"
        df = statcast_batter(start, end, player_id)
        if df is None or df.empty:
            _cache_set("batter_adv", cache_key, out)
            return out

        # xwOBA on contact: estimated_woba_using_speedangle is non-null only on
        # batted-ball events; we average those.
        if "estimated_woba_using_speedangle" in df.columns:
            xwoba_series = df["estimated_woba_using_speedangle"].dropna()
            if len(xwoba_series) >= 10:
                out["xwoba_contact"] = round(float(xwoba_series.mean()), 3)
                out["xwoba_contact_n"] = int(len(xwoba_series))

        # Pull-FB%: bb_type == 'fly_ball' AND batted_ball_location is "pull"
        # statcast doesn't expose pull as a column directly, but spray angle
        # via hc_x/hc_y is present. Easier proxy: when bb_type=='fly_ball'
        # and the launch location code is in the pull side (bat side specific).
        # Use the simpler pybaseball-supplied 'bb_type' + handedness + hc_x.
        if "bb_type" in df.columns and "stand" in df.columns:
            bb = df.dropna(subset=["bb_type"])
            fb = bb[bb["bb_type"] == "fly_ball"]
            total_bb = max(len(bb), 1)
            if "hc_x" in fb.columns and not fb.empty:
                # Park-neutral pull definition by batter handedness using hc_x.
                # Field center is approx hc_x = 125 (Statcast convention).
                # RHB pull = LF = hc_x < 125; LHB pull = RF = hc_x > 125.
                pulled = 0
                for _, row in fb.iterrows():
                    hc_x = row.get("hc_x")
                    stand = row.get("stand", "R")
                    if hc_x is None or hc_x != hc_x:  # NaN check
                        continue
                    if stand == "R" and hc_x < 125:
                        pulled += 1
                    elif stand == "L" and hc_x > 125:
                        pulled += 1
                if len(fb) >= 5:
                    out["pull_fb_pct"] = round(pulled / total_bb, 3)
                    out["pull_fb_n"] = int(len(fb))

    except Exception as e:
        print(f"  [features_v2] Batter adv fetch failed for {player_id}: {e}")

    _cache_set("batter_adv", cache_key, out)
    return out


def fetch_batter_advanced_batch(
    player_ids: list[tuple[str, int]],
    season: int,
) -> dict[int, dict]:
    """
    Batch wrapper. player_ids: [(name, player_id), ...].
    Returns {player_id: {xwoba_contact, pull_fb_pct, ...}}.
    """
    out = {}
    for name, pid in player_ids:
        if not pid or pid < 1000:
            continue
        adv = fetch_batter_advanced_stats(pid, season)
        if adv:
            out[pid] = adv
    return out


# ---------------------------------------------------------------------------
# Pitcher batted-ball profile: fly-ball% allowed
# ---------------------------------------------------------------------------

def fetch_pitcher_batted_ball_profile(
    pitcher_id: int,
    season: int,
    end_date: str | None = None,
) -> dict:
    """
    Pull season-to-date Statcast for a pitcher and compute:
      - fb_pct_allowed:   fly_ball / total_bb  (percent, 0-100)
      - gb_pct_allowed:   ground_ball / total_bb
      - ld_pct_allowed:   line_drive / total_bb

    Returns {} on failure.
    """
    if not pitcher_id or pitcher_id < 1000:
        return {}

    end = end_date or datetime.now().strftime("%Y-%m-%d")
    cache_key = f"{pitcher_id}_{season}_{end}"
    cached = _cache_get("pitcher_bb", cache_key, TTL_PITCHER_BB)
    if cached is not None:
        return cached

    out: dict = {}
    try:
        from pybaseball import statcast_pitcher
        start = f"{season}-03-20"
        df = statcast_pitcher(start, end, pitcher_id)
        if df is None or df.empty:
            # Try prior season as backstop
            start_prior = f"{season - 1}-03-20"
            end_prior = f"{season - 1}-10-01"
            df = statcast_pitcher(start_prior, end_prior, pitcher_id)
            if df is None or df.empty:
                _cache_set("pitcher_bb", cache_key, out)
                return out

        if "bb_type" in df.columns:
            bb = df.dropna(subset=["bb_type"])
            total = max(len(bb), 1)
            if total >= 20:  # minimum sample
                fb = (bb["bb_type"] == "fly_ball").sum()
                gb = (bb["bb_type"] == "ground_ball").sum()
                ld = (bb["bb_type"] == "line_drive").sum()
                out["fb_pct_allowed"] = round(float(fb) / total * 100, 1)
                out["gb_pct_allowed"] = round(float(gb) / total * 100, 1)
                out["ld_pct_allowed"] = round(float(ld) / total * 100, 1)
                out["bb_sample_n"] = int(total)

    except Exception as e:
        print(f"  [features_v2] Pitcher BB fetch failed for {pitcher_id}: {e}")

    _cache_set("pitcher_bb", cache_key, out)
    return out


def fetch_pitcher_bb_batch(
    pitcher_ids_by_name: dict[str, int],
    season: int,
) -> dict[str, dict]:
    """
    Batch wrapper. pitcher_ids_by_name: {pitcher_name: pitcher_id}.
    Returns {pitcher_name: {fb_pct_allowed, ...}}.
    """
    out = {}
    for name, pid in pitcher_ids_by_name.items():
        if not pid or pid < 1000:
            continue
        prof = fetch_pitcher_batted_ball_profile(pid, season)
        if prof:
            out[name] = prof
    return out


# ---------------------------------------------------------------------------
# BULK Savant leaderboard fetchers — used by the live daily path.
# Two HTTP calls populate xwOBA on contact for ALL batters and FB% allowed
# for ALL pitchers in seconds, vs ~30 minutes of per-player Statcast pulls.
# ---------------------------------------------------------------------------

SAVANT_BASE = "https://baseballsavant.mlb.com/leaderboard"


def fetch_batter_xwoba_bulk(season: int) -> dict[int, float]:
    """
    One HTTP call -> {player_id: xwoba} for every qualified batter in the
    season. Used by generate_picks.py to populate xwoba_contact for the
    whole slate without per-player Statcast calls.

    24-hour cached so multiple runs in one day share a single call.
    """
    cache_key = f"bulk_xwoba_{season}"
    cached = _cache_get("bulk_savant", cache_key, TTL_BATTER_ADV)
    if cached is not None:
        return {int(k): v for k, v in cached.items()}

    url = f"{SAVANT_BASE}/expected_statistics"
    params = {"type": "batter", "year": str(season), "min": "1", "csv": "true"}
    out: dict[int, float] = {}
    try:
        import io
        import pandas as pd
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        for _, row in df.iterrows():
            pid = row.get("player_id")
            xwoba = row.get("est_woba")
            if pd.notna(pid) and pd.notna(xwoba):
                out[int(pid)] = float(xwoba)
    except Exception as e:
        print(f"  [features_v2] bulk xwOBA fetch failed: {e}")

    if out:
        _cache_set("bulk_savant", cache_key, {str(k): v for k, v in out.items()})
    return out


def fetch_pitcher_fb_bulk(season: int) -> dict[int, float]:
    """
    One HTTP call -> {pitcher_id: fb_pct_allowed} for every qualified
    pitcher. The 'fbld' column is fly-ball + line-drive percent, a strong
    proxy for fly-ball% allowed (HR-relevant).

    Note: Savant's URL uses ?type=pitcher (not player_type=pitcher). 24h cached.
    """
    cache_key = f"bulk_pitcher_fb_{season}"
    cached = _cache_get("bulk_savant", cache_key, TTL_PITCHER_BB)
    if cached is not None:
        return {int(k): v for k, v in cached.items()}

    url = f"{SAVANT_BASE}/statcast"
    params = {"year": str(season), "abs": "10", "csv": "true",
              "type": "pitcher", "min": "10"}
    out: dict[int, float] = {}
    try:
        import io
        import pandas as pd
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        for _, row in df.iterrows():
            pid = row.get("player_id")
            fbld = row.get("fbld")
            if pd.notna(pid) and pd.notna(fbld):
                pct = float(fbld)
                if pct < 1:
                    pct *= 100
                out[int(pid)] = round(pct, 1)
    except Exception as e:
        print(f"  [features_v2] bulk pitcher FB% fetch failed: {e}")

    if out:
        _cache_set("bulk_savant", cache_key, {str(k): v for k, v in out.items()})
    return out


# ---------------------------------------------------------------------------
# Vegas implied team totals (the-odds-api.com)
# ---------------------------------------------------------------------------

ODDS_API_BASE = "https://api.the-odds-api.com/v4"


def fetch_vegas_implied_totals(
    date_str: str | None = None,
    api_key: str | None = None,
    bookmaker: str = "draftkings",
) -> dict[str, float]:
    """
    Pull MLB game totals + moneylines from the-odds-api.com and compute the
    implied run total per team.

    Implied team total = (game_total / 2) +/- (moneyline_run_diff_proxy / 2)

    For simplicity (and because moneyline run-diff conversion is noisy at the
    free tier), we split the game total 50/50 and lean on team_total_pct's
    within-slate ranking to do the rest. If the API exposes team totals
    directly (some bookmaker data does), prefer that.

    Returns {team_abbrev: implied_total} or {} on any failure.

    Set the API key via the VEGAS_ODDS_API_KEY env var, or pass api_key.
    Free tier: 500 req/month. One call per day covers the whole MLB slate.
    """
    api_key = api_key or os.environ.get("VEGAS_ODDS_API_KEY")
    if not api_key:
        return {}

    cache_key = f"mlb_totals_{date_str or datetime.now().strftime('%Y-%m-%d')}_{bookmaker}"
    cached = _cache_get("vegas", cache_key, TTL_VEGAS)
    if cached is not None:
        return cached

    out: dict[str, float] = {}
    try:
        url = f"{ODDS_API_BASE}/sports/baseball_mlb/odds"
        params = {
            "apiKey": api_key,
            "regions": "us",
            "markets": "totals,h2h",
            "oddsFormat": "decimal",
            "bookmakers": bookmaker,
        }
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        games = resp.json()

        for g in games:
            home = _team_to_abbrev(g.get("home_team", ""))
            away = _team_to_abbrev(g.get("away_team", ""))
            if not home or not away:
                continue

            total = None
            ml_home = ml_away = None
            for bk in g.get("bookmakers", []):
                for market in bk.get("markets", []):
                    key = market.get("key")
                    outcomes = market.get("outcomes", [])
                    if key == "totals" and outcomes:
                        # All outcomes share the same point value (the total)
                        total = float(outcomes[0].get("point", 0)) or None
                    elif key == "h2h":
                        for oc in outcomes:
                            tabbr = _team_to_abbrev(oc.get("name", ""))
                            price = oc.get("price")
                            if tabbr == home:
                                ml_home = price
                            elif tabbr == away:
                                ml_away = price

            if not total:
                continue

            # Adjust split using moneylines (favorite gets the bigger share).
            home_share = 0.5
            if ml_home and ml_away and ml_home > 0 and ml_away > 0:
                # Decimal odds → implied probability (no vig removed for simplicity).
                p_home = 1.0 / ml_home
                p_away = 1.0 / ml_away
                z = p_home + p_away
                if z > 0:
                    p_home_norm = p_home / z
                    home_share = 0.5 + (p_home_norm - 0.5) * 0.30

            out[home] = round(total * home_share, 2)
            out[away] = round(total * (1 - home_share), 2)

    except Exception as e:
        print(f"  [features_v2] Vegas fetch failed: {e}")

    _cache_set("vegas", cache_key, out)
    return out


_ODDS_TEAM_TO_ABBREV = {
    "Arizona Diamondbacks": "ARI", "Atlanta Braves": "ATL",
    "Baltimore Orioles": "BAL", "Boston Red Sox": "BOS",
    "Chicago Cubs": "CHC", "Chicago White Sox": "CWS",
    "Cincinnati Reds": "CIN", "Cleveland Guardians": "CLE",
    "Colorado Rockies": "COL", "Detroit Tigers": "DET",
    "Houston Astros": "HOU", "Kansas City Royals": "KC",
    "Los Angeles Angels": "LAA", "Los Angeles Dodgers": "LAD",
    "Miami Marlins": "MIA", "Milwaukee Brewers": "MIL",
    "Minnesota Twins": "MIN", "New York Mets": "NYM",
    "New York Yankees": "NYY", "Oakland Athletics": "OAK",
    "Athletics": "OAK",
    "Philadelphia Phillies": "PHI", "Pittsburgh Pirates": "PIT",
    "San Diego Padres": "SD", "San Francisco Giants": "SF",
    "Seattle Mariners": "SEA", "St. Louis Cardinals": "STL",
    "Tampa Bay Rays": "TB", "Texas Rangers": "TEX",
    "Toronto Blue Jays": "TOR", "Washington Nationals": "WSH",
}


def _team_to_abbrev(name: str) -> str:
    if not name:
        return ""
    if name in _ODDS_TEAM_TO_ABBREV:
        return _ODDS_TEAM_TO_ABBREV[name]
    for full, abbr in _ODDS_TEAM_TO_ABBREV.items():
        if full.lower() in name.lower() or name.lower() in full.lower():
            return abbr
    return ""


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Inspect features_v2 fetchers")
    parser.add_argument("--batter", type=int)
    parser.add_argument("--pitcher", type=int)
    parser.add_argument("--vegas", action="store_true")
    parser.add_argument("--bulk", action="store_true", help="Test bulk fetchers")
    parser.add_argument("--season", type=int, default=2026)
    args = parser.parse_args()
    if args.batter:
        print(json.dumps(fetch_batter_advanced_stats(args.batter, args.season), indent=2))
    if args.pitcher:
        print(json.dumps(fetch_pitcher_batted_ball_profile(args.pitcher, args.season), indent=2))
    if args.vegas:
        totals = fetch_vegas_implied_totals()
        print(f"{len(totals)} teams") if totals else print("(empty - check VEGAS_ODDS_API_KEY)")
    if args.bulk:
        b = fetch_batter_xwoba_bulk(args.season)
        p = fetch_pitcher_fb_bulk(args.season)
        print(f"bulk batter xwoba: {len(b)} entries")
        print(f"bulk pitcher fb%:  {len(p)} entries")
