#!/usr/bin/env python3
"""
generate_picks.py — Generate today's 8-pick HR parlay card.

LIVE MODE (default): Fetches real data from MLB Stats API (lineups, probable
pitchers), Open-Meteo (weather), and pybaseball/FanGraphs (Statcast metrics).
Falls back to offline simulation if any API is unreachable.

OFFLINE MODE (--offline): Uses hardcoded 2025 season data with simulated
matchups. Useful for backtesting or when APIs are blocked.

Usage:
    # Live — pulls real lineups, pitchers, weather for today
    python generate_picks.py --date 2026-03-26

    # Offline fallback
    python generate_picks.py --date 2026-03-26 --offline

    # Custom tier blend
    python generate_picks.py --date 2026-03-26 --combo 2,3,3

Requirements (live mode):
    pip install pybaseball pandas numpy requests
"""

import argparse
import json
import sys
import hashlib
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from score_batters import (
    WEIGHT_CONFIGS,
    compute_composite,
    select_top_picks,
    compute_slate_context,
)
from fetch_daily_data import (
    DOME_STADIUMS,
    VENUE_COORDS,
    get_hardcoded_park_factors,
    get_schedule,
    get_weather,
    get_roster,
    get_lineup,
    build_live_tiers,
    get_recent_game_log,
)
from mlb_2025_tiers import (
    ALL_TIERS,
    PITCHERS_2025,
    get_tier_for_batter,
    get_all_batters_lookup,
)
from pitcher_profile import (
    build_pitcher_profiles_batch,
    build_victim_profiles_batch,
    build_victim_profile,
    build_pitcher_profile as build_single_pitcher_profile,
)
from features_v2 import (
    fetch_batter_advanced_batch,
    fetch_pitcher_bb_batch,
    fetch_vegas_implied_totals,
    fetch_batter_xwoba_bulk,
    fetch_pitcher_fb_bulk,
)

# Toggle for the slow per-player Statcast paths (archetype profiles +
# per-batter advanced stats). Set to False for the daily live path; the
# bulk Savant CSV fetchers fill the same data in ~2 seconds vs ~25 minutes.
# Re-enable manually if you want richer matchup_v2 archetype scoring on
# a one-off run.
USE_PER_PLAYER_STATCAST = True

# ---------------------------------------------------------------------------
# Data source status tracking
# ---------------------------------------------------------------------------

class DataSourceStatus:
    """Tracks which data sources succeeded/failed for the status table."""

    def __init__(self):
        self._sources = {}

    def ok(self, source: str, detail: str = ""):
        self._sources[source] = ("✓", detail)

    def warn(self, source: str, detail: str = ""):
        self._sources[source] = ("⚠", detail)

    def fail(self, source: str, detail: str = ""):
        self._sources[source] = ("✗", detail)

    def format_table(self) -> str:
        lines = []
        lines.append("")
        lines.append("  DATA SOURCE STATUS")
        lines.append("  " + "─" * 64)
        for source, (icon, detail) in self._sources.items():
            lines.append(f"    {icon}  {source:<28} {detail}")
        lines.append("  " + "─" * 64)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Scoring config & tier point values
# ---------------------------------------------------------------------------
# Single weight config for all batters — picks top N by composite score
# regardless of tier. Tiers are labels only (for round-robin structuring).
SCORING_CONFIG = "default"  # power 0.30, matchup 0.25, park 0.20, form 0.15, weather 0.10
TIER_POINTS = {1: 1, 2: 3, 3: 9}

# Players to exclude from picks (long-term IL, season-ending injuries, etc.).
# Edit this list as needed — much easier than touching the tier data.
EXCLUDED_PLAYERS = {
    "Anthony Santander",  # torn ACL — out for 2026
    "Eli White",          # rarely in lineups — exclude from recommendations
}


# Map team abbreviations (from MLB Stats API) to full names (from schedule)
TEAM_ABBREV_TO_FULL = {
    "ARI": "Arizona Diamondbacks", "ATL": "Atlanta Braves",
    "BAL": "Baltimore Orioles", "BOS": "Boston Red Sox",
    "CHC": "Chicago Cubs", "CWS": "Chicago White Sox",
    "CIN": "Cincinnati Reds", "CLE": "Cleveland Guardians",
    "COL": "Colorado Rockies", "DET": "Detroit Tigers",
    "HOU": "Houston Astros", "KC": "Kansas City Royals",
    "LAA": "Los Angeles Angels", "LAD": "Los Angeles Dodgers",
    "MIA": "Miami Marlins", "MIL": "Milwaukee Brewers",
    "MIN": "Minnesota Twins", "NYM": "New York Mets",
    "NYY": "New York Yankees", "OAK": "Oakland Athletics",
    "PHI": "Philadelphia Phillies", "PIT": "Pittsburgh Pirates",
    "SD": "San Diego Padres", "SF": "San Francisco Giants",
    "SEA": "Seattle Mariners", "STL": "St. Louis Cardinals",
    "TB": "Tampa Bay Rays", "TEX": "Texas Rangers",
    "TOR": "Toronto Blue Jays", "WSH": "Washington Nationals",
}
# Reverse lookup: full name → abbreviation
TEAM_FULL_TO_ABBREV = {v: k for k, v in TEAM_ABBREV_TO_FULL.items()}

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

# Venue alias map: the MLB Stats API sometimes returns renamed/sponsored
# venue names that don't match our park-factor table.  This maps those
# alternate names to the canonical names used in VENUE_COORDS and
# get_hardcoded_park_factors().
VENUE_ALIASES = {
    # Dodger Stadium naming-rights deal
    "UNIQLO Field at Dodger Stadium": "Dodger Stadium",
    "Uniqlo Field at Dodger Stadium": "Dodger Stadium",
    # Guaranteed Rate Field / old "Rate Field" truncation
    "Rate Field": "Guaranteed Rate Field",
    # Minute Maid Park renamed
    "Daikin Park": "Minute Maid Park",
    # Any future renames — add rows here
}


def normalize_venue(raw_venue: str) -> str:
    """Return the canonical venue name used by park-factor & weather tables."""
    if raw_venue in VENUE_ALIASES:
        return VENUE_ALIASES[raw_venue]
    # Fallback: substring match against canonical names
    for canon in VENUE_COORDS:
        if canon.lower() in raw_venue.lower() or raw_venue.lower() in canon.lower():
            return canon
    return raw_venue


