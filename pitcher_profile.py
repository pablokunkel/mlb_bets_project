#!/usr/bin/env python3
"""
pitcher_profile.py — Pitcher archetype matching for Daily HR Bet.

Builds "victim profiles" for batters (what kind of pitcher they homer against)
and compares them to today's opposing pitcher's profile.  Produces a 0-100
archetype similarity score that feeds into the new two-signal matchup scoring.

Two signals blended 50/50:
  1. Pitcher vulnerability — is this pitcher generally hittable for HRs?
  2. Archetype similarity — does this pitcher look like pitchers this batter
     has historically taken deep?

Data sources (all free):
  - Baseball Savant via pybaseball: statcast_batter() for HR events,
    statcast_pitcher() for pitch-level data
  - MLB Stats API: pitcher season stats, handedness, basic bio

Caching:
  - Batter HR events: 24h TTL (JSON in data/cache/)
  - Victim profiles: 24h TTL
  - Pitcher arsenals: 7d TTL
  - Pitcher profiles: 24h TTL
"""

import json
import os
import time
from datetime import datetime, timedelta
from math import sqrt
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Cache setup
# ---------------------------------------------------------------------------

CACHE_DIR = Path(__file__).parent.parent / "data" / "cache"

# TTLs in seconds
TTL_BATTER_HR_EVENTS = 86400      # 24 hours
TTL_VICTIM_PROFILE = 86400        # 24 hours
TTL_PITCHER_ARSENAL = 7 * 86400   # 7 days
TTL_PITCHER_PROFILE = 86400       # 24 hours


def _cache_path(namespace: str, key: str) -> Path:
    ns_dir = CACHE_DIR / namespace
    ns_dir.mkdir(parents=True, exist_ok=True)
    return ns_dir / f"{key}.json"


def _cache_get(namespace: str, key: str, ttl: int):
    """Return cached data if fresh, else None."""
    path = _cache_path(namespace, key)
    if not path.exists():
        return None
    try:
        mtime = path.stat().st_mtime
        if time.time() - mtime > ttl:
            return None
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _cache_set(namespace: str, key: str, data):
    """Write data to cache."""
    path = _cache_path(namespace, key)
    try:
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Normalization ranges for similarity calculation
# ---------------------------------------------------------------------------

# Known MLB ranges for each dimension (used to normalize to 0-1)
DIMENSION_RANGES = {
    "avg_fb_velo":        (85.0, 100.0),
    "fb_usage_pct":       (0.20, 0.80),
    "breaking_usage_pct": (0.05, 0.50),
    "offspeed_usage_pct": (0.00, 0.40),
    "avg_fb_spin":        (1800.0, 2700.0),
    "avg_extension":      (5.0, 7.5),
}

# Weights for each dimension in similarity calculation
DIMENSION_WEIGHTS = {
    "avg_fb_velo":        0.30,  # Strongest signal — velo differentiates HR outcomes
    "fb_usage_pct":       0.10,  # Part of pitch mix
    "breaking_usage_pct": 0.08,  # Part of pitch mix
    "offspeed_usage_pct": 0.07,  # Part of pitch mix
    "handedness":         0.20,  # Platoon effects are large
    "avg_fb_spin":        0.15,  # Affects plane and movement
    "avg_extension":      0.10,  # Affects perceived velocity
}

# League-average victim profile (fallback for batters with < 3 HRs)
LEAGUE_AVG_VICTIM = {
    "avg_fb_velo": 93.5,
    "fb_usage_pct": 0.53,
    "breaking_usage_pct": 0.28,
    "offspeed_usage_pct": 0.15,
    "hand_R_pct": 0.65,  # ~65% of pitchers are RHP
    "avg_fb_spin": 2250.0,
    "avg_extension": 6.2,
}


# ---------------------------------------------------------------------------
# Pitch type classification
# ---------------------------------------------------------------------------

FASTBALL_TYPES = {"FF", "SI", "FC", "FA"}    # 4-seam, sinker, cutter, generic FB
BREAKING_TYPES = {"SL", "CU", "KC", "SV", "CS", "KN", "SC", "EP"}  # slider, curve, knuckle-curve, etc.
OFFSPEED_TYPES = {"CH", "FS", "FO", "KN"}    # changeup, splitter, forkball, knuckleball


