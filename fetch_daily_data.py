#!/usr/bin/env python3
"""
fetch_daily_data.py — Data collection for Daily HR Bet skill.

Pulls game schedules, lineups, pitcher data, batter stats, park factors,
and weather from MLB Stats API, pybaseball (Statcast/FanGraphs), and Open-Meteo.

Usage:
    python fetch_daily_data.py --date today
    python fetch_daily_data.py --date 2024-07-15
    python fetch_daily_data.py --season 2024  # bulk fetch for backtesting
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

import requests

# pybaseball imports
try:
    import pybaseball
    from pybaseball import (
        statcast,
        statcast_batter,
        batting_stats,
        pitching_stats,
        cache,
    )
    cache.enable()
except ImportError:
    print("ERROR: pybaseball not installed. Run: pip install pybaseball --break-system-packages")
    sys.exit(1)

import pandas as pd
import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MLB_STATS_API = "https://statsapi.mlb.com/api/v1"
BDFED_MATCHUP_API = "https://bdfed.stitch.mlbinfra.com/bdfed/matchup"

# Venue ID → name mapping for dome detection
DOME_STADIUMS = {
    "Tropicana Field", "Chase Field", "American Family Field",
    "Minute Maid Park", "Globe Life Field", "Rogers Centre",
    "T-Mobile Park", "loanDepot park",
}

# Venue coordinates for weather lookups
VENUE_COORDS = {
    "Angel Stadium": (33.8003, -117.8827),
    "Busch Stadium": (38.6226, -90.1928),
    "Chase Field": (33.4453, -112.0667),
    "Citi Field": (40.7571, -73.8458),
    "Citizens Bank Park": (39.9061, -75.1665),
    "Comerica Park": (42.3390, -83.0485),
    "Coors Field": (39.7559, -104.9942),
    "Dodger Stadium": (34.0739, -118.2400),
    "Fenway Park": (42.3467, -71.0972),
    "Globe Life Field": (32.7473, -97.0845),
    "Great American Ball Park": (39.0975, -84.5069),
    "Guaranteed Rate Field": (41.8299, -87.6338),
    "Kauffman Stadium": (39.0517, -94.4803),
    "loanDepot park": (25.7781, -80.2197),
    "Minute Maid Park": (29.7573, -95.3555),
    "Nationals Park": (38.8730, -77.0074),
    "Oakland Coliseum": (37.7516, -122.2005),
    "Oracle Park": (37.7786, -122.3893),
    "Oriole Park at Camden Yards": (39.2838, -76.6216),
    "Petco Park": (32.7076, -117.1570),
    "PNC Park": (40.4469, -80.0058),
    "Progressive Field": (41.4962, -81.6852),
    "Rogers Centre": (43.6414, -79.3894),
    "T-Mobile Park": (47.5914, -122.3325),
    "Target Field": (44.9818, -93.2775),
    "Tropicana Field": (27.7682, -82.6534),
    "Truist Park": (33.8908, -84.4678),
    "Wrigley Field": (41.9484, -87.6553),
    "Yankee Stadium": (40.8296, -73.9262),
    "American Family Field": (43.0280, -87.9712),
}

# Venue → IANA timezone (used for weather hour lookups)
VENUE_TZ = {
    "Angel Stadium": "America/Los_Angeles",
    "Busch Stadium": "America/Chicago",
    "Chase Field": "America/Phoenix",
    "Citi Field": "America/New_York",
    "Citizens Bank Park": "America/New_York",
    "Comerica Park": "America/Detroit",
    "Coors Field": "America/Denver",
    "Dodger Stadium": "America/Los_Angeles",
    "Fenway Park": "America/New_York",
    "Globe Life Field": "America/Chicago",
    "Great American Ball Park": "America/New_York",
    "Guaranteed Rate Field": "America/Chicago",
    "Kauffman Stadium": "America/Chicago",
    "loanDepot park": "America/New_York",
    "Minute Maid Park": "America/Chicago",
    "Nationals Park": "America/New_York",
    "Oakland Coliseum": "America/Los_Angeles",
    "Oracle Park": "America/Los_Angeles",
    "Oriole Park at Camden Yards": "America/New_York",
    "Petco Park": "America/Los_Angeles",
    "PNC Park": "America/New_York",
    "Progressive Field": "America/New_York",
    "Rogers Centre": "America/Toronto",
    "T-Mobile Park": "America/Los_Angeles",
    "Target Field": "America/Chicago",
    "Tropicana Field": "America/New_York",
    "Truist Park": "America/New_York",
    "Wrigley Field": "America/Chicago",
    "Yankee Stadium": "America/New_York",
    "American Family Field": "America/Chicago",
}

DATA_DIR = Path(__file__).parent.parent / "data"


def ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# MLB Stats API helpers
# ---------------------------------------------------------------------------

def get_schedule(date_str: str) -> list[dict]:
    """Fetch MLB games for a given date from the Stats API."""
    url = f"{MLB_STATS_API}/schedule"
    params = {
        "sportId": 1,
        "date": date_str,
        "hydrate": "team,venue,probablePitcher,linescore",
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    games = []
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            game = {
                "game_pk": g["gamePk"],
                "date": date_str,
                "status": g["status"]["detailedState"],
                "home_team": g["teams"]["home"]["team"]["name"],
                "away_team": g["teams"]["away"]["team"]["name"],
                "home_team_id": g["teams"]["home"]["team"]["id"],
                "away_team_id": g["teams"]["away"]["team"]["id"],
                "venue": g.get("venue", {}).get("name", "Unknown"),
                "venue_id": g.get("venue", {}).get("id"),
                "game_time": g.get("gameDate", ""),
            }
            # Probable pitchers
            hp = g["teams"]["home"].get("probablePitcher", {})
            ap = g["teams"]["away"].get("probablePitcher", {})
            game["home_pitcher_id"] = hp.get("id")
            game["home_pitcher_name"] = hp.get("fullName", "TBD")
            game["away_pitcher_id"] = ap.get("id")
            game["away_pitcher_name"] = ap.get("fullName", "TBD")
            games.append(game)
    return games


def get_game_boxscore(game_pk: int) -> dict:
    """Fetch boxscore for a completed game (used in backtesting)."""
    url = f"{MLB_STATS_API}/game/{game_pk}/boxscore"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_roster(team_id: int, date_str: str) -> list[dict]:
    """Fetch active roster for a team."""
    url = f"{MLB_STATS_API}/teams/{team_id}/roster"
    params = {"rosterType": "active", "date": date_str}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    roster = []
    for p in resp.json().get("roster", []):
        if p.get("position", {}).get("abbreviation") != "P":
            roster.append({
                "player_id": p["person"]["id"],
                "name": p["person"]["fullName"],
                "position": p.get("position", {}).get("abbreviation", ""),
                "bats": p.get("person", {}).get("batSide", {}).get("code", "R"),
            })
    return roster


# Per-process cache of the schedule+lineups bundle. The MLB Stats API
# returns ALL games for a date in a single call when we hydrate=lineups,
# so caller code that asks for one game at a time still pays for only
# one API roundtrip per date. Keyed by date_str.
_LINEUPS_BY_DATE_CACHE: dict[str, dict] = {}


def fetch_lineups_for_date(date_str: str) -> dict:
    """Fetch all games' confirmed lineups for *date_str* via the MLB
    Stats API schedule endpoint with hydrate=lineups.

    Returns {game_pk: {"home": [...players-in-batting-order],
                       "away": [...players-in-batting-order],
                       "lineup_posted": bool}}

    Each player dict carries `player_id`, `name` (fullName), `position`,
    and `batting_order` (1-9, derived from the array index — the API
    returns players in batting-order sequence).

    `lineup_posted` is True iff the API actually returned a `lineups`
    block (homePlayers OR awayPlayers) for that game. Games without
    posted lineups (typically evening games before ~3-5pm ET) come
    back with empty lists and `lineup_posted=False` — caller decides
    whether to fall back to the bdfed roster (no batting order known).

    2026-05-04 critical fix: replaces our long-standing use of the
    bdfed/matchup endpoint, which returned the alphabetized 26-man
    active roster, NOT the batting-order lineup. We were treating
    array-index as batting order, so e.g. Aaron Judge ("J" sorts at
    position 8) was getting score_lineup_position(8)=50 instead of
    score_lineup_position(2)=85 every time he batted #2. The 0.150
    composite weight on lineup made this a real degradation.
    """
    if date_str in _LINEUPS_BY_DATE_CACHE:
        return _LINEUPS_BY_DATE_CACHE[date_str]

    url = f"{MLB_STATS_API}/schedule"
    params = {
        "sportId": 1,
        "date": date_str,
        "hydrate": "lineups,team",
    }
    out: dict[int, dict] = {}
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [LINEUP] statsapi schedule fetch failed for {date_str}: {e}")
        # Cache the empty result so we don't retry-spam on a sustained
        # outage; caller will fall through to bdfed roster fallback.
        _LINEUPS_BY_DATE_CACHE[date_str] = out
        return out

    for d in data.get("dates", []) or []:
        for g in d.get("games", []) or []:
            gpk = g.get("gamePk")
            if not gpk:
                continue
            lineups_block = g.get("lineups") or {}
            home_raw = lineups_block.get("homePlayers") or []
            away_raw = lineups_block.get("awayPlayers") or []
            posted = bool(home_raw or away_raw)

            def _shape(plist, lineup_source: str = "posted"):
                shaped = []
                for i, p in enumerate(plist):
                    if not isinstance(p, dict):
                        continue
                    pos = p.get("primaryPosition") or {}
                    shaped.append({
                        "player_id": p.get("id"),
                        "name": p.get("fullName", ""),
                        "position": pos.get("abbreviation") if isinstance(pos, dict) else str(pos),
                        # Array index 0 → batting #1, etc. Lineup arrays
                        # are exactly 9 entries when posted.
                        "batting_order": i + 1 if i < 9 else None,
                        # 2026-05-05: provenance flag so downstream
                        # consumers can distinguish a just-posted lineup
                        # from a recent-game fallback or roster fallback.
                        "lineup_source": lineup_source,
                    })
                return shaped

            # Capture team_ids per side so the recent-lineup fallback in
            # get_lineup() can look up "this team's most recent posted
            # lineup" without re-fetching the schedule.
            teams_block = g.get("teams") or {}
            home_team_id = (teams_block.get("home") or {}).get("team", {}).get("id")
            away_team_id = (teams_block.get("away") or {}).get("team", {}).get("id")

            out[gpk] = {
                "home": _shape(home_raw, "posted"),
                "away": _shape(away_raw, "posted"),
                "lineup_posted": posted,
                "home_team_id": home_team_id,
                "away_team_id": away_team_id,
            }

    _LINEUPS_BY_DATE_CACHE[date_str] = out
    return out


# Per-process cache of recent-lineup lookups. Keyed by team_id since
# "today's date" is fixed for the lifetime of a daily run. Avoids
# re-querying the schedule range when multiple games on today's slate
# need to fall back for the same team (only happens with doubleheaders,
# but cheap insurance).
_RECENT_LINEUP_BY_TEAM_CACHE: dict[int, dict | None] = {}


def fetch_recent_lineup_for_team(
    team_id: int,
    today_date_str: str,
    lookback_days: int = 7,
) -> dict | None:
    """Find a team's most recent posted lineup before *today_date_str*.

    Used as a smart fallback when the team hasn't posted today's lineup
    yet. Better than alphabetical-roster (the bdfed fallback) because:
      - Carries real batting order (~80-90% accurate vs. today's eventual
        lineup, since starting cores are stable for stretches)
      - Carries real fullName (no boxscoreName / "Castro, W" pattern)
      - Reflects actual platoon usage (vs. lefty-only batters might
        still appear if the team faced a righty recently — caveat
        documented for diagnostics)

    Returns:
      {"players": [9 ordered],
       "source_date": "YYYY-MM-DD",
       "side_in_source_game": "home" | "away"}
      or None if no lineup found in the lookback window.

    Each player dict carries `lineup_source = "recent:YYYY-MM-DD"` so
    downstream consumers (and the dashboard) can flag fallback rows.
    """
    if team_id in _RECENT_LINEUP_BY_TEAM_CACHE:
        return _RECENT_LINEUP_BY_TEAM_CACHE[team_id]

    try:
        from datetime import date as _date, timedelta as _td
        end = _date.fromisoformat(today_date_str) - _td(days=1)
        start = end - _td(days=lookback_days - 1)
    except Exception:
        _RECENT_LINEUP_BY_TEAM_CACHE[team_id] = None
        return None

    url = f"{MLB_STATS_API}/schedule"
    params = {
        "sportId": 1,
        "teamId": team_id,
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "hydrate": "lineups,team",
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [LINEUP] recent-lineup fetch failed for team_id={team_id}: {e}")
        _RECENT_LINEUP_BY_TEAM_CACHE[team_id] = None
        return None

    # Walk dates newest-first; pick the first game where this team's
    # side has a posted lineup. Each `dates[]` entry has `games[]` for
    # that calendar date.
    candidates = []
    for d in data.get("dates", []) or []:
        d_str = d.get("date")
        for g in d.get("games", []) or []:
            teams_block = g.get("teams") or {}
            home_id = (teams_block.get("home") or {}).get("team", {}).get("id")
            away_id = (teams_block.get("away") or {}).get("team", {}).get("id")
            side = "home" if home_id == team_id else ("away" if away_id == team_id else None)
            if not side:
                continue
            lineups_block = g.get("lineups") or {}
            players = lineups_block.get(f"{side}Players") or []
            if not players:
                continue
            candidates.append((d_str, side, players))

    if not candidates:
        _RECENT_LINEUP_BY_TEAM_CACHE[team_id] = None
        return None

    # Newest first by date string (lexical sort works for YYYY-MM-DD).
    candidates.sort(key=lambda c: c[0], reverse=True)
    src_date, side, players = candidates[0]

    shaped = []
    for i, p in enumerate(players):
        if not isinstance(p, dict):
            continue
        pos = p.get("primaryPosition") or {}
        shaped.append({
            "player_id": p.get("id"),
            "name": p.get("fullName", ""),
            "position": pos.get("abbreviation") if isinstance(pos, dict) else str(pos),
            "batting_order": i + 1 if i < 9 else None,
            "lineup_source": f"recent:{src_date}",
        })

    result = {
        "players": shaped,
        "source_date": src_date,
        "side_in_source_game": side,
    }
    _RECENT_LINEUP_BY_TEAM_CACHE[team_id] = result
    return result


def _bdfed_roster_fallback(game_pk: int) -> dict:
    """Last-resort fallback: pull the bdfed/matchup roster.

    Returns same shape as fetch_lineups_for_date entries, but with
    batting_order=None for every player (the bdfed endpoint returns
    the alphabetized 26-man active roster — array index is NOT real
    batting order). Caller's downstream lineup_score should then read
    None and apply the documented "unknown-position" fallback (35.0)
    rather than pretending we have ordered data.
    """
    url = f"{BDFED_MATCHUP_API}/{game_pk}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [LINEUP] bdfed roster fallback failed for {game_pk}: {e}")
        return {"home": [], "away": [], "lineup_posted": False}

    result = {"home": [], "away": [], "lineup_posted": False}
    for side in ["home", "away"]:
        side_data = data.get(side, {})
        if isinstance(side_data, list):
            players = side_data
        elif isinstance(side_data, dict):
            players = [side_data[k] for k in sorted(side_data.keys(), key=lambda x: int(x)) if side_data[k]]
        else:
            players = []
        for player in players:
            if not isinstance(player, dict):
                continue
            result[side].append({
                "player_id": player.get("id"),
                "name": player.get("boxscoreName", ""),
                "position": player.get("primaryPosition", ""),
                # Critical: NOT i+1. bdfed roster is alphabetical, not
                # batting-ordered — assigning index here is the bug we
                # just fixed at the source.
                "batting_order": None,
                "lineup_source": "roster_fallback",
            })
    return result


def get_lineup(game_pk: int, date_str: str | None = None) -> dict:
    """Fetch the confirmed starting lineup for *game_pk*.

    Tiered fallback strategy (per side, independent):
      1. **Posted lineup** (statsapi schedule?hydrate=lineups) — real
         batting order, fullName. The authoritative source.
      2. **Recent lineup** (statsapi schedule for prior 7 days, last
         posted) — typically ~80-90% accurate vs. today's eventual
         lineup. Carries real batting order + fullName but is stale.
         Players stamped `lineup_source = "recent:YYYY-MM-DD"`.
      3. **Roster fallback** (bdfed/matchup) — alphabetical 26-man
         active roster. NO batting order (`batting_order=None`),
         boxscoreName only. Players stamped
         `lineup_source = "roster_fallback"`.

    Each side resolves independently — common during the 2-4 hour
    window before evening first pitches when home clubs have posted
    and away clubs haven't (or vice versa).

    Returns {"home": [...], "away": [...], "lineup_posted": bool}.
    `lineup_posted` is True iff BOTH sides came from tier 1 (statsapi
    posted lineups). Recent + roster fallbacks both flip it False.

    Each player dict: player_id, name, position, batting_order,
    lineup_source (one of "posted" / "recent:YYYY-MM-DD" /
    "roster_fallback").
    """
    if date_str is None:
        try:
            from zoneinfo import ZoneInfo
            from datetime import datetime as _dt
            date_str = _dt.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        except Exception:
            from datetime import datetime as _dt
            date_str = _dt.now().strftime("%Y-%m-%d")

    by_game = fetch_lineups_for_date(date_str)
    entry = by_game.get(game_pk) or {}
    home = entry.get("home") or []
    away = entry.get("away") or []
    home_team_id = entry.get("home_team_id")
    away_team_id = entry.get("away_team_id")

    # Tier 2: recent-lineup fallback for any missing side. Tries to
    # avoid the alphabetical-roster fallback when we have a real recent
    # lineup we can borrow.
    if not home and home_team_id:
        recent = fetch_recent_lineup_for_team(home_team_id, date_str)
        if recent:
            home = recent["players"]
    if not away and away_team_id:
        recent = fetch_recent_lineup_for_team(away_team_id, date_str)
        if recent:
            away = recent["players"]

    # Tier 3: bdfed roster fallback for whatever's still missing.
    # Single API call per game (only when at least one side still needs
    # it after tier 1 + tier 2).
    if not home or not away:
        fb = _bdfed_roster_fallback(game_pk)
        if not home:
            home = fb.get("home") or []
        if not away:
            away = fb.get("away") or []

    return {
        "home": home,
        "away": away,
        "lineup_posted": bool(entry.get("home") and entry.get("away")),
    }


def _fetch_season_batting_splits(start_str: str, end_str: str) -> list:
    """
    Fetch batting stats from the MLB Stats API for a date range.
    Returns the raw splits list, or [] on failure.
    """
    url = f"{MLB_STATS_API}/stats"
    params = {
        "stats": "byDateRange",
        "startDate": start_str,
        "endDate": end_str,
        "group": "hitting",
        "gameType": "R",
        "sportId": 1,
        "sortStat": "homeRuns",
        "order": "desc",
        "limit": 300,
    }
    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        stats_list = data.get("stats", [])
        if not stats_list:
            return []
        return stats_list[0].get("splits", [])
    except Exception as e:
        print(f"  [TIERS] MLB Stats API batting stats failed ({start_str}–{end_str}): {e}")
        return []


# Full-name → abbreviation mapping (shared across tier-building helpers)
_TEAM_NAME_TO_ABBREV = {
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
    "Philadelphia Phillies": "PHI", "Pittsburgh Pirates": "PIT",
    "San Diego Padres": "SD", "San Francisco Giants": "SF",
    "Seattle Mariners": "SEA", "St. Louis Cardinals": "STL",
    "Tampa Bay Rays": "TB", "Texas Rangers": "TEX",
    "Toronto Blue Jays": "TOR", "Washington Nationals": "WSH",
}


def _splits_to_batters(splits: list) -> list[dict]:
    """
    Convert raw MLB Stats API splits into our standard batter-dict format.
    No filtering — returns every split as a batter dict.
    """
    batters = []
    for s in splits:
        stat = s.get("stat", {})
        player = s.get("player", {})
        team = s.get("team", {})

        hr = stat.get("homeRuns", 0)
        ab = stat.get("atBats", 0)
        pa = stat.get("plateAppearances", ab)
        games = stat.get("gamesPlayed", 0)

        avg = float(stat.get("avg", ".000") or ".000")
        slg = float(stat.get("slg", ".000") or ".000")
        obp = float(stat.get("obp", ".000") or ".000")
        iso = round(slg - avg, 3)
        # Audit MED: this is a SYNTHETIC wOBA proxy `0.7*OBP + 0.3*SLG`,
        # NOT real wOBA which uses Tango-style linear weights on
        # 1B/2B/3B/HR/BB/HBP. Effects: under-scores power-heavy hitters
        # (slug component is under-weighted vs ~0.5 in real wOBA),
        # over-scores walk-heavy guys without pop. The Stats API DOES
        # return individual hit types — a future refactor could compute
        # real wOBA. For now this estimate flows into season_batting and
        # is the fallback `enrich_with_season_batting` reaches for, so
        # caveats here propagate to score_matchup's woba_vs_hand.
        woba = round(obp * 0.7 + slg * 0.3, 3)

        # Synthetic Statcast estimates from hr_per_pa. Constants are
        # back-fit to MLB-Stats-API splits-only data, NOT real Statcast.
        # Audit MED flag: these constants drift over time and have no
        # source documentation. Consumers should prefer real Savant /
        # FanGraphs values when available; `enrich_with_season_batting`
        # only enriches when the live tier dict's value is zero/missing,
        # so this synthesis is the floor when better data isn't there.
        hr_per_pa = hr / max(pa, 1)
        # 200 = empirically back-fit constant; ~average HR/contact ratio
        # times barrel-rate-on-HR-contact. Real barrel% is Statcast-tracked.
        est_barrel_pct = round(min(25, hr_per_pa * 200), 1)
        # 82 + slg*15: linear extrapolation. Real exit velo ranges ~85-95.
        est_exit_velo = round(82 + slg * 15, 1)
        # hr_per_pa * 100 * 1.8: HR/FB approximation assuming ~55% FB/PA
        # contact rate. Real HR/FB% is computed from FB count, not PA.
        est_hr_fb_pct = round(hr_per_pa * 100 * 1.8, 1)

        team_name = team.get("name", "")
        team_abbrev = (team.get("abbreviation")
                       or _TEAM_NAME_TO_ABBREV.get(team_name, "???"))
        bat_side = player.get("batSide", {}).get("code", "R")

        batters.append({
            "name": player.get("fullName", "Unknown"),
            "team": team_abbrev,
            "bats": bat_side,
            "hr": hr,
            "pa": pa,
            "ab": ab,
            "games": games,
            "hr_per_pa": round(hr_per_pa, 4),
            "barrel_pct": est_barrel_pct,
            "exit_velo": est_exit_velo,
            "hr_fb_pct": est_hr_fb_pct,
            "iso": iso,
            "woba": woba,
            "player_id": player.get("id", 0),
            # Provenance for the synthetic Statcast estimates above. Real
            # Statcast values get stamped 'statcast' downstream when bulk
            # Savant fetches return non-null values; until then we record
            # that these fields are MLB-Stats-API-derived estimates.
            "_barrel_pct_source": "synthetic_hr_per_pa",
        })
    return batters


def build_live_tiers(
    date_str: str,
    window_games: int = 40,
    min_games: int = 5,
    t1_pct: float = 0.15,
    t2_pct: float = 0.30,
    t3_pct: float = 0.30,
    t1_floor: int = 10,
    t2_floor: int = 15,
    t3_floor: int = 15,
    lineup_player_ids: set[int] | None = None,
) -> dict | None:
    """
    Build live tiers using a **rolling game window** that crosses season
    boundaries.  Ranks by HR/PA rate descending.

    Strategy:
    1. Pull current-season batting stats (start of season → date_str).
    2. If a player has fewer than *window_games* in the current season,
       backfill from the end of the prior season so every player has up to
       *window_games* of data to be evaluated on.
    3. Qualify any batter who BOTH:
         (a) has ≥ *min_games* games played across the window AND ≥ 1 HR
         (b) has at least one game in the CURRENT season OR appears in
             today's lineup (`lineup_player_ids`). This excludes prior-
             season-only ghosts (B5 — e.g., Blaine Crim's 2025 line on a
             2026 slate) while still admitting true rookies and IL
             returnees the moment they're penciled into a lineup.
    4. Rank by HR/PA rate descending.
    5. Adaptive tier sizing:
       - T1 (Chalk):    top *t1_pct* of qualified pool, min *t1_floor*
       - T2 (Mid):      next *t2_pct*, min *t2_floor*
       - T3 (Longshot): next *t3_pct*, min *t3_floor*
       - Bottom 25% = untiered (not eligible for picks)

    `lineup_player_ids` is optional for backwards compatibility — callers
    that don't pass it get pre-B5 behavior (no current-season requirement).
    Real production callers should pass the set from today's `lineups`.

    Returns {1: [...], 2: [...], 3: [...]} or None on failure.
    """
    season = int(date_str[:4])
    end_dt = datetime.strptime(date_str, "%Y-%m-%d")

    # ── Step 1: current-season stats ──────────────────────────────────
    opening_day_approx = datetime(season, 3, 27)
    cur_start = max(opening_day_approx, datetime(season, 1, 1))
    cur_start_str = cur_start.strftime("%Y-%m-%d")

    print(f"  [TIERS] Pulling {season} season stats: {cur_start_str} → {date_str}")
    cur_splits = _fetch_season_batting_splits(cur_start_str, date_str)
    cur_batters = _splits_to_batters(cur_splits)
    cur_by_id = {b["player_id"]: b for b in cur_batters}

    print(f"  [TIERS] Current season: {len(cur_batters)} batters returned")

    # ── Step 2: prior-season backfill ─────────────────────────────────
    # Fetch last ~90 days of prior season to fill rolling window
    prior_end = datetime(season - 1, 9, 29)
    prior_start = prior_end - timedelta(days=90)
    prior_start_str = prior_start.strftime("%Y-%m-%d")
    prior_end_str = prior_end.strftime("%Y-%m-%d")

    print(f"  [TIERS] Pulling {season - 1} backfill: {prior_start_str} → {prior_end_str}")
    prior_splits = _fetch_season_batting_splits(prior_start_str, prior_end_str)
    prior_batters = _splits_to_batters(prior_splits)
    prior_by_id = {b["player_id"]: b for b in prior_batters}

    print(f"  [TIERS] Prior season backfill: {len(prior_batters)} batters returned")

    # ── Step 3: merge into rolling window ─────────────────────────────
    # For each player: use current-season data as primary. If they have
    # fewer than window_games in the current season, blend in prior-season
    # stats (weighted by games) to fill the gap.
    all_player_ids = set(cur_by_id.keys()) | set(prior_by_id.keys())
    merged = []

    for pid in all_player_ids:
        cur = cur_by_id.get(pid)
        prior = prior_by_id.get(pid)

        if cur and cur["games"] >= window_games:
            # Enough current-season data — use as-is
            merged.append(cur)
        elif cur and prior:
            # Blend: weight by games played
            cur_g = cur["games"]
            need_g = window_games - cur_g
            prior_g = min(prior["games"], need_g)
            total_g = cur_g + prior_g
            if total_g == 0:
                continue

            # Weighted averages for rate stats
            cur_w = cur_g / total_g
            prior_w = prior_g / total_g

            blended = {**cur}  # start with current-season metadata (team, etc.)
            blended["games"] = total_g
            blended["hr"] = cur["hr"] + round(prior["hr"] * (prior_g / max(prior["games"], 1)))
            blended["pa"] = cur["pa"] + round(prior["pa"] * (prior_g / max(prior["games"], 1)))
            blended["ab"] = cur["ab"] + round(prior["ab"] * (prior_g / max(prior["games"], 1)))
            blended["hr_per_pa"] = round(blended["hr"] / max(blended["pa"], 1), 4)
            blended["iso"] = round(cur["iso"] * cur_w + prior["iso"] * prior_w, 3)
            blended["woba"] = round(cur["woba"] * cur_w + prior["woba"] * prior_w, 3)

            # Re-estimate power metrics from blended rates
            blended["barrel_pct"] = round(min(25, blended["hr_per_pa"] * 200), 1)
            blended["exit_velo"] = round(82 + (cur["iso"] * cur_w + prior["iso"] * prior_w + 0.250) * 15, 1)
            blended["hr_fb_pct"] = round(blended["hr_per_pa"] * 100 * 1.8, 1)
            blended["_blend"] = f"{cur_g}g cur + {prior_g}g prior"
            # Provenance: still synthetic, just blended across two seasons.
            blended["_barrel_pct_source"] = "synthetic_hr_per_pa"
            merged.append(blended)
        elif cur:
            # Only current-season data (no prior season match)
            merged.append(cur)
        elif prior:
            # Only prior-season data (hasn't appeared this year)
            # Still include — they might be in today's lineup
            merged.append(prior)

    # ── Step 4: qualify and rank ──────────────────────────────────────
    # B5 (2026-05-20): a player must EITHER have appeared in the current
    # season (any 2026 games at all — `cur_by_id` membership) OR be in
    # today's lineup. This filters prior-season-only ghosts (Blaine Crim
    # had 0 games in 2026 but his 2025 line was qualifying him) while
    # admitting rookies and IL-returnees the moment they're in a lineup.
    # When lineup_player_ids is None (older callers), this collapses to
    # the pre-B5 behavior.
    lineup_ids = lineup_player_ids if lineup_player_ids is not None else None
    def _qualifies(b):
        if b["games"] < min_games or b["hr"] < 1:
            return False
        if lineup_ids is None:
            return True  # legacy behavior — no current-season requirement
        return (b["player_id"] in cur_by_id) or (b["player_id"] in lineup_ids)

    qualified = [b for b in merged if _qualifies(b)]
    qualified.sort(key=lambda x: x["hr_per_pa"], reverse=True)

    n = len(qualified)
    if n < 10:
        print(f"  [TIERS] Only {n} qualified batters — not enough for tiers")
        return None

    print(f"  [TIERS] {n} qualified batters (≥{min_games} games, ≥1 HR, "
          f"current-season OR in-lineup), ranked by HR/PA")

    # ── Step 5: adaptive tier sizing with floors ──────────────────────
    t1_size = max(t1_floor, round(n * t1_pct))
    t2_size = max(t2_floor, round(n * t2_pct))
    t3_size = max(t3_floor, round(n * t3_pct))

    # Make sure we don't exceed the pool
    total_tiered = t1_size + t2_size + t3_size
    if total_tiered > n:
        # Scale down proportionally, respecting floors
        available = n
        t1_size = min(t1_size, available)
        available -= t1_size
        t2_size = min(t2_size, available)
        available -= t2_size
        t3_size = min(t3_size, available)

    tiers = {
        1: qualified[:t1_size],
        2: qualified[t1_size:t1_size + t2_size],
        3: qualified[t1_size + t2_size:t1_size + t2_size + t3_size],
    }

    # ── Step 6: REMOVED — within-tier power normalization ─────────────
    # Previously this re-ranked barrel_pct/exit_velo/hr_fb_pct/iso within
    # each tier to fit fixed display ranges. That destroyed the actual
    # signal: a top hitter with the lowest barrel% in T1 was renormalized
    # down to ~0, making score_power compute ~13 for legit elite power
    # bats (Buxton 5/1: real barrel 11.9 → renormed to 2.3 → power 13.1).
    # Removed 2026-05-01. Real barrel/EV/HR-FB enrichment now happens in
    # generate_picks.score_live_slate via FanGraphs + season_batting fallback.
    # The synthetic estimates in _splits_to_batters remain as the floor
    # when neither real source has the player. score_power skips zeros.

    # Mark synthetic estimates so downstream consumers can tell them apart
    # from real Statcast values.
    for tier_batters in tiers.values():
        for b in tier_batters:
            b["_power_source"] = "estimate_from_hr_per_pa"

    # Summary
    untiered = n - (t1_size + t2_size + t3_size)
    for t_num, t_list in tiers.items():
        if t_list:
            top = t_list[0]
            bot = t_list[-1]
            label = {1: "T1-Chalk", 2: "T2-Mid", 3: "T3-Longshot"}[t_num]
            print(f"  [TIERS] {label}: {len(t_list)} batters "
                  f"(HR/PA: {top['hr_per_pa']:.4f}–{bot['hr_per_pa']:.4f}) "
                  f"top={top['name']} bot={bot['name']}")
    print(f"  [TIERS] Untiered (bottom): {untiered} batters excluded")

    return tiers


# ---------------------------------------------------------------------------
# Weather via Open-Meteo
# ---------------------------------------------------------------------------

def get_weather(venue_name: str, game_time_iso: str) -> dict:
    """
    Fetch weather for a venue at game time using Open-Meteo.

    Correctly handles timezones: Open-Meteo is queried with the venue's local
    IANA timezone, the local date of first pitch, and the local hour of first
    pitch used as the hourly index. Previously this function used UTC date and
    UTC hour against an Eastern-time array, which mis-pulled weather by a day
    and/or several hours for any game outside the Eastern timezone.

    Each return dict carries an `_source` field (added 2026-05-03) so
    downstream consumers can stratify by data quality:
      - 'dome_default'              indoor venue, fixed indoor weather
      - 'coords_missing_default'    venue not in VENUE_COORDS, neutral fallback
      - 'open_meteo'                real API fetch
      - 'api_failed_default'        API call raised, neutral fallback
    """
    if venue_name in DOME_STADIUMS:
        return {"temperature_f": 72, "wind_mph": 0, "wind_direction_deg": 0,
                "dome": True, "_source": "dome_default"}

    coords = VENUE_COORDS.get(venue_name)
    if not coords:
        return {"temperature_f": 68, "wind_mph": 5, "wind_direction_deg": 0,
                "dome": False, "_source": "coords_missing_default"}

    lat, lon = coords
    tz_name = VENUE_TZ.get(venue_name, "America/New_York")

    try:
        # Parse UTC game time, then convert to venue-local time.
        dt_utc = datetime.fromisoformat(game_time_iso.replace("Z", "+00:00"))
        dt_local = dt_utc.astimezone(ZoneInfo(tz_name))
        date_str = dt_local.strftime("%Y-%m-%d")  # LOCAL date at first pitch
        game_hour = dt_local.hour                 # LOCAL hour of first pitch

        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m,windspeed_10m,winddirection_10m,relativehumidity_2m",
            "start_date": date_str,
            "end_date": date_str,
            "temperature_unit": "fahrenheit",
            "windspeed_unit": "mph",
            "timezone": tz_name,  # Align hourly array to venue-local time
        }
        # Open-Meteo's free tier appears to deprioritize GitHub Actions
        # egress IPs — 10s read timeouts were producing 5-8 venue failures
        # per noon run. 30s + one retry on transient errors clears most
        # of them without changing behavior for healthy responses.
        resp = None
        for attempt in range(2):
            try:
                resp = requests.get(url, params=params, timeout=30)
                resp.raise_for_status()
                break
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 0:
                    time.sleep(1.5)
                    continue
                raise
        hourly = resp.json().get("hourly", {})

        temps = hourly.get("temperature_2m", []) or [68]
        winds = hourly.get("windspeed_10m", []) or [5]
        wind_dirs = hourly.get("winddirection_10m", []) or [0]
        humidities = hourly.get("relativehumidity_2m", []) or [50]

        # Clamp hour index to the hourly array we got back
        idx = max(0, min(game_hour, len(temps) - 1))

        return {
            "temperature_f": temps[idx] if idx < len(temps) else 68,
            "wind_mph": winds[idx] if idx < len(winds) else 5,
            "wind_direction_deg": wind_dirs[idx] if idx < len(wind_dirs) else 0,
            "humidity_pct": humidities[idx] if idx < len(humidities) else 50,
            "dome": False,
            "tz": tz_name,
            "local_hour": game_hour,
            "_source": "open_meteo",
        }
    except Exception as e:
        print(f"  [WEATHER] {venue_name}: fetch failed ({e}), using neutral defaults")
        return {"temperature_f": 68, "wind_mph": 5, "wind_direction_deg": 0,
                "dome": False, "_source": "api_failed_default"}


# ---------------------------------------------------------------------------
# Statcast / pybaseball helpers
# ---------------------------------------------------------------------------

def get_statcast_batter_stats(season: int, min_pa: int = 50) -> pd.DataFrame:
    """
    Pull season-level batter stats from FanGraphs via pybaseball.
    Returns DataFrame with key power metrics.
    """
    print(f"  Fetching FanGraphs batting stats for {season} (min {min_pa} PA)...")
    try:
        df = batting_stats(season, qual=min_pa)
        # Standardize column names
        cols_map = {
            "IDfg": "fg_id", "Name": "name", "Team": "team",
            "PA": "pa", "HR": "hr", "AB": "ab",
            "Barrel%": "barrel_pct", "HardHit%": "hard_hit_pct",
            "HR/FB": "hr_fb_pct", "ISO": "iso",
            "wOBA": "woba", "wRC+": "wrc_plus",
            "Exit Velocity (avg)": "exit_velo",
            "FB%": "fb_pct",
        }
        # Only rename columns that exist
        rename = {k: v for k, v in cols_map.items() if k in df.columns}
        df = df.rename(columns=rename)

        # Try to find barrel% and exit velo under alternate names
        if "barrel_pct" not in df.columns and "Barrel%" in df.columns:
            df["barrel_pct"] = df["Barrel%"]
        if "exit_velo" not in df.columns:
            for col in ["EV", "Exit Velocity", "HardHit"]:
                if col in df.columns:
                    df["exit_velo"] = df[col]
                    break

        return df
    except Exception as e:
        print(f"  Warning: Could not fetch batting stats: {e}")
        return pd.DataFrame()


def get_statcast_pitcher_stats(season: int, min_ip: int = 20) -> pd.DataFrame:
    """Pull pitcher stats from FanGraphs."""
    print(f"  Fetching FanGraphs pitching stats for {season} (min {min_ip} IP)...")
    try:
        df = pitching_stats(season, qual=min_ip)
        cols_map = {
            "IDfg": "fg_id", "Name": "name", "Team": "team",
            "IP": "ip", "HR/9": "hr_per_9", "ERA": "era",
            "HardHit%": "hard_hit_pct_allowed", "K/9": "k_per_9",
            "BB/9": "bb_per_9", "FIP": "fip",
        }
        rename = {k: v for k, v in cols_map.items() if k in df.columns}
        df = df.rename(columns=rename)
        return df
    except Exception as e:
        print(f"  Warning: Could not fetch pitching stats: {e}")
        return pd.DataFrame()


def get_park_factors_data(season: int) -> pd.DataFrame:
    """
    Pull park factors with L/R handedness splits.

    Tries the SQLite DB first (populated by etl_nightly.sync_park_factors),
    then falls back to the curated seed dataset in etl.park_factors_seed.

    Returns a DataFrame with columns:
        venue, hr_pf_overall, hr_pf_lhb, hr_pf_rhb, hr_park_factor (legacy alias)
    """
    # Try the database first
    try:
        from etl.db import get_db
        conn = get_db()
        df = pd.read_sql_query(
            "SELECT venue, hr_pf_overall, hr_pf_lhb, hr_pf_rhb "
            "FROM park_factors WHERE season = ?",
            conn, params=(season,)
        )
        conn.close()
        if not df.empty:
            df["hr_park_factor"] = df["hr_pf_overall"]
            print(f"  Loaded {len(df)} park factors from DB for {season}")
            return df
    except Exception as e:
        print(f"  [park_factors] DB lookup failed ({e}), falling back to seed")

    # Fallback to curated seed
    return get_hardcoded_park_factors()


def get_hardcoded_park_factors() -> pd.DataFrame:
    """
    Curated 30-venue HR park factors with L/R splits.

    Returns the seed dataset from etl.park_factors_seed. Used as a fallback
    when the DB isn't populated yet.
    """
    try:
        from etl.park_factors_seed import get_seed_dataframe
        return get_seed_dataframe()
    except Exception:
        # Last-resort fallback: legacy overall-only table. Should never
        # happen in practice since park_factors_seed is always present.
        data = {
            "venue": [
                "Coors Field", "Great American Ball Park", "Yankee Stadium",
                "Globe Life Field", "Fenway Park", "Wrigley Field",
                "Citizens Bank Park", "Dodger Stadium", "Truist Park",
                "Guaranteed Rate Field", "Busch Stadium", "Minute Maid Park",
                "Target Field", "Kauffman Stadium", "Angel Stadium",
                "Citi Field", "PNC Park", "Comerica Park",
                "Nationals Park", "Progressive Field", "Petco Park",
                "American Family Field", "Oriole Park at Camden Yards",
                "T-Mobile Park", "Rogers Centre", "Chase Field",
                "Tropicana Field", "loanDepot park", "Oracle Park",
                "Oakland Coliseum",
            ],
            "hr_park_factor": [
                130, 118, 115, 112, 110, 108, 107, 105, 104, 103,
                102, 106, 101, 100, 99, 98, 95, 96, 103, 97,
                92, 105, 108, 93, 107, 104, 96, 90, 82, 88,
            ],
        }
        df = pd.DataFrame(data)
        df["hr_pf_overall"] = df["hr_park_factor"]
        df["hr_pf_lhb"] = df["hr_park_factor"]
        df["hr_pf_rhb"] = df["hr_park_factor"]
        return df


def get_recent_statcast(player_id: int, days: int = 14) -> pd.DataFrame:
    """Pull recent Statcast batted-ball data for a player (last N days)."""
    end = datetime.now()
    start = end - timedelta(days=days)
    try:
        df = statcast_batter(
            start.strftime("%Y-%m-%d"),
            end.strftime("%Y-%m-%d"),
            player_id,
        )
        return df
    except Exception:
        return pd.DataFrame()


def get_recent_game_log(player_id: int, season: int,
                        hr_window: int = 10, rate_window: int = 30) -> dict:
    """
    Pull a player's recent game log from the MLB Stats API and summarize it
    over two windows (Form factor rebuild, 2026-05-19):

      - HR count over the last *hr_window* games — HRs are lumpy, so a short
        window keeps the signal recent.
      - ISO / AVG / SLG over the last *rate_window* games — rate stats need a
        bigger sample to stabilize (a 10-game ISO whipsaws wildly).

    Also returns `recent_window_days`: the calendar span of the rate window.
    A normal 30-game window spans ~36-42 days; a much larger span flags a
    batter who has missed significant time (IL stint) — score_form's
    long-rest dampener pulls form toward neutral for those.

    One HTTP call fetches the full season game log; both windows slice from
    it. Returns {} on failure.
    """
    url = f"{MLB_STATS_API}/people/{player_id}/stats"
    params = {
        "stats": "gameLog",
        "season": season,
        "group": "hitting",
        "gameType": "R",
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
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

    def _agg(game_splits: list) -> dict:
        """Aggregate a list of game-log splits into counting + rate stats."""
        ab = sum(g.get("stat", {}).get("atBats", 0) for g in game_splits)
        hits = sum(g.get("stat", {}).get("hits", 0) for g in game_splits)
        d2 = sum(g.get("stat", {}).get("doubles", 0) for g in game_splits)
        d3 = sum(g.get("stat", {}).get("triples", 0) for g in game_splits)
        hr = sum(g.get("stat", {}).get("homeRuns", 0) for g in game_splits)
        tb = hits + d2 + d3 * 2 + hr * 3
        avg = hits / ab if ab else 0.0
        slg = tb / ab if ab else 0.0
        return {"ab": ab, "hits": hits, "hr": hr,
                "avg": avg, "slg": slg, "iso": slg - avg}

    hr_games = splits[-hr_window:]
    rate_games = splits[-rate_window:]
    hr_stat = _agg(hr_games)
    rate_stat = _agg(rate_games)

    # Calendar span of the rate window — staleness signal for IL returns.
    rate_dates = sorted(g["date"] for g in rate_games if g.get("date"))
    window_days = None
    if len(rate_dates) >= 2:
        try:
            window_days = (datetime.fromisoformat(rate_dates[-1])
                           - datetime.fromisoformat(rate_dates[0])).days
        except (ValueError, TypeError):
            window_days = None

    # Hot streak: HR in at least 2 of last 5 games (kept for back-compat).
    last_5 = splits[-5:]
    games_with_hr = sum(1 for g in last_5
                        if g.get("stat", {}).get("homeRuns", 0) > 0)

    return {
        # --- split-window Form inputs (consumed by score_form) ---
        "recent_hr_10g":        hr_stat["hr"],
        "recent_iso_30g":       round(rate_stat["iso"], 3),
        "recent_avg_30g":       round(rate_stat["avg"], 3),
        "recent_slg_30g":       round(rate_stat["slg"], 3),
        "recent_window_days":   window_days,
        "games_in_hr_window":   len(hr_games),
        "games_in_rate_window": len(rate_games),
        # --- legacy keys, kept so other consumers keep working ---
        "recent_hr":    hr_stat["hr"],
        "recent_ab":    rate_stat["ab"],
        "recent_hits":  rate_stat["hits"],
        "recent_slg":   round(rate_stat["slg"], 3),
        "recent_iso":   round(rate_stat["iso"], 3),
        "recent_avg":   round(rate_stat["avg"], 3),
        "games_played": len(rate_games),
        "hot_streak":   games_with_hr >= 2,
        "games_with_hr_last5": games_with_hr,
    }


def get_recent_pitcher_game_log(
    pitcher_id: int,
    season: int,
    today_str: str | None = None,
    days: int = 21,
) -> dict:
    """
    Pull a pitcher's recent game log from the MLB Stats API and aggregate
    HR allowed + IP over the last *days* days BEFORE *today_str* (exclusive).

    Mirrors get_recent_game_log() but for pitchers. Adds the "pitcher
    recency" signal that score_pitcher_vulnerability() needed —
    season-aggregate HR/9 lags 3-4 bad starts, so a pitcher whose recent
    stretch has collapsed (e.g., Brady Singer on 2026-05-12: 9 HR allowed
    over 4 starts vs. season HR/9 of 1.89) was invisible to the model.

    Returns dict with keys:
        recent_hr_count   — HRs allowed in window
        recent_ip         — innings pitched in window (parsed from "5.2" =
                            5 IP + 2 outs format MLB API uses)
        recent_starts     — count of games started in window
        recent_hr_per_9   — HR * 9 / IP, or None if IP < 1.0
    Empty dict on API failure (caller treats as missing signal).
    """
    url = f"{MLB_STATS_API}/people/{pitcher_id}/stats"
    params = {
        "stats": "gameLog",
        "season": season,
        "group": "pitching",
        "gameType": "R",
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
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

    # Window: [today - days, today). today_str excluded — we're scoring
    # games yet to be played, recent form is what came BEFORE today.
    if today_str:
        try:
            today_dt = datetime.strptime(today_str, "%Y-%m-%d")
        except ValueError:
            today_dt = datetime.now()
    else:
        today_dt = datetime.now()
    cutoff_str = (today_dt - timedelta(days=days)).strftime("%Y-%m-%d")
    today_iso = today_dt.strftime("%Y-%m-%d")

    recent_hr = 0
    recent_outs = 0     # 1 IP = 3 outs; lets us add "5.2" + "4.1" precisely
    recent_starts = 0
    for g in splits:
        gdate = g.get("date") or ""
        if gdate < cutoff_str or gdate >= today_iso:
            continue
        s = g.get("stat", {}) or {}
        # inningsPitched format: "5.2" = 5 IP + 2 outs (i.e. 5 2/3 IP).
        ip_str = str(s.get("inningsPitched", "0") or "0")
        try:
            whole, _, frac = ip_str.partition(".")
            outs = int(whole) * 3 + (int(frac) if frac else 0)
        except (ValueError, TypeError):
            outs = 0
        games_started = int(s.get("gamesStarted", 0) or 0)
        # Filter to starts only — recency signal is "how he's pitching as
        # a starter," not noise from random relief appearances. A starter
        # appearing in relief is rare but still skipped here.
        if games_started == 0 and outs == 0:
            continue
        recent_hr += int(s.get("homeRuns", 0) or 0)
        recent_outs += outs
        recent_starts += games_started

    recent_ip = recent_outs / 3.0
    if recent_ip < 1.0:
        # Below 1 IP in 21 days isn't a usable rate — return counts but
        # let recent_hr_per_9 be None so the blend falls back to season.
        return {
            "recent_hr_count": recent_hr,
            "recent_ip": round(recent_ip, 1),
            "recent_starts": recent_starts,
            "recent_hr_per_9": None,
        }

    recent_hr_per_9 = recent_hr * 9.0 / recent_ip

    return {
        "recent_hr_count": recent_hr,
        "recent_ip": round(recent_ip, 1),
        "recent_starts": recent_starts,
        "recent_hr_per_9": round(recent_hr_per_9, 2),
    }


# ---------------------------------------------------------------------------
# Aggregate fetch for a single date
# ---------------------------------------------------------------------------

def fetch_for_date(date_str: str, season: int = None) -> dict:
    """
    Master fetch function: pulls all data needed for scoring on a given date.
    Returns a dict with games, batters, pitchers, park_factors, weather.
    """
    if season is None:
        season = int(date_str[:4])

    print(f"\n{'='*60}")
    print(f"Fetching data for {date_str}")
    print(f"{'='*60}")

    # 1. Schedule
    print("  Fetching schedule...")
    games = get_schedule(date_str)
    print(f"  Found {len(games)} games")

    if not games:
        return {"date": date_str, "games": [], "batters": [], "pitchers": [],
                "park_factors": pd.DataFrame(), "weather": {}}

    # 2. Season-level batter stats
    batter_stats = get_statcast_batter_stats(season)

    # 3. Season-level pitcher stats
    pitcher_stats = get_statcast_pitcher_stats(season)

    # 4. Park factors
    park_factors_df = get_park_factors_data(season)

    # 5. Weather for each game
    weather_data = {}
    for g in games:
        venue = g["venue"]
        game_time = g.get("game_time", "")
        weather_data[g["game_pk"]] = get_weather(venue, game_time)

    return {
        "date": date_str,
        "season": season,
        "games": games,
        "batter_stats": batter_stats,
        "pitcher_stats": pitcher_stats,
        "park_factors": park_factors_df,
        "weather": weather_data,
    }


# ---------------------------------------------------------------------------
# Bulk fetch for backtesting a full season
# ---------------------------------------------------------------------------

def fetch_season(season: int, sample_days: int = None) -> list[str]:
    """
    Fetch schedule for every day of an MLB season.
    Returns list of dates that had games.
    If sample_days is set, randomly samples that many game-dates.
    """
    # MLB regular season: ~late March to late September
    start = datetime(season, 3, 28)
    end = datetime(season, 9, 29)

    print(f"Scanning {season} season for game dates...")
    all_dates = []
    current = start
    while current <= end:
        date_str = current.strftime("%Y-%m-%d")
        try:
            games = get_schedule(date_str)
            if games:
                all_dates.append(date_str)
        except Exception:
            pass
        current += timedelta(days=1)
        time.sleep(0.1)  # rate limiting

    print(f"Found {len(all_dates)} game dates in {season}")

    if sample_days and sample_days < len(all_dates):
        rng = np.random.default_rng(42)
        all_dates = sorted(rng.choice(all_dates, sample_days, replace=False).tolist())
        print(f"Sampled {sample_days} dates for backtesting")

    return all_dates


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fetch MLB data for HR parlay scoring")
    parser.add_argument("--date", default="today", help="Date (YYYY-MM-DD or 'today')")
    parser.add_argument("--season", type=int, help="Fetch full season for backtesting")
    parser.add_argument("--output", default=None, help="Output JSON path")
    args = parser.parse_args()

    if args.season:
        dates = fetch_season(args.season)
        output_path = args.output or str(DATA_DIR / f"season_{args.season}_dates.json")
        ensure_data_dir()
        with open(output_path, "w") as f:
            json.dump(dates, f, indent=2)
        print(f"Saved {len(dates)} game dates to {output_path}")
    else:
        if args.date == "today":
            date_str = datetime.now().strftime("%Y-%m-%d")
        else:
            date_str = args.date

        data = fetch_for_date(date_str)
        output_path = args.output or str(DATA_DIR / f"daily_{date_str}.json")
        ensure_data_dir()

        # Serialize — convert DataFrames to dicts
        serializable = {
            "date": data["date"],
            "games": data["games"],
            "weather": data["weather"],
            "batter_count": len(data.get("batter_stats", [])),
            "pitcher_count": len(data.get("pitcher_stats", [])),
        }
        with open(output_path, "w") as f:
            json.dump(serializable, f, indent=2, default=str)
        print(f"Saved daily data to {output_path}")


if __name__ == "__main__":
    main()