# ---------------------------------------------------------------------------
# LIVE MODE — real data from APIs
# ---------------------------------------------------------------------------

def try_fetch_statcast_recent(player_id: int, days: int = 14) -> dict:
    """Try to get recent Statcast data for form scoring. Returns dict or empty."""
    try:
        from pybaseball import statcast_batter
        end = datetime.now()
        start = end - timedelta(days=days)
        df = statcast_batter(
            start.strftime("%Y-%m-%d"),
            end.strftime("%Y-%m-%d"),
            player_id,
        )
        if df.empty:
            return {}
        # Compute recent form metrics
        hr_events = df[df["events"] == "home_run"]
        barrels = df[df["launch_speed_angle"].fillna(0) >= 6] if "launch_speed_angle" in df.columns else df.head(0)
        recent_ev = df["launch_speed"].mean() if "launch_speed" in df.columns else None
        season_ev_approx = 88.0  # rough league average
        return {
            "recent_hr_14d": len(hr_events),
            "recent_barrel_pct_14d": len(barrels) / max(len(df), 1) * 100,
            "ev_trend_14d": (recent_ev - season_ev_approx) if recent_ev else 0,
        }
    except Exception:
        return {}


def fetch_form_data_batch(player_ids: list[tuple[str, int]], season: int) -> dict:
    """
    Fetch recent game logs for a batch of players via the MLB Stats API.
    Much more reliable than per-batter Statcast calls.

    *player_ids* is a list of (name, player_id) tuples.
    Returns {player_id: game_log_dict}.
    """
    results = {}
    for name, pid in player_ids:
        if not pid or pid < 1000:
            continue
        log = get_recent_game_log(pid, season, last_n_games=10)
        if log:
            results[pid] = log
    return results


def try_fetch_pitcher_season_stats(pitcher_name: str, season: int) -> dict:
    """Try to get pitcher season stats from FanGraphs. Returns dict or empty.
    DEPRECATED — FanGraphs now blocks automated requests via Cloudflare.
    Kept as fallback but fetch_pitcher_stats_mlb() should be used instead.
    """
    try:
        from pybaseball import pitching_stats
        df = pitching_stats(season, qual=10)
        match = df[df["Name"] == pitcher_name]
        if match.empty:
            match = df[df["Name"].str.contains(pitcher_name.split()[-1], na=False)]
        if match.empty:
            return {}
        row = match.iloc[0]
        return {
            "name": pitcher_name,
            "hr_per_9": row.get("HR/9", 1.2),
            "era": row.get("ERA", 4.0),
            "hard_hit_pct_allowed": row.get("HardHit%", 35),
            "throws": row.get("Throws", "R") if "Throws" in row.index else "R",
            "k_per_9": row.get("K/9", 8.0),
        }
    except Exception:
        return {}


