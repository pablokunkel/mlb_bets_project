"""
Wind-direction utilities for diagnostics.

The scoring engine (score_batters.score_wind) already uses wind direction
relative to park orientation AND batter handedness for per-batter scoring.
These helpers compute a simpler, hand-agnostic derived metric — the
projection of the wind vector onto the home-plate-to-CF axis — for use
in dashboard diagnostics.

Positive helping_factor = wind blowing OUT toward CF (HR-helping).
Negative helping_factor = wind blowing IN toward home (HR-suppressing).

Use score_wind for actual scoring. Use these for "is wind a tailwind today?"
at-a-glance diagnostics and for HR-rate-by-wind-band aggregations.
"""

import math

# Reuse the bearing lookup the scoring engine already uses.
# Imported lazily inside functions to avoid a hard dependency at import time
# (some callers — e.g., a freshly cloned repo without numpy — should still
# be able to import wind_utils for unit tests).


def wind_to_bearing(wind_dir_from_deg: float) -> float:
    """
    Convert meteorological "wind FROM" direction to "wind TO" direction
    (where the wind is blowing toward). Adds 180 mod 360.

    Weather feeds report direction as "FROM" by convention (e.g.,
    wind_dir=180 means wind from the south, blowing north).
    """
    return (wind_dir_from_deg + 180.0) % 360.0


def angular_diff_deg(a: float, b: float) -> float:
    """
    Smallest signed angle from a to b (degrees), result in [-180, 180].
    """
    d = (b - a) % 360.0
    return d if d <= 180.0 else d - 360.0


def wind_helping_factor(
    wind_mph,
    wind_dir_from_deg,
    venue: str,
):
    """
    Project the wind vector onto the home-plate-to-CF axis.

    Returns a float in MPH:
      - Positive: wind blowing OUT toward CF — HR-helping
      - Negative: wind blowing IN toward home — HR-suppressing
      - Zero/near-zero: pure crosswind (perpendicular to CF axis) or no wind

    Returns None if any input is missing or the venue isn't in the
    bearing lookup. Caller should filter out dome games upstream — this
    function does not gate on dome status.

    Worked example: wind_dir_from=180 (from south), wind_mph=15, at a park
    with cf_bearing=0 (CF due north).
      wind_to = (180 + 180) % 360 = 0 (wind moving north toward CF)
      angle = angular_diff_deg(0, 0) = 0
      cos(0) = 1
      helping = 15 × 1 = +15 MPH (full tailwind toward CF)
    """
    if wind_mph is None or wind_dir_from_deg is None:
        return None
    try:
        wind_mph = float(wind_mph)
        wind_dir_from_deg = float(wind_dir_from_deg)
    except (TypeError, ValueError):
        return None

    if wind_mph < 1.0:
        # Below noise floor — call it neutral. Avoids amplifying sensor jitter.
        return 0.0

    from score_batters import PARK_CF_BEARING  # lazy import (see module docstring)
    cf_bearing = PARK_CF_BEARING.get(venue)
    if cf_bearing is None:
        return None

    wind_to = wind_to_bearing(wind_dir_from_deg)
    angle = angular_diff_deg(wind_to, cf_bearing)
    return wind_mph * math.cos(math.radians(angle))


def helping_band(helping) -> str:
    """
    Coarse label for a helping_factor value. Bands are tuned for at-a-glance
    diagnostics rather than precise physics — 5 MPH was chosen as the
    threshold where the alignment-driven term starts dominating ambient
    weather variation in our sample.

    Returns one of: in_strong, in_mild, neutral, out_mild, out_strong, unknown.
    """
    if helping is None:
        return "unknown"
    if helping >= 5:
        return "out_strong"
    if helping >= 1:
        return "out_mild"
    if helping > -1:
        return "neutral"
    if helping > -5:
        return "in_mild"
    return "in_strong"


HELPING_BAND_ORDER = ["in_strong", "in_mild", "neutral", "out_mild", "out_strong"]
HELPING_BAND_LABELS = {
    "in_strong":  "Wind IN (5+ MPH)",
    "in_mild":    "Wind IN (1-5 MPH)",
    "neutral":    "Crosswind / calm",
    "out_mild":   "Wind OUT (1-5 MPH)",
    "out_strong": "Wind OUT (5+ MPH)",
}