def _classify_pitch_mix(arsenal: dict) -> dict:
    """
    Given a pitch arsenal dict {pitch_type: usage_pct, ...},
    return aggregated {fb_usage_pct, breaking_usage_pct, offspeed_usage_pct}.
    """
    fb = sum(v for k, v in arsenal.items() if k in FASTBALL_TYPES)
    brk = sum(v for k, v in arsenal.items() if k in BREAKING_TYPES)
    off = sum(v for k, v in arsenal.items() if k in OFFSPEED_TYPES)
    total = fb + brk + off
    if total == 0:
        return {"fb_usage_pct": 0.53, "breaking_usage_pct": 0.28, "offspeed_usage_pct": 0.15}
    return {
        "fb_usage_pct": round(fb / total, 3),
        "breaking_usage_pct": round(brk / total, 3),
        "offspeed_usage_pct": round(off / total, 3),
    }


# ---------------------------------------------------------------------------
# Data fetching: Batter HR events
# ---------------------------------------------------------------------------

def _fetch_batter_hr_events(player_id: int, season: int) -> list[dict]:
    """
    Fetch all HR events for a batter via statcast_batter().
    Returns list of dicts with pitcher_id, pitcher hand, pitch type, velo, etc.
    Pulls current season + prior season for rolling coverage.
    """
    cache_key = f"{player_id}_{season}"
    cached = _cache_get("batter_hr_events", cache_key, TTL_BATTER_HR_EVENTS)
    if cached is not None:
        return cached

    hr_events = []

    try:
        from pybaseball import statcast_batter

        # Current season
        start_cur = f"{season}-03-20"
        end_cur = datetime.now().strftime("%Y-%m-%d")
        df = statcast_batter(start_cur, end_cur, player_id)
        if df is not None and not df.empty:
            hrs = df[df["events"] == "home_run"]
            for _, row in hrs.iterrows():
                hr_events.append({
                    "pitcher_id": int(row.get("pitcher", 0)),
                    "p_throws": row.get("p_throws", "R"),
                    "pitch_type": row.get("pitch_type", "FF"),
                    "release_speed": float(row.get("release_speed", 0) or 0),
                    "release_spin_rate": float(row.get("release_spin_rate", 0) or 0),
                    "release_extension": float(row.get("release_extension", 0) or 0),
                    "game_date": str(row.get("game_date", "")),
                })

        # Prior season backfill
        start_prior = f"{season - 1}-03-20"
        end_prior = f"{season - 1}-10-01"
        df2 = statcast_batter(start_prior, end_prior, player_id)
        if df2 is not None and not df2.empty:
            hrs2 = df2[df2["events"] == "home_run"]
            for _, row in hrs2.iterrows():
                hr_events.append({
                    "pitcher_id": int(row.get("pitcher", 0)),
                    "p_throws": row.get("p_throws", "R"),
                    "pitch_type": row.get("pitch_type", "FF"),
                    "release_speed": float(row.get("release_speed", 0) or 0),
                    "release_spin_rate": float(row.get("release_spin_rate", 0) or 0),
                    "release_extension": float(row.get("release_extension", 0) or 0),
                    "game_date": str(row.get("game_date", "")),
                })

    except Exception as e:
        print(f"  [ARCHETYPE] Failed to fetch HR events for player {player_id}: {e}")

    _cache_set("batter_hr_events", cache_key, hr_events)
    return hr_events


# ---------------------------------------------------------------------------
# Data fetching: Pitcher arsenal
# ---------------------------------------------------------------------------

