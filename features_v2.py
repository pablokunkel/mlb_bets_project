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
from datetime import datetime, timedelta
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
TTL_RECENT_STATCAST = 86400  # 24h; cache key includes as_of_date so backfill targets aren't poisoned by noon runs


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
# B6a recent quality-contact bulk fetcher (2026-05-21)
# ---------------------------------------------------------------------------
# Pulls 14 days of pitch-level Statcast in ONE bulk call (no per-player
# fan-out -- that path hung the noon run 2026-04-29). Aggregates per-batter
# to three rolling 14d quality-contact metrics that feed score_power when
# USE_RECENT_STATCAST_BLEND is on:
#
#   recent_barrel_real_14d  : real barrel events / batted balls (%)
#                              (launch_speed_angle == 6 is the Statcast
#                              "barrel" classification — exact, not synthetic)
#   recent_xwoba_contact_14d: mean estimated_woba_using_speedangle over
#                              contact events (PA-ending batted balls)
#   recent_iso_14d          : (TB - H) / AB in window, where AB excludes
#                              walks/HBP/SF/SH
#
# As-of-date-aware: the cache key includes the date so the 2025-season
# backfill can target historical dates without colliding with daily
# noon-run cache. The 14d window is [as_of_date - 14d, as_of_date) --
# strictly before as_of_date, so games played on that date itself are
# excluded (matches B4's pitcher-recency window semantics and prevents
# look-ahead bias when reconstructing historical predictions).

# Statcast events that count as plate-appearance-ending. Used to compute
# AB / H / TB for recent_iso_14d. Walks, HBP, sac flies, sac bunts, and
# catcher interference do NOT count as ABs.
_PA_AB_EVENTS = {
    "single", "double", "triple", "home_run",
    "field_out", "strikeout", "force_out",
    "grounded_into_double_play", "fielders_choice",
    "fielders_choice_out", "double_play", "triple_play",
    "field_error", "strikeout_double_play",
    # Hit-into-play outs that are sometimes labeled separately
    "sac_fly_double_play",   # batter gets credited an AB on this rare combo
}
_HIT_EVENTS = {"single", "double", "triple", "home_run"}
_TB_PER_HIT = {"single": 1, "double": 2, "triple": 3, "home_run": 4}


def _aggregate_recent_statcast(df, min_batted_balls: int = 10) -> dict[int, dict]:
    """Aggregate a pitch-level Statcast DataFrame to per-batter 14d metrics.

    Filters to PA-ending events and computes:
      - recent_barrel_real_14d  : (launch_speed_angle == 6) count / batted_balls
      - recent_xwoba_contact_14d: mean(estimated_woba_using_speedangle) on contact
      - recent_iso_14d          : (TB - H) / AB

    Pure-function so backfill can call it with any historical date's
    DataFrame; no caching here (caller manages cache).
    """
    if df is None or df.empty:
        return {}

    import pandas as pd

    # Per-pitch rows -> PA-ending rows. `events` is the terminal event for
    # the at-bat; non-terminal pitches (balls, called strikes, fouls) have
    # events == NaN. Keep only PA-terminal rows.
    if "events" not in df.columns or "batter" not in df.columns:
        return {}
    pa = df.dropna(subset=["events", "batter"]).copy()
    if pa.empty:
        return {}

    # Normalize batter id to int (statcast returns float in some rows).
    pa["batter"] = pa["batter"].astype("int64", errors="ignore")

    out: dict[int, dict] = {}
    for bid, grp in pa.groupby("batter"):
        bid = int(bid)
        events = grp["events"]

        # Batted-ball events = anything with a non-null bb_type, OR any
        # hit event (covers a few corner cases like inside-the-park HRs).
        if "bb_type" in grp.columns:
            bb_mask = grp["bb_type"].notna()
        else:
            bb_mask = events.isin(_HIT_EVENTS)
        n_bb = int(bb_mask.sum())

        ab_mask = events.isin(_PA_AB_EVENTS)
        n_ab = int(ab_mask.sum())

        if n_bb < min_batted_balls and n_ab < min_batted_balls:
            continue

        entry: dict = {}

        # Barrel%: launch_speed_angle classification 6 = "barrel" (Statcast's
        # canonical exact barrel definition). Denominator is batted balls.
        if "launch_speed_angle" in grp.columns and n_bb > 0:
            n_barrel = int(((grp["launch_speed_angle"] == 6) & bb_mask).sum())
            entry["recent_barrel_real_14d"] = round(n_barrel / n_bb * 100.0, 2)

        # xwOBA on contact: mean of estimated_woba_using_speedangle on
        # batted-ball events. (Statcast computes this column from launch
        # speed + angle, NaN for non-contact pitches.)
        if "estimated_woba_using_speedangle" in grp.columns:
            xwoba_series = grp["estimated_woba_using_speedangle"].dropna()
            if len(xwoba_series) >= min_batted_balls:
                entry["recent_xwoba_contact_14d"] = round(float(xwoba_series.mean()), 3)

        # ISO over the window: SLG - AVG, computed cleanly as (TB - H) / AB.
        # Skip when AB sample is too thin (avoids ISO inflation from a tiny
        # sample with one HR).
        if n_ab >= min_batted_balls:
            n_hits = int(events.isin(_HIT_EVENTS).sum())
            tb = sum(_TB_PER_HIT.get(e, 0) for e in events)
            iso = (tb - n_hits) / n_ab if n_ab > 0 else None
            if iso is not None and iso >= 0:
                entry["recent_iso_14d"] = round(iso, 3)

        if entry:
            out[bid] = entry

    return out