def fetch_pitcher_stats_mlb(pitcher_id: int, pitcher_name: str, season: int) -> dict:
    """
    Fetch pitcher season stats from the MLB Stats API (free, no Cloudflare).
    Returns dict with keys: name, hr_per_9, era, hard_hit_pct_allowed, throws, k_per_9.
    Or empty dict on failure.
    """
    import requests
    url = f"https://statsapi.mlb.com/api/v1/people/{pitcher_id}"
    params = {
        "hydrate": f"stats(group=[pitching],type=[season],season={season})",
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {}

    people = data.get("people", [])
    if not people:
        return {}

    person = people[0]
    throws = person.get("pitchHand", {}).get("code", "R")

    # Find season pitching stats
    stats_groups = person.get("stats", [])
    stat = {}
    for group in stats_groups:
        splits = group.get("splits", [])
        if splits:
            stat = splits[0].get("stat", {})
            break

    if not stat:
        return {}

    ip = float(stat.get("inningsPitched", "0") or "0")
    hr_allowed = stat.get("homeRuns", 0)
    era = float(stat.get("era", "4.00") or "4.00")
    k = stat.get("strikeOuts", 0)
    bb = stat.get("baseOnBalls", 0)
    hits_allowed = stat.get("hits", 0)
    ab_against = stat.get("atBats", 0)

    # Compute derived metrics
    hr_per_9 = round((hr_allowed / max(ip, 1)) * 9, 2) if ip > 0 else 1.2
    k_per_9 = round((k / max(ip, 1)) * 9, 2) if ip > 0 else 8.0

    # Estimate hard-hit% from WHIP as proxy (higher WHIP ≈ more hard contact)
    whip = (bb + hits_allowed) / max(ip, 1) if ip > 0 else 1.3
    est_hard_hit_pct = round(min(50, max(25, 25 + (whip - 1.0) * 20)), 1)

    return {
        "name": pitcher_name,
        "hr_per_9": hr_per_9,
        "era": era,
        "hard_hit_pct_allowed": est_hard_hit_pct,
        "throws": throws,
        "k_per_9": k_per_9,
        "ip": ip,
        "source": "mlb_stats_api",
    }


def fetch_live_slate(date_str: str, status: DataSourceStatus = None) -> dict:
    """
    Fetch today's real MLB slate: games, lineups, pitchers, weather.
    Returns a dict with all data needed for scoring, or None if APIs fail.
    Populates *status* tracker with per-source results.
    """
    if status is None:
        status = DataSourceStatus()

    # ── Schedule ──────────────────────────────────────────────────────
    print(f"  [LIVE] Fetching MLB schedule for {date_str}...")
    try:
        games = get_schedule(date_str)
    except Exception as e:
        status.fail("MLB Schedule API", f"Error: {e}")
        return None

    if not games:
        status.fail("MLB Schedule API", "No games found")
        return None

    status.ok("MLB Schedule API", f"{len(games)} games found")

    # Normalize venue names so park factors / weather coords resolve correctly
    for g in games:
        g["venue"] = normalize_venue(g["venue"])

    # ── Weather ───────────────────────────────────────────────────────
    weather_data = {}
    weather_ok = 0
    weather_fail = 0
    for g in games:
        venue = g["venue"]
        game_time = g.get("game_time", "")
        try:
            weather_data[g["game_pk"]] = get_weather(venue, game_time)
            weather_ok += 1
        except Exception:
            is_dome = venue in DOME_STADIUMS
            weather_data[g["game_pk"]] = {
                "temperature_f": 72 if is_dome else 75,
                "wind_mph": 0 if is_dome else 5,
                "wind_direction_deg": 0,
                "dome": is_dome,
            }
            weather_fail += 1

    if weather_fail == 0:
        status.ok("Weather (Open-Meteo)", f"{weather_ok}/{len(games)} games")
    else:
        status.warn("Weather (Open-Meteo)", f"{weather_ok} OK, {weather_fail} defaulted")

    # ── Park factor matching ──────────────────────────────────────────
    pf_df = get_hardcoded_park_factors()
    pf_venues = set(pf_df["venue"].tolist())
    game_venues = {g["venue"] for g in games}
    unmatched = game_venues - pf_venues
    if not unmatched:
        status.ok("Park Factor Match", f"{len(game_venues)} venues matched")
    else:
        status.warn("Park Factor Match", f"{len(unmatched)} unmatched: {', '.join(sorted(unmatched))}")

    # ── Lineups ───────────────────────────────────────────────────────
    lineups = {}
    games_with_lineups = []
    for g in games:
        gpk = g["game_pk"]
        lu = get_lineup(gpk)
        if lu["home"] or lu["away"]:
            lineups[gpk] = lu
            games_with_lineups.append(g)
        else:
            away = g.get("away_team", "?")
            home = g.get("home_team", "?")
            print(f"  [LIVE] No lineup for game {gpk} ({away} @ {home}) — SKIPPING")

    if not games_with_lineups:
        status.fail("Confirmed Lineups", "No lineups found for any game")
        return None

    status.ok("Confirmed Lineups", f"{len(games_with_lineups)}/{len(games)} games")
    games = games_with_lineups

    # ── Pitcher stats (MLB Stats API — no FanGraphs/Cloudflare issues) ─
    pitcher_offline = {p["name"]: p for p in PITCHERS_2025}
    pitcher_lookup = {}
    season = int(date_str[:4])
    mlb_api_count = 0
    offline_count = 0
    unknown_count = 0

    for g in games:
        for side in ["home", "away"]:
            pname = g.get(f"{side}_pitcher_name", "TBD")
            pid = g.get(f"{side}_pitcher_id")
            if pname == "TBD" or pname in pitcher_lookup:
                continue

            # Try MLB Stats API first (free, reliable, no Cloudflare)
            if pid:
                mlb_stats = fetch_pitcher_stats_mlb(pid, pname, season)
                if mlb_stats:
                    pitcher_lookup[pname] = mlb_stats
                    mlb_api_count += 1
                    continue

            # Offline fallback (hardcoded 2025 data)
            if pname in pitcher_offline:
                pitcher_lookup[pname] = pitcher_offline[pname]
                offline_count += 1
            else:
                pitcher_lookup[pname] = {
                    "name": pname, "hr_per_9": 1.2,
                    "hard_hit_pct_allowed": 35, "throws": "R",
                }
                unknown_count += 1

    total_pitchers = mlb_api_count + offline_count + unknown_count
    if mlb_api_count > 0:
        status.ok("Pitcher Stats (MLB API)", f"{mlb_api_count}/{total_pitchers} live")
    elif offline_count > 0:
        status.warn("Pitcher Stats (MLB API)", f"API failed — {offline_count} offline, {unknown_count} league-avg default")
    else:
        status.fail("Pitcher Stats (MLB API)", f"All {unknown_count} pitchers defaulted to league-avg")

    # ── Pitcher archetype profiles (Statcast arsenal data) ─────────────
    pitcher_id_map = {}
    for g in games:
        for side in ["home", "away"]:
            pname = g.get(f"{side}_pitcher_name", "TBD")
            pid = g.get(f"{side}_pitcher_id")
            if pname != "TBD" and pid:
                pitcher_id_map[pname] = pid

    pitcher_profiles = {}
    if USE_PER_PLAYER_STATCAST:
        try:
            print(f"  [ARCHETYPE] Building pitcher profiles for {len(pitcher_id_map)} starters...")
            pitcher_profiles = build_pitcher_profiles_batch(pitcher_id_map, season)
            statcast_count = sum(1 for p in pitcher_profiles.values() if p.get("source") == "statcast")
            estimate_count = sum(1 for p in pitcher_profiles.values() if p.get("source") != "statcast")
            if statcast_count > 0:
                status.ok("Pitcher Archetypes", f"{statcast_count} Statcast, {estimate_count} estimated")
            else:
                status.warn("Pitcher Archetypes", f"0 Statcast — {estimate_count} estimated from MLB API")
        except Exception as e:
            status.warn("Pitcher Archetypes", f"Failed: {e} — matchup v2 will use fallback")
    else:
        # Skipped on daily path — per-pitcher Statcast arsenal builds are slow
        # and prone to hangs. Falls through to score_matchup() v1 which is
        # already slate-context aware (no archetype similarity, but vulnerability
        # + Vegas + platoon all still flow). Re-enable USE_PER_PLAYER_STATCAST
        # manually for one-off runs that want archetype matching.
        status.warn("Pitcher Archetypes", "skipped (USE_PER_PLAYER_STATCAST=False) - matchup v1 path")

    # ── Pitcher FB% allowed via BULK Savant CSV (one HTTP call) ────────
    # Was per-pitcher Statcast — wedged the noon run on 2026-04-29.
    try:
        bulk_pitcher_fb = fetch_pitcher_fb_bulk(season)
        for pname, pid in pitcher_id_map.items():
            if pid in bulk_pitcher_fb and pname in pitcher_lookup:
                pitcher_lookup[pname]["fb_pct_allowed"] = bulk_pitcher_fb[pid]
        n_with_fb = sum(1 for p in pitcher_lookup.values() if p.get("fb_pct_allowed") is not None)
        if n_with_fb > 0:
            status.ok("Pitcher FB% Allowed", f"{n_with_fb}/{len(pitcher_lookup)} via bulk Savant")
        else:
            status.warn("Pitcher FB% Allowed", "0 starters — vulnerability falls back to league avg")
    except Exception as e:
        status.warn("Pitcher FB% Allowed", f"Bulk fetch failed: {e}")

    # ── Batter xwOBA on contact via BULK Savant CSV (one HTTP call) ────
    # Replaces per-batter Statcast in score_live_slate. Slate-level cache.
    bulk_batter_xwoba: dict = {}
    try:
        bulk_batter_xwoba = fetch_batter_xwoba_bulk(season)
        if bulk_batter_xwoba:
            status.ok("Batter xwOBA (bulk)", f"{len(bulk_batter_xwoba)} batters via Savant")
        else:
            status.warn("Batter xwOBA (bulk)", "No data — power score falls back to ISO/EV")
    except Exception as e:
        status.warn("Batter xwOBA (bulk)", f"Bulk fetch failed: {e}")

    # ── Vegas implied team totals ──────────────────────────────────────
    implied_totals: dict = {}
    try:
        implied_totals = fetch_vegas_implied_totals(date_str=date_str)
        if implied_totals:
            status.ok("Vegas Implied Totals", f"{len(implied_totals)} teams (the-odds-api)")
        else:
            status.warn("Vegas Implied Totals", "No data — set VEGAS_ODDS_API_KEY to enable")
    except Exception as e:
        status.warn("Vegas Implied Totals", f"Failed: {e}")

    # ── Live tiers (rolling window) ───────────────────────────────────
    live_tiers = build_live_tiers(date_str)
    if live_tiers:
        tier_sizes = {t: len(v) for t, v in live_tiers.items()}
        status.ok("Live Tier Build", f"T1={tier_sizes.get(1,0)} T2={tier_sizes.get(2,0)} T3={tier_sizes.get(3,0)}")
    else:
        status.fail("Live Tier Build", "Failed — falling back to hardcoded 2025 tiers")

    return {
        "games": games,
        "weather": weather_data,
        "lineups": lineups,
        "pitchers": pitcher_lookup,
        "pitcher_profiles": pitcher_profiles,
        "live_tiers": live_tiers,
        "implied_totals": implied_totals,
        "bulk_batter_xwoba": bulk_batter_xwoba,
    }


def get_lineup_player_ids(slate: dict, game_pk: int, side: str) -> set:
    """Return set of player IDs in the confirmed lineup for a game/side."""
    lu = slate.get("lineups", {}).get(game_pk, {})
    return {p["player_id"] for p in lu.get(side, []) if p.get("player_id")}


def score_live_slate(
    slate: dict,
    date_str: str,
    tier: int,
    config_name: str,
    pf,
    slate_ctx: dict | None = None,
) -> list:
    """
    Score all batters in a tier against the live slate.
    Matches batters to their actual games/opponents from the schedule.
    Uses confirmed batting order position for lineup factor scoring.

    *slate_ctx* — optional pre-computed within-slate percentile context.
    When provided, park/weather/pitcher-vulnerability scoring uses
    within-slate percentile rankings rather than fixed-anchor scaling.
    Computed once in generate_card() and passed into all 3 tier passes.
    """
    # Use live tiers if available, otherwise fall back to hardcoded
    active_tiers = slate.get("live_tiers") or ALL_TIERS

    batter_lookup = get_all_batters_lookup()
    tier_batters = active_tiers.get(tier, [])

    # Map team to game — index by both full name AND abbreviation
    team_to_game = {}
    for g in slate["games"]:
        team_to_game[g["home_team"]] = g
        team_to_game[g["away_team"]] = g
        home_abbrev = TEAM_FULL_TO_ABBREV.get(g["home_team"])
        away_abbrev = TEAM_FULL_TO_ABBREV.get(g["away_team"])
        if home_abbrev:
            team_to_game[home_abbrev] = g
        if away_abbrev:
            team_to_game[away_abbrev] = g

    # Build lineup lookups: player ID → batting order, last name → batting order
    # Also build confirmed sets for the eligibility filter.
    confirmed_ids: dict[int, set] = {}    # game_pk -> set of player_ids
    confirmed_names: dict[int, set] = {}  # game_pk -> set of lowercase last names
    lineup_order_by_id: dict[int, dict[int, int]] = {}    # game_pk -> {player_id: batting_order}
    lineup_order_by_name: dict[int, dict[str, int]] = {}  # game_pk -> {last_name: batting_order}

    for gpk, lu in slate.get("lineups", {}).items():
        ids = set()
        names = set()
        order_by_id = {}
        order_by_name = {}
        # bdfed returns ALL roster players for each side (~13-15 entries)
        # with the 9 starters first, then bench. Cap real batting orders at 9;
        # positions 10+ are bench/reserves. Without this cap, bench players
        # got batting_order=11/12 which fell into score_lineup_position's
        # catch-all (35) instead of bench (15) and slipped into top-8 picks.
        bench_ids = set()
        bench_names = set()
        for side in ["home", "away"]:
            for i, p in enumerate(lu.get(side, []), 1):
                pid = p.get("player_id")
                pname = p.get("name", "").lower().strip()
                if i <= 9:
                    if pid:
                        ids.add(pid)
                        order_by_id[pid] = i
                    if pname:
                        names.add(pname)
                        order_by_name[pname] = i
                else:
                    if pid:
                        bench_ids.add(pid)
                    if pname:
                        bench_names.add(pname)
        confirmed_ids[gpk] = ids
        confirmed_names[gpk] = names
        lineup_order_by_id[gpk] = order_by_id
        lineup_order_by_name[gpk] = order_by_name
        slate.setdefault("_bench_ids", {})[gpk] = bench_ids
        slate.setdefault("_bench_names", {})[gpk] = bench_names

    all_scored = []
    season = int(date_str[:4])

    # Pre-filter to batters who are actually playing today
    eligible_batters = []
    for b in tier_batters:
        if b["name"] in EXCLUDED_PLAYERS:
            continue
        batter_team = b.get("team", "")
        game = team_to_game.get(batter_team)
        if not game:
            full = TEAM_ABBREV_TO_FULL.get(batter_team)
            if full:
                game = team_to_game.get(full)
        if not game:
            continue

        gpk = game["game_pk"]
        player_id = b.get("player_id", hash(b["name"]) % 1_000_000)
        last_name = b["name"].split()[-1].lower().strip()

        # Determine batting order / lineup status
        batting_order = None
        if gpk in confirmed_ids and confirmed_ids[gpk]:
            # Lineup data exists — check if batter is in it
            if player_id in lineup_order_by_id.get(gpk, {}):
                batting_order = lineup_order_by_id[gpk][player_id]
            elif last_name in lineup_order_by_name.get(gpk, {}):
                batting_order = lineup_order_by_name[gpk][last_name]
            else:
                # Team has lineup posted but this batter isn't in it.
                # Check roster (passed via slate) — if on roster, they're
                # a bench bat; if not, skip entirely.
                batting_order = "bench"
        else:
            # No lineup data for this game — roster-only fallback
            batting_order = "roster_only"

        eligible_batters.append((b, game, player_id, batting_order))

    # Batch-fetch game logs for all eligible batters
    player_id_list = [(b["name"], pid) for b, _, pid, _ in eligible_batters]
    game_logs = fetch_form_data_batch(player_id_list, season)
    log_hit = sum(1 for _, _, pid, _ in eligible_batters if pid in game_logs)
    print(f"  [FORM] Fetched game logs for {log_hit}/{len(eligible_batters)} "
          f"T{tier} batters")

    # xwOBA on contact: read from slate-level bulk pull (one HTTP call total
    # across the whole day, populated in fetch_live_slate). Per-player
    # statcast_batter calls used to live here and were the second
    # contributor to the noon-run hang on 2026-04-29.
    bulk_xwoba = slate.get("bulk_batter_xwoba", {})
    batter_adv = {pid: {"xwoba_contact": v} for pid, v in bulk_xwoba.items()}
    adv_hit = sum(1 for _, _, pid, _ in eligible_batters if pid in batter_adv)
    print(f"  [ADV-BULK] xwOBA on contact: {adv_hit}/{len(eligible_batters)} T{tier} batters")
    # Note: pull_fb_pct is no longer fetched on the daily path — Savant has no
    # bulk endpoint for it. Defaults to 50/neutral in score_power. Re-enable
    # via per-player fetch_batter_advanced_batch by setting USE_PER_PLAYER_STATCAST=True.

    # Build victim profiles for archetype matching (v2 matchup scoring).
    # Only runs when USE_PER_PLAYER_STATCAST=True — otherwise the third
    # source of per-player Statcast hangs. With it off, score_matchup_v2
    # is bypassed and score_matchup() v1 (slate-aware) is used instead.
    pitcher_profiles = slate.get("pitcher_profiles", {})
    victim_profiles = {}
    if pitcher_profiles and USE_PER_PLAYER_STATCAST:
        print(f"  [ARCHETYPE] Building victim profiles for {len(eligible_batters)} T{tier} batters...")
        try:
            victim_profiles = build_victim_profiles_batch(player_id_list, season)
            vp_hit = sum(1 for _, _, pid, _ in eligible_batters if pid in victim_profiles)
            print(f"  [ARCHETYPE] Victim profiles built for {vp_hit}/{len(eligible_batters)} T{tier} batters")
        except Exception as e:
            print(f"  [ARCHETYPE] Victim profile batch failed: {e} — falling back to v1 matchup")

    for b, game, player_id, batting_order in eligible_batters:
        gpk = game["game_pk"]
        ht = game["home_team"]
        at = game["away_team"]
        venue = game["venue"]

        # Determine opposing pitcher
        batter_team = b.get("team", "")
        home_abbrev = TEAM_FULL_TO_ABBREV.get(ht, ht)
        batter_is_home = (batter_team == ht or batter_team == home_abbrev)
        if batter_is_home:
            opp_pitcher_name = game.get("away_pitcher_name", "TBD")
        else:
            opp_pitcher_name = game.get("home_pitcher_name", "TBD")

        opp = slate["pitchers"].get(opp_pitcher_name, {
            "hr_per_9": 1.2, "hard_hit_pct_allowed": 35, "throws": "R"
        })

        weather = slate["weather"].get(gpk, {
            "temperature_f": 75, "wind_mph": 5, "wind_direction_deg": 0, "dome": False
        })

        # Use game-log form data (real recent performance!)
        log = game_logs.get(player_id, {})
        if log:
            recent_hr = log.get("recent_hr", 0)
            recent_barrel_est = min(25, log.get("recent_iso", 0.120) * 100)
            season_slg_approx = b.get("iso", 0.150) + 0.250
            ev_proxy = (log.get("recent_slg", season_slg_approx) - season_slg_approx) * 30
        else:
            recent_hr = b["hr"] / 25
            recent_barrel_est = b.get("barrel_pct", 8)
            ev_proxy = 0

        entry = {
            **b,
            "player_id": player_id,
            "recent_hr_14d": recent_hr,
            "recent_barrel_pct_14d": recent_barrel_est,
            "ev_trend_14d": ev_proxy,
        }
        # Layer in advanced Statcast features when available (defaults
        # to neutral if missing — score_power handles None gracefully).
        adv = batter_adv.get(player_id, {})
        if adv.get("xwoba_contact") is not None:
            entry["xwoba_contact"] = adv["xwoba_contact"]
        if adv.get("pull_fb_pct") is not None:
            entry["pull_fb_pct"] = adv["pull_fb_pct"]

        # Get archetype profiles for v2 matchup scoring
        vp = victim_profiles.get(player_id)
        pp = pitcher_profiles.get(opp_pitcher_name)

        result = compute_composite(
            entry, opp, venue, weather, pf, config_name,
            victim_profile=vp,
            pitcher_profile=pp,
            batting_order=batting_order,
            slate_ctx=slate_ctx,
            game_pk=gpk,
        )
        result["player_id"] = player_id
        result["game_pk"] = gpk
        result["opp_pitcher"] = opp_pitcher_name
        result["tier"] = tier
        result["game_venue"] = venue
        result["home_team"] = ht
        result["away_team"] = at
        result["confirmed_starter"] = isinstance(batting_order, int)
        result["has_game_log"] = bool(log)
        result["hot_streak"] = log.get("hot_streak", False)
        result["has_archetype"] = bool(vp and pp)

        all_scored.append(result)

    all_scored.sort(key=lambda x: x["composite"], reverse=True)
    return all_scored


# ---------------------------------------------------------------------------
# OFFLINE MODE — simulated slate from hardcoded data
# ---------------------------------------------------------------------------

def date_seed(date_str):
    """Deterministic seed from date string."""
    return int(hashlib.md5(date_str.encode()).hexdigest()[:8], 16)


def simulate_slate(date_str, tier, config_name, rng, pf, slate_ctx: dict | None = None):
    """Simulate a day's games for one tier and score all batters.

    *slate_ctx* — optional pre-computed slate context. In offline mode this
    is rarely populated since each call generates fresh random venues, but
    it's wired through for parity with the live path.
    """
    pitcher_lookup = {p["name"]: p for p in PITCHERS_2025}
    pitcher_names = list(pitcher_lookup.keys())
    batters = [b for b in ALL_TIERS[tier] if b["name"] not in EXCLUDED_PLAYERS]

    n_games = rng.integers(12, 16)
    venues = list(VENUES)
    rng.shuffle(venues)
    all_scored = []

    for g in range(min(n_games, 15)):
        _, venue = venues[g % len(venues)]
        hp_name = rng.choice(pitcher_names)
        ap_name = rng.choice(pitcher_names)
        gpk = int(rng.integers(1_000_000, 9_999_999))

        is_dome = venue in DOME_STADIUMS
        weather = {
            "temperature_f": 72.0 if is_dome else float(rng.normal(68, 8)),
            "wind_mph": 0.0 if is_dome else float(max(0, rng.normal(8, 5))),
            "wind_direction_deg": 0 if is_dome else int(rng.integers(0, 360)),
            "humidity_pct": 50.0 if is_dome else float(max(10, min(100, rng.normal(55, 20)))),
            "dome": is_dome,
        }

        available = list(batters)
        rng.shuffle(available)
        home = available[:5]
        away = available[5:10]

        for lineup, opp_name in [(away, hp_name), (home, ap_name)]:
            opp = pitcher_lookup.get(
                opp_name,
                {"hr_per_9": 1.2, "hard_hit_pct_allowed": 35, "throws": "R"},
            )
            for i, b in enumerate(lineup, 1):
                # Simulate batting order: sorted roughly by quality + noise
                batting_order = min(9, max(1, i + int(rng.integers(-1, 2))))
                entry = {
                    **b,
                    "player_id": hash(b["name"]) % 1_000_000,
                    "recent_hr_14d": float(
                        min(5, max(0, b["hr"] / 25 + rng.normal(0, 0.8)))
                    ),
                    "recent_barrel_pct_14d": float(
                        max(0, b.get("barrel_pct", 8) + rng.normal(0, 3))
                    ),
                    "ev_trend_14d": float(rng.normal(0, 1.5)),
                }
                result = compute_composite(
                    entry, opp, venue, weather, pf, config_name,
                    batting_order=batting_order,
                    slate_ctx=slate_ctx,
                    game_pk=gpk,
                )
                result["player_id"] = entry["player_id"]
                result["game_pk"] = gpk
                result["opp_pitcher"] = opp_name
                result["tier"] = tier
                all_scored.append(result)

    all_scored.sort(key=lambda x: x["composite"], reverse=True)
    return all_scored


# ---------------------------------------------------------------------------
# Card generation — works for both live and offline
# ---------------------------------------------------------------------------

def generate_card(date_str, combo=(3, 2, 3), force_offline=False):
    """
    Generate the full 8-pick card with tier blending.
    Tries live API data first; falls back to offline simulation.
    """
    pf = get_hardcoded_park_factors()
    mode = "offline_simulation"
    live_slate = None
    status = DataSourceStatus()

    if not force_offline:
        print("\n  Attempting live data fetch...")
        live_slate = fetch_live_slate(date_str, status=status)
        if live_slate and live_slate["games"]:
            mode = "live"
            tier_src = "LIVE rolling" if live_slate.get("live_tiers") else "hardcoded 2025"
            print(f"  Using LIVE data ({len(live_slate['games'])} games, tiers: {tier_src})")
        else:
            print("  Live fetch failed or no games — falling back to offline mode")
            live_slate = None

    if not live_slate:
        print("  Using OFFLINE simulated slate")
        status.fail("Mode", "OFFLINE — no live data available")

    card = []
    tier_details = {}
    full_board = []  # every scored batter across all tiers
    seed = date_seed(date_str)
    rng = np.random.default_rng(seed)
    total_picks = sum(combo)  # typically 8

    # Score ALL tiers into one pool, then pick top N by composite.
    # Tiers are kept as labels (chalk/mid/longshot) but don't constrain selection.
    config = "default"  # single weight config for all batters

    # Compute slate-context percentile rankings ONCE for the whole day.
    # Park, weather, and pitcher-vulnerability scoring will use within-slate
    # ranks instead of fixed-anchor scaling, fixing the score-compression
    # problem where the worst HR-allowing pitchers all clustered at ~70.
    slate_ctx = None
    if live_slate:
        try:
            slate_ctx = compute_slate_context(
                games=live_slate["games"],
                weather_by_gpk=live_slate.get("weather", {}),
                pitcher_stats_by_name=live_slate.get("pitchers", {}),
                park_factors=pf,
                implied_totals_by_team=live_slate.get("implied_totals", {}),
            )
            n_parks = len(slate_ctx.get("park_pct", {}))
            n_weather = len(slate_ctx.get("weather_pct", {}))
            n_pitchers = len(slate_ctx.get("pitcher_pct", {}))
            n_totals = len(slate_ctx.get("team_total_pct", {}))
            status.ok(
                "Slate-Rank Context",
                f"{n_parks} venues, {n_weather} games, {n_pitchers} pitchers, {n_totals} team totals",
            )
        except Exception as e:
            status.warn("Slate-Rank Context", f"Failed: {e} — falling back to fixed-anchor scoring")
            slate_ctx = None

    for tier in [1, 2, 3]:
        if live_slate:
            scored = score_live_slate(
                live_slate, date_str, tier, config, pf,
                slate_ctx=slate_ctx,
            )
        else:
            scored = simulate_slate(date_str, tier, config, rng, pf, slate_ctx=slate_ctx)

        if not scored:
            print(f"  WARNING: No batters scored for tier {tier} — "
                  f"team may have off day. Falling back to offline.")
            scored = simulate_slate(date_str, tier, config, rng, pf, slate_ctx=slate_ctx)

        tier_label = {1: "T1-Chalk", 2: "T2-Mid", 3: "T3-Longshot"}[tier]
        for s in scored:
            s["tier_label"] = tier_label
            s["tier"] = tier
        full_board.extend(scored)

        tier_details[tier] = {
            "config": config,
            "pool_size": len(scored),
            "top_scorer": scored[0]["name"] if scored else "N/A",
            "top_composite": scored[0]["composite"] if scored else 0,
        }

    # Sort the entire pool by composite and pick top N
    full_board.sort(key=lambda x: x["composite"], reverse=True)

    GLOBAL_MAX_PER_GAME = 2
    global_game_counts: dict[int, int] = {}
    seen_names: set[str] = set()

    for batter in full_board:
        if len(card) >= total_picks:
            break
        name = batter.get("name", "")
        if name in seen_names:
            continue
        # Hard exclude: only confirmed starters (batting_order int 1-9) make
        # the card. Bench / roster_only / null are still on the full_board
        # for visibility but never get selected. Fixes 2026-04-29 noon
        # case where Suzuki at "BO=12" (a bench reserve) was top-8.
        bo = batter.get("batting_order")
        if not (isinstance(bo, int) and 1 <= bo <= 9):
            continue
        gpk = batter.get("game_pk")
        if global_game_counts.get(gpk, 0) >= GLOBAL_MAX_PER_GAME:
            continue
        batter["tier_pts"] = TIER_POINTS.get(batter.get("tier", 2), 3)
        batter["selected"] = True
        card.append(batter)
        seen_names.add(name)
        global_game_counts[gpk] = global_game_counts.get(gpk, 0) + 1

    # Sort card by composite descending (best pick first)
    card.sort(key=lambda x: -x["composite"])

    # Sort full board by composite descending (across all tiers)
    full_board.sort(key=lambda x: x["composite"], reverse=True)

    # Add form data coverage to status
    if full_board:
        with_log = sum(1 for b in full_board if b.get("has_game_log"))
        total = len(full_board)
        if with_log > 0:
            status.ok("Game Logs (Form)", f"{with_log}/{total} batters with real recent data")
        else:
            status.warn("Game Logs (Form)", f"0/{total} — all batters using static fallback")

        # Archetype matching coverage
        with_archetype = sum(1 for b in full_board if b.get("has_archetype"))
        v2_count = sum(1 for b in full_board if b.get("matchup_version") == "v2")
        if v2_count > 0:
            status.ok("Matchup v2 (Archetype)", f"{v2_count}/{total} batters scored with archetype matching")
        elif with_archetype > 0:
            status.warn("Matchup v2 (Archetype)", f"{with_archetype}/{total} had profiles but v2 not triggered")
        else:
            status.warn("Matchup v2 (Archetype)", f"0/{total} — all using v1 matchup fallback")

    return card, tier_details, mode, full_board, status


def format_card(card, date_str, combo, tier_details, mode):
    """Pretty-print the parlay card."""
    mode_label = "LIVE" if mode == "live" else "OFFLINE (simulated matchups)"
    tier_breakdown = {}
    for p in card:
        t = p.get("tier_label", "?")
        tier_breakdown[t] = tier_breakdown.get(t, 0) + 1
    tier_summary = ", ".join(f"{v}x {k}" for k, v in sorted(tier_breakdown.items()))

    lines = []
    lines.append("")
    lines.append("=" * 76)
    lines.append(f"  DAILY HR PARLAY CARD — {date_str}")
    lines.append(f"  Mode: {mode_label}")
    lines.append(f"  Selection: Top {len(card)} by composite score ({tier_summary})")
    lines.append(f"  Max pts if all hit: "
                 f"{sum(TIER_POINTS[c.get('tier', 1)] for c in card)}")
    lines.append("=" * 76)
    lines.append("")
    lines.append(f"  {'#':<3} {'Player':<22} {'Team':<5} {'Tier':<13} "
                 f"{'vs Pitcher':<18} {'Venue':<26} {'Score':>6} {'Order':>5} {'Pts':>4}")
    lines.append(f"  {'-' * 101}")

    for i, p in enumerate(card, 1):
        opp_p = p.get("opp_pitcher", "N/A")
        if len(opp_p) > 16:
            opp_p = opp_p[:15] + "."
        bo = p.get("batting_order", "?")
        bo_str = f"#{bo}" if isinstance(bo, int) else str(bo)[:5]
        lines.append(
            f"  {i:<3} {p['name']:<22} {p['team']:<5} {p.get('tier_label', ''):<13} "
            f"{opp_p:<18} {p['venue']:<26} {p['composite']:>5.1f} {bo_str:>5} {p.get('tier_pts', 1):>4}"
        )

    # Gameday links
    lines.append("")
    lines.append("  Game Links:")
    seen_gpk = set()
    for p in card:
        gpk = p.get("game_pk")
        if gpk and gpk not in seen_gpk:
            seen_gpk.add(gpk)
            home = p.get("home_team", p.get("away_team", "?"))
            away = p.get("away_team", p.get("home_team", "?"))
            lines.append(f"    {away} @ {home}: https://www.mlb.com/gameday/{gpk}")

    lines.append("")
    lines.append("  Factor Scores:")
    lines.append(f"  {'#':<3} {'Player':<22} {'Power':>7} {'Match':>7} {'Park':>7} {'Form':>7} {'Weath':>7} {'Lineup':>7}")
    lines.append(f"  {'-' * 68}")
    for i, p in enumerate(card, 1):
        lines.append(
            f"  {i:<3} {p['name']:<22} {p['power_score']:>6.1f} {p['matchup_score']:>6.1f} "
            f"{p['park_score']:>6.1f} {p['form_score']:>6.1f} {p['weather_score']:>6.1f} "
            f"{p.get('lineup_score', 0):>6.1f}"
        )

    lines.append("")
    lines.append("  Tier Pool Summary:")
    for tier in [1, 2, 3]:
        if tier in tier_details:
            d = tier_details[tier]
            tier_name = {1: "T1-Chalk", 2: "T2-Mid", 3: "T3-Longshot"}[tier]
            picked = tier_breakdown.get(tier_name, 0)
            lines.append(
                f"    {tier_name}: {d['pool_size']} scored | "
                f"{picked} selected | "
                f"top={d['top_scorer']} ({d['top_composite']:.1f})"
            )

    if mode != "live":
        lines.append("")
        lines.append("  NOTE: Running in OFFLINE mode with simulated matchups.")
        lines.append("  For live picks, run from a machine with internet access:")
        lines.append("    python generate_picks.py --date YYYY-MM-DD")
        lines.append("  Requires: pip install pybaseball pandas numpy requests")

    lines.append("=" * 76)
    return "\n".join(lines)


def format_full_board(full_board: list, card_names: set) -> str:
    """
    Pretty-print the full scored board — every batter for the day,
    ordered by weighted composite score. Marks selected picks with ★.
    """
    lines = []
    lines.append("")
    lines.append("=" * 110)
    lines.append(f"  FULL BOARD — All {len(full_board)} scored batters (sorted by composite)")
    lines.append("=" * 110)
    lines.append("")
    lines.append(
        f"  {'#':<4} {'':>2} {'Player':<24} {'Team':<5} {'Tier':<13} "
        f"{'vs Pitcher':<20} {'Venue':<26} {'Comp':>5} "
        f"{'Pwr':>5} {'Mtch':>5} {'Park':>5} {'Form':>5} {'Wthr':>5} {'LU':>5} {'Ord':>5}"
    )
    lines.append(f"  {'-' * 118}")

    for i, p in enumerate(full_board, 1):
        name = p.get("name", "?")
        star = "★" if name in card_names else " "
        opp = p.get("opp_pitcher", "?")
        if len(opp) > 18:
            opp = opp[:17] + "."
        venue = p.get("venue", "?")
        if len(venue) > 24:
            venue = venue[:23] + "."
        bo = p.get("batting_order", "?")
        bo_str = f"#{bo}" if isinstance(bo, int) else str(bo)[:5]
        lines.append(
            f"  {i:<4} {star:>2} {name:<24} {p.get('team', ''):<5} "
            f"{p.get('tier_label', ''):<13} {opp:<20} {venue:<26} "
            f"{p['composite']:>5.1f} "
            f"{p['power_score']:>5.1f} {p['matchup_score']:>5.1f} "
            f"{p['park_score']:>5.1f} {p['form_score']:>5.1f} {p['weather_score']:>5.1f} "
            f"{p.get('lineup_score', 0):>5.1f} {bo_str:>5}"
        )

    lines.append("=" * 110)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate daily HR parlay card (live or offline)"
    )
    parser.add_argument(
        "--date", default="today",
        help="Date (YYYY-MM-DD or 'today', default: today)"
    )
    parser.add_argument(
        "--picks", type=int, default=8,
        help="Number of picks (default: 8)"
    )
    parser.add_argument(
        "--offline", action="store_true",
        help="Force offline mode (skip live API calls)"
    )
    parser.add_argument(
        "--output", default=None,
        help="Output JSON path"
    )
    args = parser.parse_args()

    if args.date == "today":
        date_str = datetime.now().strftime("%Y-%m-%d")
    else:
        date_str = args.date

    n_picks = args.picks
    # combo is kept for compatibility but now just means "pick N total"
    combo = (n_picks, 0, 0)

    card, tier_details, mode, full_board, status = generate_card(date_str, combo, force_offline=args.offline)

    # Print data source status table FIRST
    print(status.format_table())

    output = format_card(card, date_str, combo, tier_details, mode)
    print(output)

    # Print the full board
    card_names = {p["name"] for p in card}
    board_output = format_full_board(full_board, card_names)
    print(board_output)

    # Save JSON
    if args.output:
        out_path = args.output
    else:
        results_dir = Path(__file__).parent.parent / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(results_dir / f"picks_{date_str}.json")

    pick_data = {
        "date": date_str,
        "mode": mode,
        "n_picks": len(card),
        "picks": [
            {
                "rank": i + 1,
                "name": p["name"],
                "player_id": p.get("player_id", 0),
                "team": p["team"],
                "bats": p["bats"],
                "tier": p.get("tier", 0),
                "tier_label": p.get("tier_label", ""),
                "venue": p["venue"],
                "opp_pitcher": p.get("opp_pitcher", ""),
                "opp_pitcher_id": p.get("opp_pitcher_id", 0),
                "composite": p["composite"],
                "power_score": p["power_score"],
                "matchup_score": p["matchup_score"],
                "matchup_version": p.get("matchup_version", "v1"),
                "park_score": p["park_score"],
                "form_score": p["form_score"],
                "weather_score": p["weather_score"],
                "lineup_score": p.get("lineup_score", 0),
                "batting_order": p.get("batting_order"),
                "tier_pts": p.get("tier_pts", 1),
                "game_pk": p.get("game_pk"),
                "gameday_url": f"https://www.mlb.com/gameday/{p.get('game_pk', '')}" if p.get("game_pk") else "",
                # Raw factor inputs that fed each score; consumed by load_picks_to_db
                # to populate pick_inputs for the per-factor decomposition charts.
                "inputs": p.get("inputs", {}),
            }
            for i, p in enumerate(card)
        ],
        "tier_details": {str(k): v for k, v in tier_details.items()},
        "full_board": [
            {
                "rank": i + 1,
                "name": p.get("name", ""),
                "player_id": p.get("player_id", 0),
                "team": p.get("team", ""),
                "tier": p.get("tier", 0),
                "tier_label": p.get("tier_label", ""),
                "venue": p.get("venue", ""),
                "opp_pitcher": p.get("opp_pitcher", ""),
                "opp_pitcher_id": p.get("opp_pitcher_id", 0),
                "composite": p.get("composite", 0),
                "power_score": p.get("power_score", 0),
                "matchup_score": p.get("matchup_score", 0),
                "matchup_version": p.get("matchup_version", "v1"),
                "park_score": p.get("park_score", 0),
                "form_score": p.get("form_score", 0),
                "weather_score": p.get("weather_score", 0),
                "batting_order": p.get("batting_order"),
                "game_pk": p.get("game_pk"),
                "selected": p.get("selected", False),
                "inputs": p.get("inputs", {}),
                "inputs": p.get("inputs", {}),
            }
            for i, p in enumerate(full_board)
        ],
        "generated_at": datetime.now().isoformat(),
    }
    with open(out_path, "w") as f:
        json.dump(pick_data, f, indent=2)
    print(f"\n  JSON saved to {out_path}")


if __name__ == "__main__":
    main()
