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
    LEAGUE_AVG_PITCHER,
    USE_CAREER_PRIOR,
    CAREER_PRIOR_K,
    compute_composite,
    select_top_picks,
    compute_slate_context,
    shrink_to_career,
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
    get_recent_pitcher_game_log,
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
# Parlay points by tier — higher number = longer shot.
# T4 added 2026-05-03: confirmed starters who didn't qualify for any
# season-stats tier (rookies, slow starts, returning IL). They're real
# starters but with thin track records, so they sit between T3 longshots
# and "way out there" — give them T3+3=12 to nudge above T3 in parlay
# scoring without pretending they're bottom-of-barrel longshots.
TIER_POINTS = {1: 1, 2: 3, 3: 9, 4: 12}

# Players to exclude from picks (long-term IL, season-ending injuries, etc.).
# Edit this list as needed — much easier than touching the tier data.
EXCLUDED_PLAYERS = {
    "Anthony Santander",  # torn ACL — out for 2026
    "Eli White",          # rarely in lineups — exclude from recommendations
}


# ---------------------------------------------------------------------------
# Season Statcast fallback — load season_batting from DB once per run
# ---------------------------------------------------------------------------

def load_season_batting_lookup(season: int) -> dict:
    """
    Load season_batting from the local DB into a dict keyed by player_id.
    Used as a fallback for Statcast-y metrics (barrel_pct, exit_velo,
    hr_fb_pct, iso) when the live tier batter dict has zero/missing values.

    The live tier dict comes from MLB Stats API splits and synthesizes
    these metrics from hr_per_pa — those estimates can swing wildly day
    to day. season_batting is refreshed nightly from the same API but
    stored as a stable point-in-time snapshot, plus future updates will
    populate it from FanGraphs for real Statcast values.

    Returns {} on any error so callers can degrade gracefully.
    """
    try:
        import sqlite3
        db_path = Path(__file__).parent.parent / "data" / "hr_bets.db"
        if not db_path.exists():
            return {}
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT player_id, player_name, games,
                   barrel_pct, exit_velo, hr_fb_pct, iso, woba
            FROM season_batting
            WHERE season = ?
            """,
            (season,),
        ).fetchall()
        conn.close()
        # 2026-05-04: added player_name + games to the SELECT.
        # PR #24's T4 display fix relied on sb_row["player_name"] to
        # promote "Black" → "Tyler Black" / "Cortes" → "Carlos Cortes",
        # but this helper only returned the rate stats. So the override
        # silently no-op'd and the rookie-class T4 batters still
        # rendered as last-name-only on today's noon run.
        # PR #28's platoon dampener also reads sb_row["games"], so we
        # pull it here in the same query.
        return {r["player_id"]: dict(r) for r in rows if r["player_id"]}
    except Exception as e:
        print(f"  [SEASON-BATTING] Could not load fallback ({e}) — continuing without it")
        return {}


def load_season_hr_lookup(date_str: str) -> dict[int, int]:
    """
    Cumulative season HR per batter through (but not including) `date_str`,
    sourced from `outcomes`.

    Why this exists: `score_power`'s SEASON_HR_FLOOR_TIERS lookup needs the
    batter's true season HR count. The live-tier path was reading `b["hr"]`
    from `_splits_to_batters`, which gets it from the MLB Stats API
    `byDateRange` endpoint — which **lags HR aggregation by ~3 days** even
    though the games count updates immediately. Direct API replay on
    2026-05-20 confirmed: Burger had 8 season HR per `outcomes` but the
    byDateRange endpoint returned 7 for him through 5/19. Floor fired as
    50 (5-HR tier) instead of 60 (8-HR tier). Same pattern hit Aranda,
    Dingler, and several 5-HR batters whose floor didn't fire at all.

    `outcomes` is authoritative because it's populated from `hr_events`
    (Statcast pitch-level) by the morning ETL — no API lag.

    Returns {} on any error so callers can degrade gracefully (the
    `score_power` fallback `batter.get("hr")` then kicks in, which is
    just the pre-B8 behavior).
    """
    try:
        import sqlite3
        db_path = Path(__file__).parent.parent / "data" / "hr_bets.db"
        if not db_path.exists():
            return {}
        season = int(date_str[:4])
        season_start = f"{season}-01-01"
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            """
            SELECT batter_id, COALESCE(SUM(hr_count), 0) AS season_hr
            FROM outcomes
            WHERE date >= ? AND date < ?
            GROUP BY batter_id
            """,
            (season_start, date_str),
        ).fetchall()
        conn.close()
        return {bid: int(hr) for bid, hr in rows if bid is not None and hr is not None}
    except Exception as e:
        print(f"  [SEASON-HR] Could not load outcomes-cumulative ({e}) — continuing without it")
        return {}


def load_rookie_pitcher_ids(pitcher_id_map: dict, threshold: int = 300) -> set[int]:
    """Identify pitchers with thin/no career Statcast data.

    A pitcher is "rookie" for our matchup-bonus purposes when their
    cumulative `pitcher_arsenals.total_pitches` across all seasons is
    below `threshold` (default 300 — covers their first 1-3 MLB starts).
    Pitchers with NO row in the table are also flagged rookie since
    that's the same signal: not enough big-league exposure for Savant
    to have classified them.

    Why this matters: a fresh callup like Trey Gibson facing the Yanks
    gets the LEAGUE_AVG_PITCHER fallback (1.2 HR/9, 4.0 ERA) when MLB
    Stats API doesn't yet have season data, which scores them as a
    middle-of-the-pack matchup. Reality: rookie pitchers get shelled
    historically. score_matchup adds a +15 bonus to the matchup score
    for batters facing a rookie-flagged pitcher (see ROOKIE_MATCHUP_BONUS
    in score_batters.py).

    pitcher_id_map: {pitcher_name: pitcher_id}
    Returns: set of pitcher_ids that should be flagged rookie.
    """
    if not pitcher_id_map:
        return set()
    try:
        import sqlite3
        db_path = Path(__file__).parent.parent / "data" / "hr_bets.db"
        if not db_path.exists():
            return set()
        conn = sqlite3.connect(str(db_path))
        ids = [pid for pid in pitcher_id_map.values() if pid]
        if not ids:
            conn.close()
            return set()
        # Single query: SUM(total_pitches) per pitcher across all seasons
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""SELECT pitcher_id, COALESCE(SUM(total_pitches), 0) AS career_p
                FROM pitcher_arsenals
                WHERE pitcher_id IN ({placeholders})
                GROUP BY pitcher_id""",
            ids,
        ).fetchall()
        conn.close()

        career_by_id = {r[0]: r[1] for r in rows}
        # IDs with no row -> 0 career pitches -> flagged rookie.
        # IDs with row but career_p < threshold -> flagged rookie.
        return {pid for pid in ids if career_by_id.get(pid, 0) < threshold}
    except Exception as e:
        print(f"  [ROOKIE] Could not load rookie pitcher set ({e}) — disabling penalty")
        return set()