def _fetch_pitcher_arsenal_statcast(pitcher_id: int, season: int) -> dict | None:
    """
    Fetch pitcher's pitch arsenal from Statcast via pybaseball.
    Returns dict with velo, usage, spin, extension — or None.
    """
    cache_key = f"arsenal_{pitcher_id}_{season}"
    cached = _cache_get("pitcher_arsenal", cache_key, TTL_PITCHER_ARSENAL)
    if cached is not None:
        return cached

    try:
        from pybaseball import statcast_pitcher

        start = f"{season}-03-20"
        end = datetime.now().strftime("%Y-%m-%d")
        df = statcast_pitcher(start, end, pitcher_id)

        if df is None or df.empty:
            # Try prior season
            start = f"{season - 1}-03-20"
            end = f"{season - 1}-10-01"
            df = statcast_pitcher(start, end, pitcher_id)
            if df is None or df.empty:
                return None

        # Aggregate arsenal from pitch-level data
        pitch_types = df["pitch_type"].dropna()
        if pitch_types.empty:
            return None

        total_pitches = len(pitch_types)
        arsenal_usage = {}
        for pt in pitch_types.unique():
            arsenal_usage[pt] = len(pitch_types[pitch_types == pt]) / total_pitches

        # Fastball-specific stats
        fb_mask = df["pitch_type"].isin(FASTBALL_TYPES)
        fb_data = df[fb_mask]

        avg_fb_velo = float(fb_data["release_speed"].mean()) if not fb_data.empty and fb_data["release_speed"].notna().any() else 93.5
        avg_fb_spin = float(fb_data["release_spin_rate"].mean()) if not fb_data.empty and fb_data["release_spin_rate"].notna().any() else 2250.0
        avg_extension = float(df["release_extension"].mean()) if df["release_extension"].notna().any() else 6.2
        p_throws = df["p_throws"].mode().iloc[0] if not df["p_throws"].mode().empty else "R"

        mix = _classify_pitch_mix(arsenal_usage)

        result = {
            "avg_fb_velo": round(avg_fb_velo, 1),
            "avg_fb_spin": round(avg_fb_spin, 0),
            "avg_extension": round(avg_extension, 1),
            "p_throws": p_throws,
            **mix,
            "total_pitches": total_pitches,
            "source": "statcast",
        }

        _cache_set("pitcher_arsenal", cache_key, result)
        return result

    except Exception as e:
        print(f"  [ARCHETYPE] Statcast arsenal fetch failed for pitcher {pitcher_id}: {e}")
        return None


def _fetch_pitcher_arsenal_mlb_api(pitcher_id: int, season: int) -> dict | None:
    """
    Fallback: build a rough pitcher profile from MLB Stats API.
    Less granular than Statcast but always available.
    """
    cache_key = f"arsenal_mlb_{pitcher_id}_{season}"
    cached = _cache_get("pitcher_arsenal", cache_key, TTL_PITCHER_ARSENAL)
    if cached is not None:
        return cached

    try:
        url = f"https://statsapi.mlb.com/api/v1/people/{pitcher_id}"
        params = {"hydrate": f"stats(group=[pitching],type=[season],season={season})"}
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        people = data.get("people", [])
        if not people:
            return None

        person = people[0]
        p_throws = person.get("pitchHand", {}).get("code", "R")

        # We can't get pitch arsenal from this API, so use league-average
        # pitch mix adjusted by handedness
        result = {
            "avg_fb_velo": 93.5,  # league average — can't determine from this API
            "avg_fb_spin": 2250.0,
            "avg_extension": 6.2,
            "p_throws": p_throws,
            "fb_usage_pct": 0.53,
            "breaking_usage_pct": 0.28,
            "offspeed_usage_pct": 0.15,
            "source": "mlb_api_estimate",
            "confidence": 0.5,  # low confidence — missing arsenal data
        }

        _cache_set("pitcher_arsenal", cache_key, result)
        return result

    except Exception:
        return None


# ---------------------------------------------------------------------------
# Victim profile construction
# ---------------------------------------------------------------------------