def fetch_batter_recent_statcast_14d(
    as_of_date: str | None = None,
    window_days: int = 14,
    min_batted_balls: int = 10,
) -> dict[int, dict]:
    """Bulk-pull 14d of pitch-level Statcast and aggregate per-batter.

    Returns {player_id: {recent_barrel_real_14d, recent_xwoba_contact_14d, recent_iso_14d}}.

    *as_of_date*  — YYYY-MM-DD; window is [as_of_date - window_days, as_of_date)
                    so games played ON as_of_date are excluded. Defaults to today.
    *window_days* — calendar-day window length. Default 14 per B6a spec.
    *min_batted_balls* — per-batter sample threshold below which we drop the
                    entry rather than report a noisy aggregate.

    Cache key includes as_of_date so the 2025 backfill can target historical
    dates without colliding with the daily noon-run cache. 24h TTL.

    Returns {} on any failure (so callers can treat as "no recent data" and
    skip-on-missing through score_power).
    """
    end_date = as_of_date or datetime.now().strftime("%Y-%m-%d")
    try:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        print(f"  [features_v2] bad as_of_date {end_date!r}; using today")
        end_dt = datetime.now()
        end_date = end_dt.strftime("%Y-%m-%d")
    # Window is [start_dt, end_dt - 1 day] inclusive — pybaseball.statcast()
    # is inclusive on both ends. end_dt is the noon-run date itself; we
    # exclude it so today's in-progress games can't bias the rolling stat.
    last_completed = (end_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    start_dt = (end_dt - timedelta(days=window_days)).strftime("%Y-%m-%d")

    cache_key = f"recent_statcast_{window_days}d_{end_date}"
    cached = _cache_get("bulk_savant", cache_key, TTL_RECENT_STATCAST)
    if cached is not None:
        return {int(k): v for k, v in cached.items()}

    try:
        from pybaseball import statcast
        df = statcast(start_dt=start_dt, end_dt=last_completed, verbose=False)
    except Exception as e:
        print(f"  [features_v2] bulk recent Statcast fetch failed "
              f"({start_dt}..{last_completed}): {e}")
        return {}

    out = _aggregate_recent_statcast(df, min_batted_balls=min_batted_balls)

    if out:
        _cache_set("bulk_savant", cache_key, {str(k): v for k, v in out.items()})
    return out


# ---------------------------------------------------------------------------
# Batter park archetype centroid — Phase 1 (2026-05-25)
# ---------------------------------------------------------------------------
# Background: today's score_park is a handedness-weighted lookup of three
# numbers per venue (hr_pf_overall / hr_pf_lhb / hr_pf_rhb). It says nothing
# about whether THIS specific batter has historically gone deep in parks
# that look like today's park. The archetype signal builds a per-batter
# centroid of the park-feature vectors at their career HR venues, then
# scores today's park by L2 distance to that centroid.
#
# See docs/park_archetype_design.md for the full math + rollout.
#
# Feature vector (6 elements). Pulled entirely from existing data — no
# new sources in Phase 1. The wishlist features cf_distance / lf_distance
# / rf_distance / cf_height / pull_lf_factor / oppo_rf_factor / elevation
# / foul_territory_idx / roof_status are NOT in any existing table and
# are deliberately dropped (documented in the design doc). If Phase 3
# shows the 6-feature vector has signal but is leaving lift on the table,
# the design commits to adding a park_dimensions table as a follow-up.
PARK_FEATURE_KEYS: tuple[str, ...] = (
    "hr_pf_overall",
    "hr_pf_lhb",
    "hr_pf_rhb",
    "lhb_advantage",     # = hr_pf_lhb - hr_pf_rhb (dimension asymmetry)
    "cf_bearing_sin",    # CF compass orientation, sin component
    "cf_bearing_cos",    # CF compass orientation, cos component
)

# Below this career-HR count, the builder returns None and score_park
# skips the archetype term. Swept in the harness at 5 / 10 / 20.
PARK_ARCHETYPE_MIN_HRS = 10


def _compute_park_feature_stats() -> dict[str, tuple[float, float]]:
    """Compute (mean, std) per PARK_FEATURE_KEYS across the 30-park MLB
    universe. Used to z-score features before L2 centroiding/distance.

    Imports the seed park factors + CF bearings lazily so this module
    doesn't pull pandas at import time when scoring runs (the lazy
    import mirrors fetch_batter_recent_statcast_14d's pybaseball pattern).
    """
    try:
        from etl.park_factors_seed import get_seed_dataframe
        from score_batters import PARK_CF_BEARING
    except Exception:
        # Importable in any environment — return identity scaling if the
        # seed module isn't reachable (returns mean 0, std 1 -> z-score
        # is the raw value). Smoke test pin_park_archetype_constants
        # verifies the stats dict is well-formed in a real environment.
        return {k: (0.0, 1.0) for k in PARK_FEATURE_KEYS}

    df = get_seed_dataframe()
    import math
    vectors: list[list[float]] = []
    for _, row in df.iterrows():
        venue = row["venue"]
        bearing_deg = PARK_CF_BEARING.get(venue, 0)
        rad = math.radians(bearing_deg)
        vectors.append([
            float(row["hr_pf_overall"]),
            float(row["hr_pf_lhb"]),
            float(row["hr_pf_rhb"]),
            float(row["hr_pf_lhb"]) - float(row["hr_pf_rhb"]),
            math.sin(rad),
            math.cos(rad),
        ])
    if not vectors:
        return {k: (0.0, 1.0) for k in PARK_FEATURE_KEYS}

    stats: dict[str, tuple[float, float]] = {}
    for j, key in enumerate(PARK_FEATURE_KEYS):
        col = [v[j] for v in vectors]
        mean = sum(col) / len(col)
        var = sum((x - mean) ** 2 for x in col) / len(col)
        std = var ** 0.5 if var > 0 else 1.0
        stats[key] = (mean, std)
    return stats


# Computed once at import time. Stable per (park_factors_seed +
# PARK_CF_BEARING) pair; refresh manually if either changes.
PARK_FEATURE_STATS: dict[str, tuple[float, float]] = _compute_park_feature_stats()


def build_park_feature_vector(
    venue: str,
    park_factors_lookup: dict[str, dict] | None = None,
) -> list[float] | None:
    """Build the 6-element standardized park-feature vector for *venue*.

    Returns the z-scored vector (one float per PARK_FEATURE_KEYS entry)
    or None if the venue isn't in the park-factors lookup. Used both by
    `compute_batter_park_archetype` (centroiding HR venues) and by
    `_compute_park_archetype_match` (scoring today's park against
    a batter's centroid).

    *park_factors_lookup* — optional pre-built dict
        {venue: {"hr_pf_overall": ..., "hr_pf_lhb": ..., "hr_pf_rhb": ...}}.
        When None, loads the seed table on the fly.
    """
    import math

    if park_factors_lookup is None:
        try:
            from etl.park_factors_seed import get_seed_dataframe
            df = get_seed_dataframe()
            park_factors_lookup = {
                str(r["venue"]): {
                    "hr_pf_overall": float(r["hr_pf_overall"]),
                    "hr_pf_lhb": float(r["hr_pf_lhb"]),
                    "hr_pf_rhb": float(r["hr_pf_rhb"]),
                }
                for _, r in df.iterrows()
            }
        except Exception:
            return None

    pf = park_factors_lookup.get(venue)
    if pf is None:
        return None

    try:
        from score_batters import PARK_CF_BEARING
        bearing_deg = PARK_CF_BEARING.get(venue, 0)
    except Exception:
        bearing_deg = 0
    rad = math.radians(bearing_deg)

    raw = {
        "hr_pf_overall": pf["hr_pf_overall"],
        "hr_pf_lhb": pf["hr_pf_lhb"],
        "hr_pf_rhb": pf["hr_pf_rhb"],
        "lhb_advantage": pf["hr_pf_lhb"] - pf["hr_pf_rhb"],
        "cf_bearing_sin": math.sin(rad),
        "cf_bearing_cos": math.cos(rad),
    }

    out: list[float] = []
    for key in PARK_FEATURE_KEYS:
        mean, std = PARK_FEATURE_STATS.get(key, (0.0, 1.0))
        z = (raw[key] - mean) / std if std > 0 else 0.0
        out.append(z)
    return out


def _build_park_factors_lookup() -> dict[str, dict]:
    """Load park factors keyed by venue from the seed table (or the DB
    if that ever gets richer). Lazy + defensive: returns {} on error so
    callers degrade to None rather than crash."""
    try:
        from etl.park_factors_seed import get_seed_dataframe
        df = get_seed_dataframe()
        return {
            str(r["venue"]): {
                "hr_pf_overall": float(r["hr_pf_overall"]),
                "hr_pf_lhb": float(r["hr_pf_lhb"]),
                "hr_pf_rhb": float(r["hr_pf_rhb"]),
            }
            for _, r in df.iterrows()
        }
    except Exception as e:
        print(f"  [features_v2] park factors lookup failed: {e}")
        return {}


def compute_batter_park_archetype(
    player_ids: list[int],
    as_of_date: str | None = None,
    season: int | None = None,
    db_path: str | None = None,
) -> dict[int, dict]:
    """Build per-batter park-feature centroids from career HRs.

    Returns {player_id: {"centroid": list[float] | None, "n_hrs_used": int}}.
    For each batter in *player_ids*:

      1. Fetch every HR event with game_date < as_of_date (honest as-of-date).
      2. JOIN to daily_slate.venue via game_pk to learn each HR's venue.
         HRs whose venue can't be resolved are dropped.
      3. For each HR, build the standardized 6-element park-feature vector
         and weight it by `1 / park_neutral_hr_factor` (HRs at Coors get
         less weight than HRs at Petco — the design's per-HR weighting
         policy, see docs/park_archetype_design.md).
      4. Centroid = weighted mean of those vectors.

    None+skip policy: if a batter has fewer than PARK_ARCHETYPE_MIN_HRS
    HRs (with resolvable venues) before *as_of_date*, the centroid is
    returned as None. The caller (score_park) treats None as "skip the
    archetype term" — NOT a league-average fallback. See
    docs/park_archetype_design.md for the rationale.

    *as_of_date* — YYYY-MM-DD. None (default) = today = production behavior.
    *season*     — currently unused (kept for signature consistency with
                   the rest of the features_v2 builders); HR events span
                   career, not season.
    *db_path*    — override for test injection. None = production DB.

    The function is callable today (Phase 1) and returns real centroids
    when fed a populated DB. It is NOT wired into nightly ETL until Phase 2
    — see the # TODO Phase 2: marker in etl/etl_nightly.py.
    """
    out: dict[int, dict] = {}
    want = {int(b) for b in player_ids if b and b > 0}
    if not want:
        return out

    cutoff = as_of_date or datetime.now().strftime("%Y-%m-%d")

    # Resolve DB path (matches etl.db.DB_PATH lookup but avoids circular
    # import at module load by deferring it).
    try:
        from etl.db import DB_PATH as _DB_PATH
        path = db_path or str(_DB_PATH)
    except Exception:
        return {bid: {"centroid": None, "n_hrs_used": 0} for bid in want}

    park_lookup = _build_park_factors_lookup()
    if not park_lookup:
        return {bid: {"centroid": None, "n_hrs_used": 0} for bid in want}

    # Pre-compute (vector, weight) per venue once; HRs at the same venue
    # share the per-event payload.
    venue_payload: dict[str, tuple[list[float], float]] = {}
    for venue, pf in park_lookup.items():
        vec = build_park_feature_vector(venue, park_lookup)
        if vec is None:
            continue
        # park_neutral_hr_factor = hr_pf_overall / 100. Weight = 1 / factor
        # so HRs at high-factor parks (Coors=130) contribute less.
        factor = pf["hr_pf_overall"] / 100.0
        weight = 1.0 / factor if factor > 0 else 1.0
        venue_payload[venue] = (vec, weight)

    import sqlite3
    from pathlib import Path as _Path
    if not _Path(path).exists():
        return {bid: {"centroid": None, "n_hrs_used": 0} for bid in want}

    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        want_list = sorted(want)
        placeholders = ", ".join("?" for _ in want_list)
        rows = conn.execute(
            f"""
            SELECT bhe.batter_id, COALESCE(ds.venue, '') AS venue
            FROM batter_hr_events bhe
            LEFT JOIN daily_slate ds ON ds.game_pk = bhe.game_pk
            WHERE bhe.game_date < ?
              AND bhe.batter_id IN ({placeholders})
            """,
            (cutoff, *want_list),
        ).fetchall()
        conn.close()
    except Exception as e:
        print(f"  [features_v2] park-archetype DB read failed: {e}")
        return {bid: {"centroid": None, "n_hrs_used": 0} for bid in want}

    # Group HRs by batter, collect weighted vectors for each.
    per_batter_events: dict[int, list[tuple[list[float], float]]] = {}
    for r in rows:
        bid = int(r["batter_id"])
        if bid not in want:
            continue
        venue = r["venue"]
        if not venue:
            continue
        payload = venue_payload.get(venue)
        if payload is None:
            continue
        per_batter_events.setdefault(bid, []).append(payload)

    for bid in want:
        events = per_batter_events.get(bid, [])
        if len(events) < PARK_ARCHETYPE_MIN_HRS:
            # None+skip policy. The caller treats None as "no signal, fall
            # back to base handedness park-factor logic." Don't insert a
            # league-mean fallback here.
            out[bid] = {"centroid": None, "n_hrs_used": len(events)}
            continue

        # Weighted mean across the (vector, weight) pairs.
        dim = len(PARK_FEATURE_KEYS)
        accum = [0.0] * dim
        total_w = 0.0
        for vec, w in events:
            for j, v in enumerate(vec):
                accum[j] += w * v
            total_w += w
        if total_w <= 0:
            out[bid] = {"centroid": None, "n_hrs_used": len(events)}
            continue
        centroid = [a / total_w for a in accum]
        out[bid] = {"centroid": centroid, "n_hrs_used": len(events)}

    return out


# ---------------------------------------------------------------------------
# Batter pitch-type SLG splits — Phase 1 scaffolding (2026-05-25)
# ---------------------------------------------------------------------------
# Background: today's score_matchup is blind to batter pitch-type
# preferences vs the specific pitcher's arsenal mix. The new signal blends
# pitcher arsenal usage with batter SLG-by-pitch-type-group to produce an
# expected SLG vs. today's arsenal. See docs/pitch_type_archetype_design.md.
#
# Three pitch-type buckets (mirror pitcher_profile.FASTBALL_TYPES /
# BREAKING_TYPES / OFFSPEED_TYPES):
#   FB = FF (4-seam), SI (sinker), FC (cutter), FT (2-seam), FA (generic)
#   BR = SL (slider), CU (curveball), KC (knuckle-curve), SV (slurve),
#        ST (sweeper), CS, EP
#   OS = CH (changeup), FS (splitter), FO (forkball), KN (knuckleball),
#        SC (screwball)
#
# Per-group PA threshold for the arsenal sub-signal: below this many BB
# in a pitch-type group, _compute_xslg_vs_arsenal returns None (the term
# is skipped from the matchup composite) instead of imputing league-avg.
#
# Policy change 2026-05-26: previously this used a LEAGUE_AVG_PITCH_TYPE_SLG
# fallback (.420/.350/.380 from the 2024 Statcast leaderboard), but that
# flattens every small-sample batter to a neutral xSLG and inflates their
# matchup score above where their actual signal would land. We follow the
# same convention as score_form: "no data" = "no opinion," not "average
# opinion." The League-avg anchors are documented in the design doc
# (docs/pitch_type_archetype_design.md, section "Sample-size handling")
# for future reference but no longer wired into scoring.
PITCH_TYPE_SPLIT_MIN_BB = 30


def fetch_batter_pitch_type_splits(
    player_ids: list[int],
    as_of_date: str | None = None,
    season: int | None = None,
) -> dict[int, dict]:
    """Build batter SLG splits by pitch-type group (FB/BR/OS).

    Returns {player_id: {fb_slg, fb_pa, br_slg, br_pa, os_slg, os_pa}}.
    Season-to-date through (as_of_date - 1 day); strictly excludes
    games on/after as_of_date so historical reconstruction is honest.

    *as_of_date* — YYYY-MM-DD. None (default) = today = production behavior.
    *season*     — int. None (default) = derive from as_of_date or today.

    **Phase 1 (this PR): signature + skeleton only.** The body is a
    `# TODO Phase 2:` stub that returns {}. Phase 2 will wire this to
    the bulk-Statcast-pull-and-slice pattern used by
    fetch_batter_recent_statcast_14d:

      1. Bulk-pull pitch-level Statcast for the season window
         [season-03-20, as_of_date) via `pybaseball.statcast(start, end)`.
      2. Group by batter and pitch_type, aggregate to per-bucket
         (FB/BR/OS) SLG via the standard (TB / AB) formula.
      3. Stamp `*_pa` with the batted-ball count for sample-size gating.
      4. Cache to data/cache/features_v2/pitch_type_splits/ with
         cache key including as_of_date (24h TTL, mirrors
         fetch_batter_recent_statcast_14d).

    Phase 2 will also persist the result to `batter_pitch_type_splits`
    in SQLite and to `pick_inputs.{fb_slg, br_slg, os_slg}` for the
    backtest harness.

    Until Phase 2 lands, callers get an empty dict — and the scoring
    path skips the arsenal sub-signal via its USE_ARSENAL_SUBSIGNAL=False
    guard, so this no-op is safe in production.
    """
    # TODO Phase 2: implement bulk Statcast pull + per-batter aggregation.
    # See features_v2.fetch_batter_recent_statcast_14d for the pattern.
    return {}


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


# ---------------------------------------------------------------------------
# Form archetype centroid builder (Phase 1 — added 2026-05-26)
# ---------------------------------------------------------------------------
# Per-batter "pre-HR state-of-play" centroid. Mirrors the archetype pattern
# in pitcher_profile._build_victim_profiles_from_db: for each HR a batter
# has hit, snapshot their state-of-play in the window_days days BEFORE the
# HR (strictly excluding HR-day games). Aggregate to a centroid feature
# vector. Today's same-features vector is then compared via L2 distance
# at scoring time (score_batters._compute_form_archetype_match).
#
# The vector is built from features that DO NOT OVERLAP with score_form's
# base inputs (recent_hr_10g, recent_iso_30g, ev_trend). See
# docs/form_archetype_design.md for the non-overlap-with-Form-inputs
# guardrail and the per-feature rationale.
#
# Phase 1 ships this builder callable but uncalled — no nightly ETL
# wiring, no production use. Phase 2 adds the backfill orchestrator;
# Phase 3 runs the backtest; Phase 4 enables it in production.
# ---------------------------------------------------------------------------

# Pre-HR state-of-play feature names. ORDER MATTERS — the centroid is
# stored as a positional JSON list, so changing the order would silently
# corrupt the L2 distance computation at read time. Append-only; if a
# feature is dropped in Phase 3, leave its slot as None rather than
# re-ordering.
FORM_ARCHETYPE_FEATURES = [
    "recent_xwoba_14d",       # contact-quality reading, 14d window
    "recent_barrel_pct_14d",  # quality-contact frequency
    "recent_swstr_pct_7d",    # plate-discipline / whiff rate
    "recent_pull_pct_14d",    # pull-direction signal
    "days_since_last_hr",     # rest-pattern marker
    "days_since_off",         # rest from baseball entirely
    "recent_avg_30g",         # state-descriptor (NOT load-bearing in B11 score_form)
]

FORM_ARCHETYPE_MIN_HRS = 10           # min career HRs in lookback window for centroid
FORM_ARCHETYPE_LOOKBACK_SEASONS = 2   # how many seasons of HRs to use
FORM_ARCHETYPE_DEFAULT_WINDOW = 7     # 7-day pre-HR snapshot (Phase 1 default)


def _per_hr_state_snapshot(
    df_window,  # pitch-level Statcast DataFrame for a single batter's pre-HR window
    hr_date: str,
    prev_hr_date: str | None,
    last_off_date: str | None,
) -> dict | None:
    """Compute the 7-element state-of-play vector for one pre-HR window.

    Returns a dict keyed by FORM_ARCHETYPE_FEATURES, or None if the window
    has insufficient data (< 5 PA-ending events).

    *df_window* — already filtered to [hr_date - window_days, hr_date) for
                  this one batter.
    *hr_date*   — the HR date itself ('YYYY-MM-DD'); used for the rest-day
                  computations.
    *prev_hr_date* — most recent HR before this one (None = first season HR).
    *last_off_date* — most recent off-day before hr_date (None = unknown).
    """
    if df_window is None or df_window.empty:
        return None

    import pandas as pd

    # PA-terminal events only — non-terminal pitches have events == NaN.
    if "events" not in df_window.columns:
        return None
    pa = df_window.dropna(subset=["events"]).copy()
    if len(pa) < 5:
        return None  # window too thin to characterize a state-of-play

    # Batted-ball mask
    if "bb_type" in pa.columns:
        bb_mask = pa["bb_type"].notna()
    else:
        bb_mask = pa["events"].isin(_HIT_EVENTS)
    n_bb = int(bb_mask.sum())

    out: dict[str, float | int | None] = {f: None for f in FORM_ARCHETYPE_FEATURES}

    # 1. recent_xwoba_14d — mean xwOBA over contact events (14d window)
    if "estimated_woba_using_speedangle" in pa.columns:
        xwoba_series = pa["estimated_woba_using_speedangle"].dropna()
        if len(xwoba_series) >= 5:
            out["recent_xwoba_14d"] = round(float(xwoba_series.mean()), 3)

    # 2. recent_barrel_pct_14d — exact Statcast barrel (launch_speed_angle == 6)
    if "launch_speed_angle" in pa.columns and n_bb > 0:
        n_barrel = int(((pa["launch_speed_angle"] == 6) & bb_mask).sum())
        out["recent_barrel_pct_14d"] = round(n_barrel / n_bb * 100.0, 2)

    # 3. recent_swstr_pct_7d — swinging-strike% across all pitches in window.
    # Use the raw pitch-level df (not the PA-terminal slice) so balls/swings
    # are properly denominator-counted. swstr = swinging strike per pitch.
    if "description" in df_window.columns:
        descs = df_window["description"].dropna()
        if len(descs) >= 10:
            n_swstr = int(descs.isin([
                "swinging_strike",
                "swinging_strike_blocked",
                "missed_bunt",
            ]).sum())
            out["recent_swstr_pct_7d"] = round(n_swstr / len(descs) * 100.0, 2)

    # 4. recent_pull_pct_14d — pulled batted balls / total batted balls.
    # Uses hc_x (Statcast spray-direction proxy, center ~125) and `stand`.
    if "hc_x" in pa.columns and "stand" in pa.columns and n_bb > 0:
        bb_rows = pa[bb_mask]
        pulled = 0
        for _, row in bb_rows.iterrows():
            hc_x = row.get("hc_x")
            stand = row.get("stand", "R")
            if hc_x is None or (isinstance(hc_x, float) and hc_x != hc_x):
                continue
            if stand == "R" and hc_x < 125:
                pulled += 1
            elif stand == "L" and hc_x > 125:
                pulled += 1
        out["recent_pull_pct_14d"] = round(pulled / n_bb * 100.0, 2)

    # 5. days_since_last_hr — calendar days between this HR and the previous one
    if prev_hr_date:
        try:
            d1 = datetime.strptime(hr_date, "%Y-%m-%d")
            d0 = datetime.strptime(prev_hr_date, "%Y-%m-%d")
            out["days_since_last_hr"] = max(0, (d1 - d0).days)
        except ValueError:
            pass

    # 6. days_since_off — last_off_date is supplied by the caller; if it
    # can't be computed (e.g., season opener) leave as None.
    if last_off_date:
        try:
            d1 = datetime.strptime(hr_date, "%Y-%m-%d")
            d0 = datetime.strptime(last_off_date, "%Y-%m-%d")
            out["days_since_off"] = max(0, (d1 - d0).days)
        except ValueError:
            pass

    # 7. recent_avg_30g — for the pre-HR snapshot we proxy this from the
    # window AB sample (true 30g requires gamelog access; the window
    # AVG is a reasonable approximation for the state-of-play centroid).
    ab_mask = pa["events"].isin(_PA_AB_EVENTS)
    n_ab = int(ab_mask.sum())
    if n_ab >= 5:
        n_hits = int(pa["events"].isin(_HIT_EVENTS).sum())
        out["recent_avg_30g"] = round(n_hits / n_ab, 3)

    return out


def _aggregate_centroid(snapshots: list[dict]) -> list[float | None]:
    """Mean-aggregate a list of state-of-play snapshots into a centroid.

    Returns a positional list (one entry per FORM_ARCHETYPE_FEATURES slot).
    Missing values (None) are skipped per slot — if no snapshot has a value
    for that feature, the centroid slot is None.
    """
    if not snapshots:
        return [None] * len(FORM_ARCHETYPE_FEATURES)

    centroid: list[float | None] = []
    for feat in FORM_ARCHETYPE_FEATURES:
        vals = [s.get(feat) for s in snapshots if s.get(feat) is not None]
        centroid.append(round(sum(vals) / len(vals), 4) if vals else None)
    return centroid


def compute_batter_form_archetype(
    player_ids: list[int],
    as_of_date: str | None = None,
    window_days: int = FORM_ARCHETYPE_DEFAULT_WINDOW,
) -> dict[int, dict | None]:
    """Build per-batter pre-HR state-of-play centroids.

    For each batter in *player_ids*:
      1. Pull every HR they hit in the prior FORM_ARCHETYPE_LOOKBACK_SEASONS
         seasons, filtered to game_date < as_of_date (honest as-of-date).
      2. For each HR, snapshot the state-of-play in the *window_days* days
         BEFORE the HR. Bulk Statcast pull, then per-batter slice.
      3. Mean-aggregate the snapshots into a 7-element centroid.
      4. If fewer than FORM_ARCHETYPE_MIN_HRS HRs feed the centroid, return
         None for that batter — caller skips via None propagation.

    Returns {player_id: {feature_centroid, n_hrs_used} | None}.

    *as_of_date* — YYYY-MM-DD. None (default) = today = production behavior.
    *window_days* — pre-HR snapshot window. 7 by default (sweep dimension
                    for the Phase 3 backtest harness).

    Phase 1: this builder is callable but uncalled.
    # TODO Phase 2: wire into etl/etl_nightly.py + etl/backfill_form_archetype.py.

    NOTE: each HR triggers one bulk Statcast pull for the prior
    *window_days*. For large batter lists, this can be slow — Phase 2 will
    de-duplicate by date (one bulk pull per unique window-end-date) and
    cache the per-batter slices. Phase 1 ships the simpler per-HR pull so
    the math is verifiable on a small slate first.
    """
    end_str = as_of_date or datetime.now().strftime("%Y-%m-%d")
    try:
        end_dt = datetime.strptime(end_str, "%Y-%m-%d")
    except ValueError:
        return {pid: None for pid in player_ids}

    season = end_dt.year
    season_lo = season - FORM_ARCHETYPE_LOOKBACK_SEASONS + 1

    out: dict[int, dict | None] = {}
    if not player_ids:
        return out

    # Step 1: pull all relevant HR events from the local DB.
    # Mirrors pitcher_profile._build_victim_profiles_from_db's pattern —
    # DB-first, no per-player Statcast roundtrips for the HR list.
    try:
        from etl.db import DB_PATH
        import sqlite3
        if not DB_PATH.exists():
            return {pid: None for pid in player_ids}
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        # parameterized IN clause — sqlite3 doesn't accept tuples for IN, so
        # build the placeholder string manually.
        placeholders = ",".join("?" * len(player_ids))
        rows = conn.execute(
            f"""
            SELECT batter_id, game_date
            FROM batter_hr_events
            WHERE batter_id IN ({placeholders})
              AND game_date >= ?
              AND game_date < ?
            ORDER BY batter_id, game_date
            """,
            (*player_ids, f"{season_lo}-03-01", end_str),
        ).fetchall()
        conn.close()
    except Exception as e:
        print(f"  [features_v2] form_archetype HR-events DB load failed: {e}")
        return {pid: None for pid in player_ids}

    hrs_by_batter: dict[int, list[str]] = {}
    for r in rows:
        bid = int(r["batter_id"])
        date = str(r["game_date"])
        hrs_by_batter.setdefault(bid, []).append(date)

    # Step 2 + 3 + 4: per batter, build per-HR snapshots and aggregate.
    for pid in player_ids:
        hr_dates = hrs_by_batter.get(int(pid), [])
        if len(hr_dates) < FORM_ARCHETYPE_MIN_HRS:
            out[pid] = None
            continue

        snapshots = []
        prev_hr: str | None = None
        for hr_date in hr_dates:
            try:
                hr_dt = datetime.strptime(hr_date, "%Y-%m-%d")
            except ValueError:
                prev_hr = hr_date
                continue
            window_start = (hr_dt - timedelta(days=window_days)).strftime("%Y-%m-%d")
            window_end = (hr_dt - timedelta(days=1)).strftime("%Y-%m-%d")
            # Bulk pitch-level pull for one batter's pre-HR window.
            try:
                from pybaseball import statcast_batter
                df_window = statcast_batter(window_start, window_end, int(pid))
            except Exception:
                prev_hr = hr_date
                continue

            snap = _per_hr_state_snapshot(
                df_window=df_window,
                hr_date=hr_date,
                prev_hr_date=prev_hr,
                last_off_date=None,  # Phase 2 fills this from gamelog
            )
            if snap is not None:
                snapshots.append(snap)
            prev_hr = hr_date

        if len(snapshots) < FORM_ARCHETYPE_MIN_HRS:
            # Not enough usable snapshots — None+skip per design.
            out[pid] = None
            continue

        centroid = _aggregate_centroid(snapshots)
        out[pid] = {
            "feature_centroid": centroid,
            "n_hrs_used": len(snapshots),
        }

    return out


# Phase 2 default — at scoring time, attach the 14d centroid to the batter
# dict. The diagnostics/backtest_form_archetype.py harness sweeps all 3
# windows independently from the persisted JSON, so the production default
# only needs ONE value; we pick 14d as the mid-point of the 7/14/21 grid
# (sweet spot per design doc rationale). Phase 3 may flip this to the
# winning sweep variant.
FORM_ARCHETYPE_PRODUCTION_WINDOW = 14


def fetch_form_archetype_centroids_bulk(
    player_ids: list[int],
    as_of_date: str,
    window_days: int = FORM_ARCHETYPE_PRODUCTION_WINDOW,
    db_path: str | None = None,
) -> dict[int, dict]:
    """Bulk-load persisted centroids from batter_form_archetype.

    Returns {player_id: {feature_centroid (list), n_hrs_used (int), window_days,
                         feature_centroid_json (str)}} — only for players who
    have a row at (date_through=as_of_date - 1 day, window_days).

    Players without a centroid row are simply absent from the returned dict
    — caller treats missing as None+skip. No league-avg fallback by design
    (matches docs/form_archetype_design.md).

    Phase 2: read-only lookup. Centroids are persisted by
    etl/backfill_form_archetype.py (and Phase 3+ nightly hook). With
    USE_FORM_ARCHETYPE=False (Phase 2 default), the persisted centroid is
    attached to pick_inputs for replay but does NOT enter the live score.
    """
    if not player_ids:
        return {}

    try:
        from etl.db import DB_PATH as _DB
        path = db_path or str(_DB)
        if not Path(path).exists():
            return {}
    except Exception:
        return {}

    # date_through = day before as_of_date (centroid built from HRs
    # strictly before scoring day; matches the design doc convention).
    try:
        end_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
    except ValueError:
        return {}
    date_through = (end_dt - timedelta(days=1)).strftime("%Y-%m-%d")

    placeholders = ",".join("?" * len(player_ids))
    try:
        import sqlite3
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT player_id, feature_centroid_json, n_hrs_used, window_days
            FROM batter_form_archetype
            WHERE player_id IN ({placeholders})
              AND date_through = ?
              AND window_days = ?
            """,
            (*player_ids, date_through, int(window_days)),
        ).fetchall()
        conn.close()
    except Exception as e:
        print(f"  [features_v2] form_archetype centroid bulk load failed: {e}")
        return {}

    out: dict[int, dict] = {}
    for r in rows:
        pid = int(r["player_id"])
        try:
            centroid = json.loads(r["feature_centroid_json"])
        except (TypeError, ValueError):
            continue
        out[pid] = {
            "feature_centroid": centroid,
            "feature_centroid_json": r["feature_centroid_json"],
            "n_hrs_used": int(r["n_hrs_used"] or 0),
            "window_days": int(r["window_days"]),
        }
    return out


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Inspect features_v2 fetchers")
    parser.add_argument("--batter", type=int)
    parser.add_argument("--pitcher", type=int)
    parser.add_argument("--vegas", action="store_true")
    parser.add_argument("--bulk", action="store_true", help="Test bulk fetchers")
    parser.add_argument("--recent", action="store_true", help="Test recent 14d Statcast bulk fetch")
    parser.add_argument("--as-of-date", default=None, help="YYYY-MM-DD for --recent (default: today)")
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
    if args.recent:
        r = fetch_batter_recent_statcast_14d(as_of_date=args.as_of_date)
        print(f"recent 14d Statcast (as_of={args.as_of_date or 'today'}): {len(r)} batters")
        # Show a few sample rows so the smoke test of this is visible
        for pid, vals in list(r.items())[:5]:
            print(f"  {pid}: {vals}")