def load_career_lookup() -> dict:
    """
    Load career_batting from the local DB into {player_id: career_dict}.
    Used by enrich_with_career_prior() when USE_CAREER_PRIOR is True.

    Returns {} on any error so the live path silently degrades to
    no-prior scoring rather than crashing the daily run.
    """
    try:
        import sqlite3
        db_path = Path(__file__).parent.parent / "data" / "hr_bets.db"
        if not db_path.exists():
            return {}
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT player_id, career_pa, career_hr, career_hr_per_pa,
                   career_avg, career_slg, career_obp, career_iso, career_woba,
                   seasons_played
            FROM career_batting
            WHERE career_pa IS NOT NULL AND career_pa > 0
            """,
        ).fetchall()
        conn.close()
        return {r["player_id"]: dict(r) for r in rows if r["player_id"]}
    except Exception as e:
        print(f"  [CAREER-PRIOR] Could not load career_batting ({e}) — disabling shrinkage")
        return {}


def enrich_with_career_prior(batter: dict, career_lookup: dict, k: int = CAREER_PRIOR_K) -> dict:
    """
    Bayesian-shrink current-season power inputs toward the player's
    career rate. Mirrors enrich_with_season_batting in shape but pulls
    rather than fills — current values are reduced/increased toward
    career mean rather than replaced when missing.

    Only applies when:
      - USE_CAREER_PRIOR is True (in score_batters.py)
      - The batter has a row in career_batting
      - The current sample has measurable PA (so the weighted average
        is well-defined)

    Stamps `_career_shrunk: True` and `_career_pa: <career PA>` on the
    batter dict so downstream diagnostics can flag and audit shrunk rows.

    Note: `barrel_pct` shrinkage uses the synthetic
    `career_hr_per_pa × 200` proxy because Statcast barrel% isn't
    captured in career_batting yet (would require a separate Savant
    pull — flagged in sync_career_batting.py docstring as future work).
    """
    # Read flag from score_batters module dynamically. `from score_batters
    # import USE_CAREER_PRIOR` would freeze the value at import time, so
    # flipping the flag at runtime (e.g., backtest harness) wouldn't take
    # effect. Inline import is cheap (Python caches modules).
    import score_batters as _sb
    if not _sb.USE_CAREER_PRIOR:
        return batter

    pid = batter.get("player_id")
    if not pid or pid not in career_lookup:
        return batter

    career = career_lookup[pid]
    current_pa = batter.get("pa") or batter.get("ab") or 0
    if current_pa <= 0:
        return batter

    shrunk_count = 0

    # Direct rate-stat shrinkage. career_batting carries iso + woba
    # (the latter is the same 0.7*OBP + 0.3*SLG proxy as season_batting).
    for cur_key, career_key in [
        ("iso", "career_iso"),
        ("woba", "career_woba"),
    ]:
        cur_val = batter.get(cur_key)
        cv = career.get(career_key)
        if cur_val is not None and cur_val > 0 and cv is not None and cv > 0:
            shrunk = shrink_to_career(cur_val, current_pa, cv, k=k)
            if shrunk is not None and shrunk != cur_val:
                batter[cur_key] = round(shrunk, 4)
                shrunk_count += 1

    # Synthetic Statcast proxy: career_hr_per_pa × 200 ≈ barrel%
    # (mirrors the formula in fetch_daily_data._splits_to_batters).
    # Only apply when career sample is meaningful (≥ 1000 PA, ~2 full
    # seasons) so the proxy isn't noisy for cup-of-coffee veterans.
    cv_hr_per_pa = career.get("career_hr_per_pa")
    barrel_shrunk = False
    if cv_hr_per_pa and career.get("career_pa", 0) >= 1000:
        career_barrel_proxy = min(25.0, cv_hr_per_pa * 200)
        cur_barrel = batter.get("barrel_pct")
        if cur_barrel is not None and cur_barrel > 0:
            shrunk = shrink_to_career(cur_barrel, current_pa, career_barrel_proxy, k=k)
            if shrunk is not None and shrunk != cur_barrel:
                batter["barrel_pct"] = round(shrunk, 1)
                shrunk_count += 1
                barrel_shrunk = True

    if shrunk_count:
        batter["_career_shrunk"] = True
        batter["_career_pa"] = career.get("career_pa")
        batter["_career_shrunk_count"] = shrunk_count
    if barrel_shrunk:
        # Provenance: shrinkage is a transformation of whatever source the
        # current barrel_pct already had. Track the final mutator for the
        # column so refit/audit can filter on it.
        batter["_barrel_pct_source"] = "career_shrunk"

    return batter


def enrich_with_season_batting(batter: dict, season_lookup: dict) -> dict:
    """
    Replace zero/missing power metrics on *batter* with values from
    season_batting. Real, non-zero values on *batter* are kept as-is.

    When `barrel_pct` specifically gets overwritten, stamp
    `_barrel_pct_source = "season_batting_fallback"` so the per-row
    provenance column on pick_inputs reflects the actual data path
    (added 2026-05-03; previously only set the broader `_power_source`).
    """
    pid = batter.get("player_id")
    if not pid or pid not in season_lookup:
        return batter
    sb = season_lookup[pid]
    enriched = False
    barrel_overwritten = False
    for metric in ("barrel_pct", "exit_velo", "hr_fb_pct", "iso", "woba"):
        cur = batter.get(metric)
        sb_val = sb.get(metric)
        if (cur is None or cur == 0) and sb_val is not None and sb_val > 0:
            batter[metric] = sb_val
            enriched = True
            if metric == "barrel_pct":
                barrel_overwritten = True
    if enriched:
        # Mark provenance so downstream diagnostics can flag this row
        batter["_power_source"] = "season_batting_fallback"
    if barrel_overwritten:
        batter["_barrel_pct_source"] = "season_batting_fallback"
    return batter


# Map team abbreviations (from MLB Stats API) to full names (from schedule)
#
# IMPORTANT: this map's full-name VALUES must match what the MLB Stats
# API returns in `team.name`, because score_live_slate uses them as keys
# in `team_to_game` and reverses via TEAM_FULL_TO_ABBREV. A mismatch
# silently drops every batter on that team — they fail the
# `team_to_game.get(batter_team)` lookup. The 2026-05-02 audit caught
# this for the Athletics: API renamed from "Oakland Athletics" to just
# "Athletics" after the Sacramento move, but our map still pointed at
# the old name. Result: A's batters never made the scored pool —
# 1 selection in 23 days, 0/29 teams on today's full board.
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
    "NYY": "New York Yankees", "OAK": "Athletics",
    "PHI": "Philadelphia Phillies", "PIT": "Pittsburgh Pirates",
    "SD": "San Diego Padres", "SF": "San Francisco Giants",
    "SEA": "Seattle Mariners", "STL": "St. Louis Cardinals",
    "TB": "Tampa Bay Rays", "TEX": "Texas Rangers",
    "TOR": "Toronto Blue Jays", "WSH": "Washington Nationals",
}
# Reverse lookup: full name → abbreviation. Aliases below preserve
# resolution for historical strings ("Oakland Athletics" still appears
# in older daily_picks rows, raw_data*.csv, and a few test fixtures).
TEAM_FULL_TO_ABBREV = {v: k for k, v in TEAM_ABBREV_TO_FULL.items()}
TEAM_FULL_TO_ABBREV["Oakland Athletics"] = "OAK"  # backward-compat alias

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
        log = get_recent_game_log(pid, season)
        if log:
            results[pid] = log
    return results


def fetch_pitcher_recent_form_batch(
    pitcher_ids: list[tuple[str, int]],
    season: int,
    today_str: str | None = None,
    days: int = 21,
) -> dict:
    """
    Bulk-fetch rolling 21-day HR/9 (and IP / start counts) for each pitcher
    in today's slate. Mirrors fetch_form_data_batch() but for pitchers.

    Added 2026-05-13. Closes the "pitcher recency" gap that left
    score_pitcher_vulnerability blind to pitchers whose last 3-4 starts
    were trending much worse (or better) than their season aggregate.

    *pitcher_ids* is a list of (name, pitcher_id) tuples.
    *today_str* is the date the picks are being generated for; the window
    is [today - days, today), excluding today's game itself (we're scoring
    games yet to be played).

    Returns {pitcher_id: {recent_hr_count, recent_ip, recent_starts,
    recent_hr_per_9}}. Missing pitchers (API failure, no recent gameLog,
    or <1 IP in window) are simply absent from the dict.
    """
    results = {}
    for name, pid in pitcher_ids:
        if not pid or pid < 1000:
            continue
        log = get_recent_pitcher_game_log(pid, season, today_str=today_str, days=days)
        if log:
            results[pid] = log
    return results


def try_fetch_pitcher_season_stats(pitcher_name: str, season: int) -> dict:
    """Try to get pitcher season stats from FanGraphs. Returns dict or empty.

    DEPRECATED — FanGraphs now blocks automated requests via Cloudflare.
    Kept as fallback but fetch_pitcher_stats_mlb() should be used instead.

    Audit MED fix: was using `row.get(col, league_mean)` for every column,
    silently injecting league means with no provenance flag when FanGraphs
    columns were missing. Now reads with `.get(col)` (None default) and
    skips fields not measured. Caller can `.get(field)` defensively or
    union with `LEAGUE_AVG_PITCHER` if it really wants every field.
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
        out = {"name": pitcher_name, "_source": "fangraphs"}
        # Only include keys that were actually measured. Downstream
        # scoring functions (post-HIGH #3 fix) skip on None/0 anyway.
        for src, dst in [
            ("HR/9",     "hr_per_9"),
            ("ERA",      "era"),
            ("HardHit%", "hard_hit_pct_allowed"),
            ("K/9",      "k_per_9"),
        ]:
            v = row.get(src)
            if v is not None and v == v:   # second check: not NaN
                out[dst] = float(v)
        out["throws"] = row.get("Throws", "R") if "Throws" in row.index else "R"
        return out
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

    # 2026-05-05: filter out games that won't actually be played today.
    # Three statuses indicate "no game":
    #   - Postponed:  rescheduled to a different date (most common — rain)
    #   - Cancelled:  scrubbed entirely (rare but happens)
    #   - Suspended:  game started, won't resume today (rain/weather)
    # Without this filter, generate_picks scores batters in the postponed
    # game against the probable pitchers (who aren't pitching tonight).
    # PR #33's recent-lineup fallback compounds the issue by injecting
    # YESTERDAY's lineup for those teams, so e.g. Soto/Lindor end up on
    # the pool against a phantom matchup. They could land on the 8-pick
    # card for a game that doesn't exist.
    #
    # etl_outcomes already filters on detailedState IN (Final, Game Over)
    # so the metric side is safe — these batters just register as 0/0.
    # But the SCORING side wasn't filtering, so picks could be poisoned.
    EXCLUDED_GAME_STATUSES = {"Postponed", "Cancelled", "Suspended"}
    excluded = [g for g in games if g.get("status") in EXCLUDED_GAME_STATUSES]
    games = [g for g in games if g.get("status") not in EXCLUDED_GAME_STATUSES]

    if excluded:
        labels = ", ".join(
            f"{g['away_team']}@{g['home_team']} ({g['status']})" for g in excluded
        )
        status.warn("MLB Schedule API",
                    f"{len(games)} games found ({len(excluded)} skipped: {labels})")
        print(f"  [LIVE] Skipping {len(excluded)} non-playing game(s): {labels}")
    elif not games:
        status.fail("MLB Schedule API",
                    f"All {len(excluded)} games postponed/cancelled — no slate today")
        return None
    else:
        status.ok("MLB Schedule API", f"{len(games)} games found")

    if not games:
        # Belt-and-suspenders: should be unreachable given the elif above,
        # but if every game on the calendar is postponed (extreme weather
        # day, league-wide cancellation, etc.) we'd land here.
        status.fail("MLB Schedule API", "No playable games today")
        return None

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
                # Audit MED: was inline league-mean dict; now refs the
                # shared constant so provenance flag (`_source`) flows
                # through and refit_weights / diagnostics can filter.
                pitcher_lookup[pname] = dict(LEAGUE_AVG_PITCHER, name=pname)
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

    # ── Rookie pitcher detection ──────────────────────────────────────
    # Tag pitchers with < 300 career Statcast pitches (or no arsenal row)
    # as rookies. score_matchup applies a +15 bonus to batters facing them
    # since LEAGUE_AVG_PITCHER defaults score rookies as middling matchups,
    # whereas the Aaron Judge vs Trey Gibson type spot is a HUGE edge.
    rookie_pitcher_ids = load_rookie_pitcher_ids(pitcher_id_map)
    if rookie_pitcher_ids:
        rookie_names = [n for n, pid in pitcher_id_map.items() if pid in rookie_pitcher_ids]
        status.ok("Rookie Pitcher Tag", f"{len(rookie_pitcher_ids)} flagged: {', '.join(rookie_names[:5])}{'...' if len(rookie_names) > 5 else ''}")
        # Stamp `is_rookie=True` on the pitcher dicts so score_matchup picks
        # it up via pitcher.get("is_rookie").
        for pname, pid in pitcher_id_map.items():
            if pid in rookie_pitcher_ids and pname in pitcher_lookup:
                pitcher_lookup[pname]["is_rookie"] = True

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

    # ── Pitcher recency: rolling 21-day HR/9 via MLB API gameLog ───────
    # Closes the "season HR/9 lags 3-4 bad starts" gap. score_pitcher_
    # vulnerability and compute_slate_context now blend recent + season
    # HR/9 (60/40) when recent_starts_21d >= 2. Singer (2026-05-12)
    # case: recent HR/9 3.07 vs season 1.89 → blended 2.60, lifts him
    # past Mikolas on the slate vulnerability rank.
    try:
        pitcher_ids_for_recency = [(n, pid) for n, pid in pitcher_id_map.items() if pid]
        bulk_pitcher_recency = fetch_pitcher_recent_form_batch(
            pitcher_ids_for_recency, season, today_str=date_str, days=21
        )
        n_with_recency = 0
        for pname, pid in pitcher_id_map.items():
            if pid in bulk_pitcher_recency and pname in pitcher_lookup:
                log = bulk_pitcher_recency[pid]
                pitcher_lookup[pname]["recent_hr9_21d"]       = log.get("recent_hr_per_9")
                pitcher_lookup[pname]["recent_hr_count_21d"]  = log.get("recent_hr_count")
                pitcher_lookup[pname]["recent_starts_21d"]    = log.get("recent_starts")
                pitcher_lookup[pname]["recent_ip_21d"]        = log.get("recent_ip")
                if log.get("recent_hr_per_9") is not None:
                    n_with_recency += 1
        if n_with_recency > 0:
            status.ok(
                "Pitcher Recency (21d HR/9)",
                f"{n_with_recency}/{len(pitcher_lookup)} via MLB gameLog",
            )
        else:
            status.warn(
                "Pitcher Recency (21d HR/9)",
                "0 starters — vulnerability uses season-only HR/9",
            )
    except Exception as e:
        status.warn("Pitcher Recency (21d HR/9)", f"Bulk fetch failed: {e}")

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
    # 2026-05-04: per-game-side lineup_source so the inputs_snapshot can
    # tag each batter with where their batting_order came from (tier-1
    # "posted" / tier-2 "recent:YYYY-MM-DD" / tier-3 "roster_fallback").
    # Tracked by side (not per-player) since all 9 spots in a side share
    # the same provenance — they came from the same lineup payload.
    lineup_source_by_side: dict[int, dict[str, str]] = {}  # game_pk -> {"home": src, "away": src}

    for gpk, lu in slate.get("lineups", {}).items():
        ids = set()
        names = set()
        order_by_id = {}
        order_by_name = {}
        bench_ids = set()
        bench_names = set()
        side_source: dict[str, str] = {}
        for side in ["home", "away"]:
            side_players = lu.get(side, [])
            # Capture lineup_source from the first player with the field
            # set; PR #33 stamps the same source on all players from a
            # given tier-1/tier-2/tier-3 fetch, so any one is canonical.
            for p in side_players:
                src = p.get("lineup_source")
                if src:
                    side_source[side] = src
                    break
            for i, p in enumerate(side_players, 1):
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
        lineup_source_by_side[gpk] = side_source
        slate.setdefault("_bench_ids", {})[gpk] = bench_ids
        slate.setdefault("_bench_names", {})[gpk] = bench_names

    all_scored = []
    season = int(date_str[:4])

    # Load season_batting fallback once per scoring pass.
    season_lookup = load_season_batting_lookup(season)
    if season_lookup:
        print(f"  [SEASON-BATTING] Loaded {len(season_lookup)} rows for season {season} fallback")

    # B8 (2026-05-20): outcomes-cumulative season HR per batter, used by
    # score_power's HR-floor lookup. Replaces the MLB-API-derived b["hr"]
    # which lags actuals by ~3 days. See load_season_hr_lookup docstring.
    season_hr_lookup = load_season_hr_lookup(date_str)
    if season_hr_lookup:
        print(f"  [SEASON-HR] Loaded outcomes-cumulative HR for {len(season_hr_lookup)} batters through {date_str}")

    # Career-prior shrinkage data (only loaded when the feature flag is on).
    # When off (default), this stays empty and enrich_with_career_prior
    # short-circuits so the active code path is identical to before.
    import score_batters as _sb
    career_lookup = load_career_lookup() if _sb.USE_CAREER_PRIOR else {}
    if career_lookup:
        print(f"  [CAREER-PRIOR] Loaded {len(career_lookup)} career rows; k={_sb.CAREER_PRIOR_K}")

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

        # 2026-05-04: tag the batter with the lineup_source for the side
        # they're on. PR #33 added the source flag at fetch time; this
        # threads it onto the batter dict so compute_composite's
        # inputs_snapshot can persist it. Determines side via team match
        # against the game's home/away teams (with abbreviation fallback
        # mirroring the same lookup we already do above for `team_to_game`).
        b_team = b.get("team", "")
        home_team = game.get("home_team", "")
        away_team = game.get("away_team", "")
        if b_team == home_team or TEAM_ABBREV_TO_FULL.get(b_team) == home_team:
            side = "home"
        elif b_team == away_team or TEAM_ABBREV_TO_FULL.get(b_team) == away_team:
            side = "away"
        else:
            side = None
        if side:
            b["_lineup_source"] = lineup_source_by_side.get(gpk, {}).get(side)

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

        opp = slate["pitchers"].get(opp_pitcher_name) or dict(
            LEAGUE_AVG_PITCHER, name=opp_pitcher_name or "league_avg"
        )

        weather = slate["weather"].get(gpk, {
            "temperature_f": 75, "wind_mph": 5, "wind_direction_deg": 0, "dome": False
        })

        # Recent form — split game-count windows from get_recent_game_log.
        # Missing keys (no game log) -> None -> score_form skips them.
        log = game_logs.get(player_id, {})

        # Enrich the live tier dict with season_batting fallback BEFORE
        # building the scoring entry — overwrites zero/missing barrel%,
        # exit_velo, hr_fb_pct, iso, woba with real season-level values.
        # Live tier estimates are noisy (synthesized from hr_per_pa + slg)
        # and a player with a low day-of estimate would otherwise score
        # power=13 even with elite real Statcast inputs.
        if season_lookup:
            b = enrich_with_season_batting(dict(b), season_lookup)
        # Career-prior shrinkage runs AFTER season-batting fallback so the
        # current values fed in are the best-available (real Statcast >
        # season splits > synthetic estimate). No-op when USE_CAREER_PRIOR
        # is False (default) — same as if this line weren't here.
        if career_lookup:
            b = enrich_with_career_prior(b, career_lookup)

        entry = {
            **b,
            "player_id": player_id,
            "recent_hr_10g": log.get("recent_hr_10g"),
            "recent_iso_30g": log.get("recent_iso_30g"),
            "recent_avg_30g": log.get("recent_avg_30g"),
            "recent_window_days": log.get("recent_window_days"),
            # ev_trend: Phase 2 — populated by the nightly Statcast ETL.
            "ev_trend": b.get("ev_trend"),
            # B8 (2026-05-20): outcomes-cumulative season HR — authoritative
            # source for score_power's SEASON_HR_FLOOR_TIERS lookup. Replaces
            # the MLB-API-lagged b["hr"]. 0 when batter has no outcomes rows
            # (true rookies, etc.) which correctly skips the floor.
            "season_hr": season_hr_lookup.get(player_id, 0),
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


def score_untiered_starters(
    slate: dict,
    date_str: str,
    config_name: str,
    pf,
    slate_ctx: dict | None,
    already_scored_pids: set,
) -> list:
    """Score confirmed starters who didn't qualify for any tier.

    The 3 tier passes in score_live_slate iterate `tier_batters` per tier,
    and `build_live_tiers` qualifies on `games >= 5 AND hr >= 1`. Real
    confirmed starters who don't meet that bar (low-HR contact guys,
    rookies, returning IL players, slow starts with <5 games) silently
    never enter the scored pool. This pass scoops them up so today's
    `daily_picks` reflects the actual 9 starters per game, not just the
    qualifying subset.

    Stats default to None and skip-on-missing through compute_composite;
    enrich_with_season_batting fills barrel/EV/iso/woba from the
    `season_batting` DB table when present (so a known batter who just
    happens to be off the tier table still gets real stats).

    Originating bug: 2026-05-02 SEA/KC autopsy showed 5 of 9 SEA starters
    (Crawford, J. Rodriguez, Arozarena, Joe, Rivas) absent from
    `daily_picks` for 2026-05-01 — they had been silently dropped at the
    tier-filter step before scoring even began. Tagged tier=4
    ("T4-Untiered") so the dashboard can distinguish from T1/T2/T3 picks.
    """
    untiered = []
    season = int(date_str[:4])
    season_lookup = load_season_batting_lookup(season) or {}
    # B8 (2026-05-20): outcomes-cumulative HR for the floor lookup.
    # Without this, T4 batters never had `hr`/`season_hr` set at all and
    # the floor never fired (Kurtz 8 HR scored 33.5, Rooker 7 HR scored
    # 26.3 on 2026-05-20). Setting season_hr from outcomes lets the floor
    # apply to T4 the same way it does to T1/T2/T3.
    season_hr_lookup = load_season_hr_lookup(date_str)
    # Career-prior shrinkage data (no-op when USE_CAREER_PRIOR is False).
    import score_batters as _sb
    career_lookup = load_career_lookup() if _sb.USE_CAREER_PRIOR else {}
    pitcher_profiles = slate.get("pitcher_profiles", {})

    # Build {gpk: game} once.
    game_by_gpk = {g["game_pk"]: g for g in slate["games"]}

    # Gather stub batters first so we can batch-fetch game logs for them
    # (mirrors score_live_slate's batch optimization — saves N HTTPs).
    stubs: list[tuple[dict, dict, int, str]] = []
    for gpk, lu in slate.get("lineups", {}).items():
        game = game_by_gpk.get(gpk)
        if not game:
            continue
        for side in ["home", "away"]:
            for i, p in enumerate(lu.get(side, []), 1):
                if i > 9:
                    break  # bench; only score confirmed starters
                pid = p.get("player_id")
                pname = p.get("name", "")
                if not pid or pid in already_scored_pids:
                    continue
                if pname in EXCLUDED_PLAYERS:
                    continue

                # 2026-05-03 fix: the bdfed lineup endpoint only returns
                # boxscoreName ("Cortes", "Correa", "Black") — not fullName.
                # T1/T2/T3 batters get "Carlos Cortes" because they flow
                # through _splits_to_batters which uses fullName. T4
                # batters were inheriting the boxscoreName, which then
                # showed in dropdowns/cards as last-name-only ugly stubs.
                # Override with season_batting/career_batting fullName when
                # we have it (we usually do — even rookies have a row by
                # mid-season).
                full_name = pname
                sb_row = season_lookup.get(pid) if season_lookup else None
                if sb_row and sb_row.get("player_name"):
                    full_name = sb_row["player_name"]
                elif career_lookup and pid in career_lookup:
                    full_name = career_lookup[pid].get("player_name") or pname

                # 2026-05-03 fix: game["home_team"] / ["away_team"] are
                # FULL team names ("Athletics", "Milwaukee Brewers"). T1/2/3
                # batters get the abbreviation via _splits_to_batters; T4
                # was getting the full name, which polluted the team
                # dropdown and the team column in dashboards. Convert via
                # TEAM_FULL_TO_ABBREV; fall back to full name if unmapped
                # (preserves current behavior for any new/renamed team).
                full_team = game["home_team"] if side == "home" else game["away_team"]
                team = TEAM_FULL_TO_ABBREV.get(full_team, full_team)

                stub = {
                    "name": full_name,
                    "team": team,
                    "player_id": pid,
                    # 2026-05-04: lineup_source flows through here too so
                    # T4 picks get the same provenance flag as T1/T2/T3
                    # (the source comes from the same lineup payload p
                    # that we're enumerating).
                    "_lineup_source": p.get("lineup_source"),
                    # B8 (2026-05-20): outcomes-cumulative season HR. 0 for
                    # true rookies (no prior outcomes rows). The floor in
                    # score_power skips when season_hr <= 0, so 0-default
                    # correctly no-ops for rookies; for real T4 starters
                    # with HRs, the floor now fires.
                    "season_hr": season_hr_lookup.get(pid, 0),
                }
                if season_lookup:
                    stub = enrich_with_season_batting(stub, season_lookup)
                    # 2026-05-04 platoon dampener: attach `games` from
                    # season_batting so compute_composite can compute
                    # play_rate. Without this, T4 batters would always read
                    # `games`=None and skip dampening — exactly the cohort
                    # most likely to be platoon hitters.
                    sb_row = season_lookup.get(pid) or {}
                    if sb_row.get("games") is not None:
                        stub["games"] = sb_row["games"]
                if career_lookup:
                    stub = enrich_with_career_prior(stub, career_lookup)
                stubs.append((stub, game, i, side))

    if not stubs:
        return untiered

    print(f"  [UNTIERED] {len(stubs)} confirmed starters not in any tier — "
          f"scoring with season_batting fallback")

    # Batch fetch game logs (recent form data)
    player_id_list = [(s[0]["name"], s[0]["player_id"]) for s in stubs]
    try:
        game_logs = fetch_form_data_batch(player_id_list, season)
    except Exception as e:
        print(f"  [UNTIERED] form data batch failed: {e}")
        game_logs = {}

    # Reuse the slate-level bulk xwOBA pull
    bulk_xwoba = slate.get("bulk_batter_xwoba", {})
    batter_adv = {pid: {"xwoba_contact": v} for pid, v in bulk_xwoba.items()}

    for stub, game, batting_order, side in stubs:
        gpk = game["game_pk"]
        ht = game["home_team"]
        at = game["away_team"]
        venue = game["venue"]
        pid = stub["player_id"]

        # Opposing pitcher
        opp_pitcher_name = (
            game.get("away_pitcher_name", "TBD") if side == "home"
            else game.get("home_pitcher_name", "TBD")
        )
        opp = slate["pitchers"].get(opp_pitcher_name) or dict(
            LEAGUE_AVG_PITCHER, name=opp_pitcher_name or "league_avg"
        )
        weather = slate["weather"].get(gpk, {
            "temperature_f": 75, "wind_mph": 5, "wind_direction_deg": 0, "dome": False
        })

        log = game_logs.get(pid, {})
        entry = {
            **stub,
            "recent_hr_10g": log.get("recent_hr_10g"),
            "recent_iso_30g": log.get("recent_iso_30g"),
            "recent_avg_30g": log.get("recent_avg_30g"),
            "recent_window_days": log.get("recent_window_days"),
            "ev_trend": stub.get("ev_trend"),
        }
        adv = batter_adv.get(pid, {})
        if adv.get("xwoba_contact") is not None:
            entry["xwoba_contact"] = adv["xwoba_contact"]

        pp = pitcher_profiles.get(opp_pitcher_name)

        result = compute_composite(
            entry, opp, venue, weather, pf, config_name,
            victim_profile=None,  # No archetype profile for untiered;
                                  # would require an extra DB query that
                                  # isn't worth it for the bottom of the board.
            pitcher_profile=pp,
            batting_order=batting_order,
            slate_ctx=slate_ctx,
            game_pk=gpk,
        )
        result["player_id"] = pid
        result["game_pk"] = gpk
        result["opp_pitcher"] = opp_pitcher_name
        result["tier"] = 4
        result["game_venue"] = venue
        result["home_team"] = ht
        result["away_team"] = at
        result["confirmed_starter"] = True
        result["has_game_log"] = bool(log)
        result["hot_streak"] = log.get("hot_streak", False) if log else False
        result["has_archetype"] = False
        untiered.append(result)

    untiered.sort(key=lambda x: x["composite"], reverse=True)
    return untiered


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
            opp = pitcher_lookup.get(opp_name) or dict(
                LEAGUE_AVG_PITCHER, name=opp_name or "league_avg"
            )
            for i, b in enumerate(lineup, 1):
                # Simulate batting order: sorted roughly by quality + noise
                batting_order = min(9, max(1, i + int(rng.integers(-1, 2))))
                entry = {
                    **b,
                    "player_id": hash(b["name"]) % 1_000_000,
                    "recent_hr_10g": float(
                        min(5, max(0, b["hr"] / 25 + rng.normal(0, 0.8)))
                    ),
                    "recent_iso_30g": float(
                        max(0.05, b.get("iso", 0.150) + rng.normal(0, 0.03))
                    ),
                    "recent_avg_30g": float(
                        max(0.15, b.get("avg", 0.250) + rng.normal(0, 0.025))
                    ),
                    "recent_window_days": 38,
                    "ev_trend": None,
                    # B8 (2026-05-20): offline sim doesn't have an outcomes
                    # table to compute outcomes-cumulative HR from, so use
                    # b["hr"] directly. b["hr"] in offline mode IS the
                    # ground-truth season HR (sourced from mlb_2025_tiers),
                    # without the MLB-API lag that affects the live path.
                    "season_hr": int(b.get("hr", 0) or 0),
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
            # 2026-05-04 platoon dampener: stash the max season-games count
            # on slate_ctx so compute_composite can compute play_rate per
            # batter (games / max_games) and apply a [0.90, 1.0] multiplier.
            # Daily starters (max_games) take no haircut; platoon hitters
            # slip a few ranks. The "max" is the highest games count across
            # T1/T2/T3 tier pools — same benchmark used by T4 untiered
            # batters via slate_ctx so all paths agree.
            tiers_dict = live_slate.get("live_tiers") or {}
            all_batters = []
            for tier_num in (1, 2, 3):
                all_batters.extend(tiers_dict.get(tier_num) or [])
            max_games = max((b.get("games") or 0 for b in all_batters), default=0)
            slate_ctx["max_games"] = max_games

            n_parks = len(slate_ctx.get("park_pct", {}))
            n_weather = len(slate_ctx.get("weather_pct", {}))
            n_pitchers = len(slate_ctx.get("pitcher_pct", {}))
            n_totals = len(slate_ctx.get("team_total_pct", {}))
            status.ok(
                "Slate-Rank Context",
                f"{n_parks} venues, {n_weather} games, {n_pitchers} pitchers, {n_totals} team totals, max_g={max_games}",
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

    # T4 (untiered): scoop up confirmed starters who didn't qualify for any
    # of T1/T2/T3. Without this, real #1–9 hitters with <5 games or 0 HRs
    # YTD never enter daily_picks even though they're literally batting
    # tonight. See score_untiered_starters() docstring for the SEA/KC
    # autopsy that surfaced this. Skip in offline mode (no real lineups).
    if live_slate:
        already_scored_pids = {s.get("player_id") for s in full_board if s.get("player_id")}
        untiered = score_untiered_starters(
            live_slate, date_str, config, pf, slate_ctx, already_scored_pids
        )
        for s in untiered:
            s["tier_label"] = "T4-Untiered"
        full_board.extend(untiered)
        if untiered:
            tier_details[4] = {
                "config": config,
                "pool_size": len(untiered),
                "top_scorer": untiered[0]["name"],
                "top_composite": untiered[0]["composite"],
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
    # 2026-05-03 fix: was `TIER_POINTS[c.get('tier', 1)]` — KeyError when
    # tier=4 (T4-Untiered, added by score_untiered_starters). TIER_POINTS
    # now has a 4 entry, but keep the .get() fallback so any future tier
    # additions don't crash card formatting.
    lines.append(f"  Max pts if all hit: "
                 f"{sum(TIER_POINTS.get(c.get('tier', 1), 9) for c in card)}")
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