def build_victim_profile(player_id: int, season: int) -> dict:
    """
    Build a "victim profile" for a batter — the archetype of pitcher
    they tend to hit home runs against.

    Returns a profile dict with the same dimensions as a pitcher profile,
    plus metadata (hr_count, confidence).
    """
    cache_key = f"victim_{player_id}_{season}"
    cached = _cache_get("victim_profiles", cache_key, TTL_VICTIM_PROFILE)
    if cached is not None:
        return cached

    hr_events = _fetch_batter_hr_events(player_id, season)

    if len(hr_events) < 3:
        # Not enough data — blend heavily toward league average
        profile = {**LEAGUE_AVG_VICTIM, "hr_count": len(hr_events), "confidence": 0.3}
        _cache_set("victim_profiles", cache_key, profile)
        return profile

    # Aggregate across all HR events
    # Per-event stats (from the pitch that was hit for a HR)
    velos = [e["release_speed"] for e in hr_events if e["release_speed"] > 0]
    spins = [e["release_spin_rate"] for e in hr_events if e["release_spin_rate"] > 0]
    extensions = [e["release_extension"] for e in hr_events if e["release_extension"] > 0]
    hands = [e["p_throws"] for e in hr_events if e.get("p_throws")]

    # Count pitch types from the HR pitches to get a sense of what pitch
    # types this batter crushes (this is different from the victim pitcher's
    # full arsenal but still informative)
    pitch_types = [e["pitch_type"] for e in hr_events if e.get("pitch_type")]
    pitch_type_counts = {}
    for pt in pitch_types:
        pitch_type_counts[pt] = pitch_type_counts.get(pt, 0) + 1

    # Now fetch full arsenal data for each unique victim pitcher
    # to build the "what kind of pitcher" profile
    victim_pitcher_ids = list(set(e["pitcher_id"] for e in hr_events if e["pitcher_id"] > 0))
    pitcher_arsenals = []

    # Count HRs per pitcher for weighting
    hr_per_pitcher = {}
    for e in hr_events:
        pid = e["pitcher_id"]
        if pid > 0:
            hr_per_pitcher[pid] = hr_per_pitcher.get(pid, 0) + 1

    for pid in victim_pitcher_ids[:30]:  # Cap at 30 unique pitchers to avoid too many API calls
        arsenal = _fetch_pitcher_arsenal_statcast(pid, season)
        if arsenal is None:
            arsenal = _fetch_pitcher_arsenal_mlb_api(pid, season)
        if arsenal:
            arsenal["_weight"] = hr_per_pitcher.get(pid, 1)
            pitcher_arsenals.append(arsenal)

    if pitcher_arsenals:
        # Weighted average across victim pitchers (weighted by HR count)
        total_weight = sum(a["_weight"] for a in pitcher_arsenals)

        avg_fb_velo = sum(a.get("avg_fb_velo", 93.5) * a["_weight"] for a in pitcher_arsenals) / total_weight
        fb_usage = sum(a.get("fb_usage_pct", 0.53) * a["_weight"] for a in pitcher_arsenals) / total_weight
        brk_usage = sum(a.get("breaking_usage_pct", 0.28) * a["_weight"] for a in pitcher_arsenals) / total_weight
        off_usage = sum(a.get("offspeed_usage_pct", 0.15) * a["_weight"] for a in pitcher_arsenals) / total_weight
        avg_spin = sum(a.get("avg_fb_spin", 2250) * a["_weight"] for a in pitcher_arsenals) / total_weight
        avg_ext = sum(a.get("avg_extension", 6.2) * a["_weight"] for a in pitcher_arsenals) / total_weight
    else:
        # Fall back to per-event data (less accurate but still useful)
        avg_fb_velo = sum(velos) / len(velos) if velos else 93.5
        avg_spin = sum(spins) / len(spins) if spins else 2250.0
        avg_ext = sum(extensions) / len(extensions) if extensions else 6.2
        fb_usage = 0.53
        brk_usage = 0.28
        off_usage = 0.15

    hand_r_pct = sum(1 for h in hands if h == "R") / max(len(hands), 1) if hands else 0.65

    # Determine confidence based on sample size
    hr_count = len(hr_events)
    n_pitchers = len(pitcher_arsenals)
    if hr_count >= 15 and n_pitchers >= 8:
        confidence = 1.0
    elif hr_count >= 8 and n_pitchers >= 4:
        confidence = 0.8
    elif hr_count >= 3:
        confidence = 0.6
    else:
        confidence = 0.3

    profile = {
        "avg_fb_velo": round(avg_fb_velo, 1),
        "fb_usage_pct": round(fb_usage, 3),
        "breaking_usage_pct": round(brk_usage, 3),
        "offspeed_usage_pct": round(off_usage, 3),
        "hand_R_pct": round(hand_r_pct, 2),
        "avg_fb_spin": round(avg_spin, 0),
        "avg_extension": round(avg_ext, 1),
        "hr_count": hr_count,
        "n_victim_pitchers": n_pitchers,
        "confidence": confidence,
    }

    _cache_set("victim_profiles", cache_key, profile)
    return profile


# ---------------------------------------------------------------------------
# Today's pitcher profile
# ---------------------------------------------------------------------------

def build_pitcher_profile(pitcher_id: int, season: int) -> dict:
    """
    Build the archetype vector for today's opposing pitcher.
    Tries Statcast first, falls back to MLB Stats API.
    """
    cache_key = f"profile_{pitcher_id}_{season}"
    cached = _cache_get("pitcher_profiles", cache_key, TTL_PITCHER_PROFILE)
    if cached is not None:
        return cached

    # Try Statcast arsenal first (full granularity)
    profile = _fetch_pitcher_arsenal_statcast(pitcher_id, season)

    if profile is None:
        # Fall back to MLB Stats API (less detailed)
        profile = _fetch_pitcher_arsenal_mlb_api(pitcher_id, season)

    if profile is None:
        # Last resort: league-average profile
        profile = {
            "avg_fb_velo": 93.5,
            "fb_usage_pct": 0.53,
            "breaking_usage_pct": 0.28,
            "offspeed_usage_pct": 0.15,
            "p_throws": "R",
            "avg_fb_spin": 2250.0,
            "avg_extension": 6.2,
            "source": "league_avg_default",
            "confidence": 0.2,
        }

    _cache_set("pitcher_profiles", cache_key, profile)
    return profile


# ---------------------------------------------------------------------------
# Similarity scoring
# ---------------------------------------------------------------------------

def archetype_similarity(victim_profile: dict, pitcher_profile: dict) -> float:
    """
    Compute 0-100 similarity between a batter's victim profile and
    today's opposing pitcher's profile.

    Uses weighted Euclidean distance on normalized dimensions.
    """
    weights = DIMENSION_WEIGHTS
    ranges = DIMENSION_RANGES

    weighted_sq_diff = 0.0
    total_weight = 0.0

    # Continuous dimensions
    for dim in ["avg_fb_velo", "fb_usage_pct", "breaking_usage_pct",
                "offspeed_usage_pct", "avg_fb_spin", "avg_extension"]:
        w = weights.get(dim, 0)
        if w == 0:
            continue

        lo, hi = ranges[dim]
        v_val = victim_profile.get(dim, (lo + hi) / 2)
        p_val = pitcher_profile.get(dim, (lo + hi) / 2)

        # Normalize to 0-1
        v_norm = max(0, min(1, (v_val - lo) / (hi - lo)))
        p_norm = max(0, min(1, (p_val - lo) / (hi - lo)))

        weighted_sq_diff += w * (v_norm - p_norm) ** 2
        total_weight += w

    # Handedness dimension (categorical — binary distance)
    hand_w = weights.get("handedness", 0.20)
    victim_r_pct = victim_profile.get("hand_R_pct", 0.65)
    pitcher_is_R = 1.0 if pitcher_profile.get("p_throws", "R") == "R" else 0.0

    # Distance: how far is this pitcher's hand from the batter's victim preference?
    hand_diff = abs(victim_r_pct - pitcher_is_R)
    weighted_sq_diff += hand_w * hand_diff ** 2
    total_weight += hand_w

    # Normalize by total weight and compute distance
    if total_weight > 0:
        weighted_sq_diff /= total_weight

    distance = sqrt(weighted_sq_diff)

    # Convert to 0-100 similarity (max possible distance is ~1.0)
    raw_similarity = max(0, min(100, (1 - distance) * 100))

    # Apply confidence scaling: if victim profile is low-confidence,
    # pull similarity toward 50 (neutral)
    confidence = victim_profile.get("confidence", 1.0)
    pitcher_confidence = pitcher_profile.get("confidence", 1.0)
    combined_confidence = min(confidence, pitcher_confidence)

    # Blend toward neutral based on confidence
    similarity = raw_similarity * combined_confidence + 50 * (1 - combined_confidence)

    return round(similarity, 1)


# ---------------------------------------------------------------------------
# Pitcher vulnerability scoring
# ---------------------------------------------------------------------------

def score_pitcher_vulnerability(
    pitcher_stats: dict,
    slate_ctx: dict | None = None,
) -> float:
    """
    Score how vulnerable a pitcher is to giving up HRs (0-100).
    Higher = more vulnerable = better for batter.

    With slate_ctx active, returns the within-slate percentile rank for
    this pitcher's name — which fixes the HR/9 cap problem where the 5
    worst HR-allowing pitchers all clustered at score ≈ 70.

    Without slate_ctx, falls back to fixed-anchor scaling (HR/9 cap
    raised from 3.0 to 4.5 so 4+ HR/9 outliers can still distinguish
    themselves).
    """
    pname = pitcher_stats.get("name", "")
    if (
        slate_ctx
        and slate_ctx.get("active")
        and pname in slate_ctx.get("pitcher_pct", {})
    ):
        return slate_ctx["pitcher_pct"][pname]

    # 2026-05-02 fix (audit HIGH #3 sibling): was using `.get(k, league_mean)`
    # for every input, then averaging. Silently injected league mean for
    # missing data with no provenance flag distinguishing measured-1.2
    # from missing-1.2. Now: skip-on-missing per component. Score is the
    # average of however-many components were measured; falls back to 50
    # only when nothing was measured.
    scores = []

    hr9 = pitcher_stats.get("hr_per_9")
    if hr9 is not None and hr9 > 0:
        # HR/9: 0–4.5 → 0–100 (higher = more vulnerable). Cap raised from
        # 3.0 to 4.5 so 4+ HR/9 outliers can rank above merely-bad pitchers.
        scores.append(max(0, min(100, (hr9 / 4.5) * 100)))

    era = pitcher_stats.get("era")
    if era is not None and era > 0:
        # ERA: 2.0–6.0 → 0–100
        scores.append(max(0, min(100, (era - 2.0) / 4.0 * 100)))

    hh = pitcher_stats.get("hard_hit_pct_allowed")
    if hh is not None and hh > 0:
        # Hard-hit% allowed: 25–50% → 0–100
        scores.append(max(0, min(100, (hh - 25) / 25 * 100)))

    k9 = pitcher_stats.get("k_per_9")
    if k9 is not None and k9 > 0:
        # K/9 inverse: range ~4–14, higher K = less vulnerable
        scores.append(max(0, min(100, (14 - k9) / 10 * 100)))

    # IP as a season sample size indicator (don't trust stats on < 10 IP).
    # Only fires when IP is measured + low; missing IP no longer collapses
    # to the league-mean 50 default (was `or 50` previously).
    ip = pitcher_stats.get("ip")
    if ip is not None and ip < 10:
        return 50.0   # low sample — pull toward neutral

    return round(sum(scores) / len(scores), 1) if scores else 50.0


# ---------------------------------------------------------------------------
# Combined matchup score v2
# ---------------------------------------------------------------------------

def score_matchup_v2(
    batter: dict,
    pitcher_stats: dict,
    victim_profile: dict | None,
    pitcher_profile: dict | None,
    vulnerability_weight: float = 0.50,
    similarity_weight: float = 0.50,
    slate_ctx: dict | None = None,
    batter_team: str | None = None,
) -> float:
    """
    Three-signal matchup score (when slate_ctx + Vegas data present).

    Signal 1: Pitcher vulnerability (slate-rank-aware HR/9 + ERA + HH% + FB% allowed)
    Signal 2: Archetype similarity (victim profile vs today's pitcher)
    Signal 3: Vegas implied team total percentile (game environment) —
              only added when slate_ctx has team_total_pct AND batter_team given.

    Handedness is captured inside archetype similarity (weight 0.20), so the
    +5 platoon bonus that used to live here was a double-count and has been
    removed. Reverse-platoon signal still flows via similarity weighting.

    Returns 0-100.
    """
    # Signal 1: Pitcher vulnerability — slate-rank-aware (now includes FB%)
    vulnerability = score_pitcher_vulnerability(pitcher_stats, slate_ctx=slate_ctx)

    # Signal 2: Archetype similarity
    if victim_profile and pitcher_profile:
        similarity = archetype_similarity(victim_profile, pitcher_profile)
    else:
        # No profile data — fall back to neutral
        similarity = 50.0

    # Signal 3 (optional): Vegas implied team total percentile
    team_total_pct = None
    if (
        slate_ctx
        and batter_team
        and slate_ctx.get("team_total_pct")
        and batter_team in slate_ctx["team_total_pct"]
    ):
        team_total_pct = slate_ctx["team_total_pct"][batter_team]

    # Signal 4 (added 2026-04-30, anchors retightened 2026-05-01):
    # Batter wOBA vs. pitcher handedness. Empirical HR rate climbs 4.5x
    # across woba quintiles but the original (0.280, 0.420) anchors only
    # shifted matchup_score by ~2 points across the same range. Pulled in
    # to (0.290, 0.395) so the score curve actually slopes through the
    # signal-rich part of the distribution. Same anchors as v1.
    woba_raw = batter.get("woba_vs_hand", batter.get("woba"))
    woba_score = None
    if woba_raw is not None and woba_raw > 0:
        woba_score = max(0.0, min(100.0, (woba_raw - 0.290) / (0.395 - 0.290) * 100.0))

    # Blend — variable arity based on which optional signals are available.
    # vulnerability + similarity always present (fall back to neutral 50 if
    # data missing). team_total_pct and woba_score are added when available.
    signals = [vulnerability, similarity]
    if team_total_pct is not None:
        signals.append(team_total_pct)
    if woba_score is not None:
        signals.append(woba_score)
    raw = sum(signals) / len(signals)


    # Elite-pitcher dampening
    # Even if archetype matches perfectly, we don't want to bet against aces.
    # NOTE: when slate_ctx is active, vulnerability is a percentile rank, so
    # "elite" thresholds map to the bottom of today's slate (rank < 25 = the
    # day's least-vulnerable starters).
    if vulnerability < 25:
        raw = raw * 0.70   # 30% penalty for elite/least-vulnerable pitchers
    elif vulnerability < 40:
        raw = raw * 0.85   # 15% penalty for good pitchers

    return round(min(100, max(0, raw)), 1)


# ---------------------------------------------------------------------------
# Batch operations for generate_picks.py integration
# ---------------------------------------------------------------------------

def build_pitcher_profiles_batch(
    pitcher_ids: dict[str, int],
    season: int,
) -> dict[str, dict]:
    """
    Build pitcher profiles for all starting pitchers on today's slate.
    pitcher_ids: {pitcher_name: pitcher_id}
    Returns: {pitcher_name: profile_dict}
    """
    profiles = {}
    for name, pid in pitcher_ids.items():
        if pid and pid > 0:
            profiles[name] = build_pitcher_profile(pid, season)
        else:
            profiles[name] = {
                "avg_fb_velo": 93.5,
                "fb_usage_pct": 0.53,
                "breaking_usage_pct": 0.28,
                "offspeed_usage_pct": 0.15,
                "p_throws": "R",
                "avg_fb_spin": 2250.0,
                "avg_extension": 6.2,
                "source": "unknown_pitcher_default",
                "confidence": 0.1,
            }
    return profiles


def build_victim_profiles_batch(
    batter_ids: list[tuple[str, int]],
    season: int,
) -> dict[int, dict]:
    """
    Build victim profiles for a batch of batters.
    batter_ids: [(name, player_id), ...]
    Returns: {player_id: victim_profile_dict}
    """
    profiles = {}
    for name, pid in batter_ids:
        if pid and pid > 0:
            profiles[pid] = build_victim_profile(pid, season)
    return profiles


# ---------------------------------------------------------------------------
# CLI for testing / inspection
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Inspect pitcher archetype profiles")
    parser.add_argument("--batter-id", type=int, help="MLB player ID for batter")
    parser.add_argument("--pitcher-id", type=int, help="MLB player ID for pitcher")
    parser.add_argument("--season", type=int, default=2026)
    args = parser.parse_args()

    if args.batter_id:
        vp = build_victim_profile(args.batter_id, args.season)
        print(json.dumps(vp, indent=2))

    if args.pitcher_id:
        print(f"\nBuilding pitcher profile for pitcher {args.pitcher_id}...")
        pp = build_pitcher_profile(args.pitcher_id, args.season)
        print(json.dumps(pp, indent=2))

    if args.batter_id and args.pitcher_id:
        vp = build_victim_profile(args.batter_id, args.season)
        pp = build_pitcher_profile(args.pitcher_id, args.season)
        sim = archetype_similarity(vp, pp)
        print(f"\nArchetype similarity: {sim}/100")
