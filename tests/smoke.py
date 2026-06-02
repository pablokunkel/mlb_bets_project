#!/usr/bin/env python3
"""
tests/smoke.py — Smoke tests + DB sanity probes for the MLB HR Bets pipeline.

Two layers:

1. **Pin tests** (function-level, no DB).
   Lock down the scoring functions' outputs for known inputs so weight
   refits and curve tweaks can't silently change the math without us
   noticing. Fast (<1s); runs in any environment.

2. **DB sanity probes** (run against the real SQLite DB).
   Catch the bug classes flagged by the 2026-05-02 audit before they
   poison `pick_inputs` / `daily_picks`. Skipped automatically if the
   DB isn't present (e.g., CI or a fresh checkout).

Severity tiers:

- **HALT** — pipeline-blocking. Failure means `run_daily.bat` should NOT
  ship picks for today. Today's stale picks stay on the dashboard.
- **WARN** — anomaly worth flagging but not blocking. Surfaces in logs;
  optionally bannered on the dashboard.
- **INFO** — diagnostic; logged for the daily pulse.

Usage:
    python -m tests.smoke                    # run all
    python -m tests.smoke --pin-only         # skip DB checks
    python -m tests.smoke --db-only          # skip pin tests
    python -m tests.smoke --strict           # WARN exits non-zero too

Exit codes:
    0  all PASS
    1  WARN(s) only (when --strict, or always)
    2  any HALT failed
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Callable, Optional

# Make project root importable regardless of where this is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ANSI colors. No-op if the terminal doesn't support them.
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


class Result:
    """One smoke check's outcome."""
    HALT = "HALT"
    WARN = "WARN"
    INFO = "INFO"
    PASS = "PASS"

    def __init__(self, name: str, status: str, detail: str = ""):
        self.name = name
        self.status = status
        self.detail = detail

    def __repr__(self) -> str:
        color = {
            Result.HALT: RED,
            Result.WARN: YELLOW,
            Result.PASS: GREEN,
            Result.INFO: DIM,
        }.get(self.status, "")
        # ASCII markers for Windows cp1252 consoles. The status word
        # already conveys outcome; the marker is just a quick eye-grep.
        marker = {
            Result.HALT: "X",
            Result.WARN: "!",
            Result.PASS: "+",
            Result.INFO: ".",
        }.get(self.status, "?")
        return (
            f"  {color}{marker} {self.status:<5}{RESET}  "
            f"{self.name:<55}  {DIM}{self.detail}{RESET}"
        )


# ---------------------------------------------------------------------------
# Pin tests — lock down scoring function outputs
# ---------------------------------------------------------------------------

def pin_score_power_empty() -> Result:
    """score_power with no inputs returns the neutral default."""
    from score_batters import score_power
    val = score_power({})
    if val == 50.0:
        return Result("score_power({}) -> 50.0 (neutral, all skipped)", Result.PASS)
    return Result(
        "score_power({}) -> 50.0",
        Result.HALT,
        f"got {val}; expected 50.0 (skip-on-missing)",
    )


def pin_score_power_all_zero() -> Result:
    """A zero-everywhere batter is treated as no-data, not punished."""
    from score_batters import score_power
    val = score_power({
        "barrel_pct": 0, "exit_velo": 0, "hr_fb_pct": 0, "iso": 0,
    })
    # All four `> 0` guards skip; result equals empty case.
    if val == 50.0:
        return Result(
            "score_power(all-zero) -> 50.0 (regression guard)", Result.PASS
        )
    return Result(
        "score_power(all-zero) -> 50.0 (regression guard)",
        Result.HALT,
        f"got {val}; the Buxton-class bug is back — zeros are being scored",
    )


def pin_score_power_elite() -> Result:
    """A Buxton-class profile must score above 70."""
    from score_batters import score_power
    val = score_power({
        "barrel_pct": 18.0, "exit_velo": 92, "hr_fb_pct": 25, "iso": 0.260,
        "xwoba_contact": 0.450,
    })
    if val >= 70:
        return Result(
            f"score_power(elite) -> {val:.1f} (>=70)", Result.PASS
        )
    return Result(
        "score_power(elite) >= 70",
        Result.HALT,
        f"got {val}; elite power inputs aren't producing elite scores",
    )


def _check_anchor_score(input_key: str, value: float, lo: float, hi: float, expected: float, tol: float = 0.5) -> str | None:
    """Helper: score a single power input via score_power and check against expected.

    Returns failure string or None on pass.
    """
    from score_batters import score_power
    got = score_power({input_key: value})
    # Per score_power's mean-of-populated-inputs formula, with one input we
    # expect: score = min_max_scale(value, lo, hi).
    want = max(0.0, min(100.0, (value - lo) / (hi - lo) * 100))
    if abs(got - want) > tol:
        return f"{input_key}={value} expected ~{want:.1f} (anchor {lo}-{hi}), got {got:.1f}"
    if abs(got - expected) > tol:
        return f"{input_key}={value} expected ~{expected:.1f}, got {got:.1f}"
    return None


def pin_score_power_barrel_anchors() -> Result:
    """B17 (2026-05-27): score_power barrel_pct anchor is (3, 11), p10/p90 of 2025 BF."""
    from score_batters import score_power
    # Anchor low: 3 → score 0.  Anchor high: 11 → score 100.  Empirical p50 6.6 → ~45.
    cases = [
        ("barrel_pct", 3.0,  0.0),    # anchor low
        ("barrel_pct", 11.0, 100.0),  # anchor high
        ("barrel_pct", 6.6,  45.0),   # 2025 BF empirical p50 → ~mid-band
        ("barrel_pct", 2.0,  0.0),    # below low → clamp
        ("barrel_pct", 13.0, 100.0),  # above high → clamp
    ]
    failures = []
    for k, v, exp in cases:
        err = _check_anchor_score(k, v, 3.0, 11.0, exp)
        if err:
            failures.append(err)
    if failures:
        return Result(
            "score_power(barrel_pct) anchor (3, 11)",
            Result.HALT,
            "; ".join(failures),
        )
    return Result(
        "score_power(barrel_pct) anchor (3, 11) — B17 recal",
        Result.PASS,
    )


def pin_score_power_hr_fb_anchors() -> Result:
    """B17 (2026-05-27): score_power hr_fb_pct anchor is (3, 10), p10/p90 of 2025 BF."""
    cases = [
        ("hr_fb_pct", 3.0,  0.0),
        ("hr_fb_pct", 10.0, 100.0),
        ("hr_fb_pct", 6.0,  42.857),  # 2025 BF empirical p50 → (6-3)/(10-3)*100 ≈ 42.86
        ("hr_fb_pct", 2.0,  0.0),
        ("hr_fb_pct", 12.0, 100.0),
    ]
    failures = []
    for k, v, exp in cases:
        err = _check_anchor_score(k, v, 3.0, 10.0, exp)
        if err:
            failures.append(err)
    if failures:
        return Result(
            "score_power(hr_fb_pct) anchor (3, 10)",
            Result.HALT,
            "; ".join(failures),
        )
    return Result(
        "score_power(hr_fb_pct) anchor (3, 10) — B17 recal",
        Result.PASS,
    )


def pin_score_power_iso_anchors() -> Result:
    """B17 (2026-05-27): score_power iso anchor is (0.100, 0.250), p10/p90 of 2025 BF."""
    cases = [
        ("iso", 0.100, 0.0),
        ("iso", 0.250, 100.0),
        ("iso", 0.167, 44.667),  # 2025 BF empirical p50 → (0.167-0.100)/(0.150)*100 ≈ 44.67
        ("iso", 0.075, 0.0),
        ("iso", 0.300, 100.0),
    ]
    failures = []
    for k, v, exp in cases:
        err = _check_anchor_score(k, v, 0.100, 0.250, exp)
        if err:
            failures.append(err)
    if failures:
        return Result(
            "score_power(iso) anchor (0.100, 0.250)",
            Result.HALT,
            "; ".join(failures),
        )
    return Result(
        "score_power(iso) anchor (0.100, 0.250) — B17 recal",
        Result.PASS,
    )


def pin_score_power_xwoba_anchors() -> Result:
    """B17 (2026-05-27): score_power xwoba_contact anchor is (0.260, 0.390), p10/p90 of 2026 live."""
    cases = [
        ("xwoba_contact", 0.260, 0.0),
        ("xwoba_contact", 0.390, 100.0),
        ("xwoba_contact", 0.316, 43.077),  # 2026 live empirical p50 → ~43
        ("xwoba_contact", 0.200, 0.0),
        ("xwoba_contact", 0.420, 100.0),
    ]
    failures = []
    for k, v, exp in cases:
        err = _check_anchor_score(k, v, 0.260, 0.390, exp)
        if err:
            failures.append(err)
    if failures:
        return Result(
            "score_power(xwoba_contact) anchor (0.260, 0.390)",
            Result.HALT,
            "; ".join(failures),
        )
    return Result(
        "score_power(xwoba_contact) anchor (0.260, 0.390) — B17 recal",
        Result.PASS,
    )


def pin_score_power_recent_xwoba_anchors() -> Result:
    """B17 (2026-05-27): score_power recent_xwoba_contact_14d anchor is (0.225, 0.410)
    on the B6a-gated path. 14d window is wider than season live xwoba_contact, so this
    anchor deviates from live xwoba_contact's (0.260, 0.390).
    """
    import score_batters as sb
    prev = sb.USE_RECENT_STATCAST_BLEND
    sb.USE_RECENT_STATCAST_BLEND = True
    try:
        # Anchor low: 0.225 → score 0.  Anchor high: 0.410 → score 100.
        # Empirical p50 0.314 → (0.314-0.225)/(0.185)*100 ≈ 48.1.
        cases = [
            ("recent_xwoba_contact_14d", 0.225, 0.0),
            ("recent_xwoba_contact_14d", 0.410, 100.0),
            ("recent_xwoba_contact_14d", 0.314, 48.108),
            ("recent_xwoba_contact_14d", 0.150, 0.0),
            ("recent_xwoba_contact_14d", 0.450, 100.0),
        ]
        failures = []
        for k, v, exp in cases:
            err = _check_anchor_score(k, v, 0.225, 0.410, exp)
            if err:
                failures.append(err)
    finally:
        sb.USE_RECENT_STATCAST_BLEND = prev
    if failures:
        return Result(
            "score_power(recent_xwoba_contact_14d) anchor (0.225, 0.410)",
            Result.HALT,
            "; ".join(failures),
        )
    return Result(
        "score_power(recent_xwoba_contact_14d) anchor (0.225, 0.410) — B17 recal",
        Result.PASS,
    )


def pin_score_lineup_position_table() -> Result:
    """Lineup-position scores honor the documented table."""
    from score_batters import score_lineup_position
    cases = [
        (None, 35.0),
        (3, 78.0),       # #3 hitter
        ("bench", 15.0),
        ("roster_only", 15.0),
        (10, 35.0),      # out-of-range fallthrough
    ]
    failures = []
    for inp, want in cases:
        got = score_lineup_position(inp)
        if got != want:
            failures.append(f"{inp!r} -> {got} (want {want})")
    if not failures:
        return Result(
            "score_lineup_position(table) honored", Result.PASS
        )
    return Result(
        "score_lineup_position(table)",
        Result.HALT,
        "; ".join(failures),
    )


def pin_score_matchup_no_data() -> Result:
    """Matchup score with no batter/pitcher data should fall to neutral, not the woba-fallback bug value."""
    from score_batters import score_matchup
    val = score_matchup({}, {"throws": "R"})
    # Pre-fix: 0.320 league-mean fill scored to ~28 with platoon=0 → unwell.
    # Post-fix: scores list is empty → returns 50.0 neutral.
    if 45 <= val <= 60:
        return Result(
            f"score_matchup(empty) -> {val:.1f} (neutral, no woba bug)", Result.PASS
        )
    return Result(
        "score_matchup(empty) ≈ 50",
        Result.HALT,
        f"got {val}; the woba=0.320 fallback bug may have regressed",
    )


def pin_platoon_dampener_table() -> Result:
    """Platoon dampener honors the documented [floor, 1.0] curve."""
    from score_batters import _platoon_dampener, PLATOON_DAMPENER_FLOOR
    cases = [
        # (games, max_games, expected_multiplier)
        (None, 33, 1.0),                          # no info → no dampening
        (33, None, 1.0),                          # no slate ctx → no-op
        (33, 0, 1.0),                             # zero max → no-op
        (33, 33, 1.0),                            # daily starter
        (40, 33, 1.0),                            # over-max (DH'd a doubleheader) → no boost
        (0, 33, PLATOON_DAMPENER_FLOOR),          # never played → floor
        # Linear interp: 50% → midway between floor and 1.0
        (16, 32, PLATOON_DAMPENER_FLOOR + (1.0 - PLATOON_DAMPENER_FLOOR) * 0.5),
    ]
    failures = []
    for games, max_g, expected in cases:
        got = _platoon_dampener(games, max_g)
        if abs(got - expected) > 0.001:
            failures.append(f"({games}, {max_g}) -> {got:.3f} (want {expected:.3f})")
    if not failures:
        return Result(
            f"_platoon_dampener(table) honored (floor={PLATOON_DAMPENER_FLOOR})",
            Result.PASS,
        )
    return Result(
        "_platoon_dampener(table)",
        Result.HALT,
        "; ".join(failures),
    )


def pin_score_matchup_rookie_bonus() -> Result:
    """Rookie pitcher (`is_rookie=True`) adds a +15 matchup bonus."""
    from score_batters import score_matchup, ROOKIE_MATCHUP_BONUS
    veteran = score_matchup(
        {"woba_vs_hand": 0.330},
        {"throws": "R", "hr_per_9": 1.2, "hard_hit_pct_allowed": 35},
    )
    rookie = score_matchup(
        {"woba_vs_hand": 0.330},
        {"throws": "R", "hr_per_9": 1.2, "hard_hit_pct_allowed": 35, "is_rookie": True},
    )
    delta = rookie - veteran
    if abs(delta - ROOKIE_MATCHUP_BONUS) < 0.5:
        return Result(
            f"score_matchup rookie bonus = +{delta:.1f} (~{ROOKIE_MATCHUP_BONUS})",
            Result.PASS,
        )
    return Result(
        "score_matchup rookie bonus",
        Result.HALT,
        f"got delta={delta:.1f}, expected ~{ROOKIE_MATCHUP_BONUS}",
    )


def pin_compute_slate_context_empty() -> Result:
    """compute_slate_context with empty inputs returns a clean dict, not a crash."""
    from score_batters import compute_slate_context
    ctx = compute_slate_context([], {}, {}, None, {})
    expected_keys = {"active", "park_pct", "pitcher_pct", "team_total_pct", "weather_pct"}
    if set(ctx.keys()) >= expected_keys:
        return Result(
            f"compute_slate_context(empty) keys ok ({len(ctx)})", Result.PASS
        )
    return Result(
        "compute_slate_context(empty)",
        Result.HALT,
        f"missing keys: {expected_keys - set(ctx.keys())}",
    )


def pin_compute_slate_context_skip_missing_pitcher() -> Result:
    """A pitcher with all-None stats must be SKIPPED from pitcher_pct, not filled with league mean."""
    from score_batters import compute_slate_context
    ctx = compute_slate_context([], {}, {"NoDataPitcher": {}}, None, {})
    if not ctx["pitcher_pct"]:
        return Result(
            "compute_slate_context skips no-data pitcher", Result.PASS
        )
    return Result(
        "compute_slate_context skips no-data pitcher",
        Result.HALT,
        "pitcher with no measured stats is being included with league-mean defaults — audit HIGH #3 regressed",
    )


def pin_compute_slate_context_two_signal_pitcher() -> Result:
    """A pitcher with 2+ measured signals must be INCLUDED in pitcher_pct."""
    from score_batters import compute_slate_context
    ctx = compute_slate_context(
        [], {}, {"P": {"hr_per_9": 1.5, "era": 3.5}}, None, {}
    )
    if "P" in ctx["pitcher_pct"]:
        return Result(
            "compute_slate_context includes 2-signal pitcher", Result.PASS
        )
    return Result(
        "compute_slate_context includes 2-signal pitcher",
        Result.HALT,
        "skip threshold may be too aggressive — 2 signals should qualify",
    )


def pin_shrink_to_career_basic() -> Result:
    """Bayesian shrinkage formula: (n*current + k*career) / (n + k)."""
    from score_batters import shrink_to_career
    # Ozuna case: 0.027 HR/PA in 112 PA, 0.044 career, k=200
    expected = (112 * 0.027 + 200 * 0.044) / (112 + 200)
    got = shrink_to_career(0.027, 112, 0.044, k=200)
    if abs(got - expected) < 1e-6:
        return Result(
            f"shrink_to_career(Ozuna case) -> {got:.4f}", Result.PASS
        )
    return Result(
        "shrink_to_career(Ozuna case)",
        Result.HALT,
        f"got {got:.6f}; expected {expected:.6f}",
    )


def pin_shrink_to_career_no_career_pass_through() -> Result:
    """When career value is missing, return current unchanged."""
    from score_batters import shrink_to_career
    val = shrink_to_career(0.10, 100, None)
    if val == 0.10:
        return Result("shrink_to_career(no career) passes through", Result.PASS)
    return Result(
        "shrink_to_career(no career) passes through",
        Result.HALT,
        f"got {val}; expected 0.10",
    )


def pin_shrink_to_career_huge_sample_barely_shrinks() -> Result:
    """With current_n >> k, the prior's influence should be small."""
    from score_batters import shrink_to_career
    # n=5000, k=200: prior weight ~3.8% → minimal shrinkage
    val = shrink_to_career(0.10, 5000, 0.05, k=200)
    # Expected ≈ 0.0981 — current dominates
    if 0.097 < val < 0.099:
        return Result(
            f"shrink_to_career(big sample) -> {val:.4f} (~0.098)", Result.PASS
        )
    return Result(
        "shrink_to_career(big sample)",
        Result.HALT,
        f"got {val:.4f}; expected ~0.098 (current dominates)",
    )


def pin_use_career_prior_default_off() -> Result:
    """Production must default to no shrinkage until backtest validates."""
    from score_batters import USE_CAREER_PRIOR
    if USE_CAREER_PRIOR is False:
        return Result(
            "USE_CAREER_PRIOR default = False (production-safe)", Result.PASS
        )
    return Result(
        "USE_CAREER_PRIOR default = False",
        Result.HALT,
        f"got {USE_CAREER_PRIOR}; flipping the flag without backtest "
        "could degrade picks. Default must be False.",
    )


def pin_compute_season_hr_floor_tiers() -> Result:
    """Floor tier table: 5/8/12/18/25 HR thresholds → 50/60/70/78/85 floors."""
    from score_batters import compute_season_hr_floor
    cases = [
        (None,  0.0),
        (0,     0.0),
        (3,     0.0),
        (5,    50.0),
        (8,    60.0),
        (12,   70.0),
        (18,   78.0),
        (25,   85.0),
        (40,   85.0),  # cap at top tier
    ]
    failures = []
    for hr, want in cases:
        got = compute_season_hr_floor(hr)
        if got != want:
            failures.append(f"hr={hr}: got {got}, want {want}")
    if not failures:
        return Result("compute_season_hr_floor(tiers) honored", Result.PASS)
    return Result(
        "compute_season_hr_floor(tiers)", Result.HALT,
        "; ".join(failures),
    )


def pin_use_recent_statcast_blend_default_off() -> Result:
    """USE_RECENT_STATCAST_BLEND must stay off until backtest validates the blend."""
    from score_batters import USE_RECENT_STATCAST_BLEND
    if USE_RECENT_STATCAST_BLEND is False:
        return Result(
            "USE_RECENT_STATCAST_BLEND default = False (pre-backtest)", Result.PASS
        )
    return Result(
        "USE_RECENT_STATCAST_BLEND default = False",
        Result.HALT,
        f"got {USE_RECENT_STATCAST_BLEND}; flipping the recent-Statcast blend on "
        "requires a documented refit + backtest decision in WEIGHT_REFIT_LOG.md.",
    )


def pin_score_power_recent_blend_flag_off_no_op() -> Result:
    """With flag off, recent_* inputs on the batter dict are IGNORED by score_power."""
    import score_batters as sb
    # Same season-only inputs in both cases; only the recent_* fields differ.
    base_inputs = {
        "barrel_pct": 10.0, "exit_velo": 90.0, "hr_fb_pct": 14.0, "iso": 0.200,
    }
    season_only = sb.score_power(base_inputs)
    with_recents = sb.score_power({
        **base_inputs,
        "recent_barrel_real_14d": 20.0,        # extreme value
        "recent_xwoba_contact_14d": 0.500,
        "recent_iso_14d": 0.400,
    })
    if abs(with_recents - season_only) < 0.01:
        return Result(
            f"score_power(recents) ignored when flag off ({season_only:.1f})",
            Result.PASS,
        )
    return Result(
        "score_power flag-off no-op",
        Result.HALT,
        f"season_only={season_only}, with_recents={with_recents}; the recent_* "
        "inputs leaked into the score with USE_RECENT_STATCAST_BLEND=False",
    )


def pin_score_power_recent_blend_flag_on_lifts_score() -> Result:
    """With flag on, elite recent_* inputs lift the score above season-only."""
    import score_batters as sb
    prev = sb.USE_RECENT_STATCAST_BLEND
    sb.USE_RECENT_STATCAST_BLEND = True
    try:
        # Bohm-class scenario: weakish season inputs, elite recent values.
        base_inputs = {
            "barrel_pct": 7.0, "exit_velo": 87.0, "hr_fb_pct": 9.0, "iso": 0.140,
        }
        season_only_score = sb.score_power(base_inputs)
        # Mutex flag toggle inside the function so we re-run with same off behavior
        sb.USE_RECENT_STATCAST_BLEND = False
        cold_baseline = sb.score_power(base_inputs)
        sb.USE_RECENT_STATCAST_BLEND = True
        with_hot_recent = sb.score_power({
            **base_inputs,
            "recent_barrel_real_14d": 17.0,           # elite (anchor 8-18)
            "recent_xwoba_contact_14d": 0.450,        # elite (anchor .330-.450)
            "recent_iso_14d": 0.290,                  # near elite (anchor .100-.300)
        })
    finally:
        sb.USE_RECENT_STATCAST_BLEND = prev
    # Sanity: season_only with flag on should still match flag off when recents are absent
    if abs(season_only_score - cold_baseline) > 0.01:
        return Result(
            "score_power flag-on no-recents == flag-off",
            Result.HALT,
            f"flag-on no-recents={season_only_score:.1f}, flag-off={cold_baseline:.1f}",
        )
    if with_hot_recent <= season_only_score + 5:
        return Result(
            "score_power flag-on with hot recents lifts score",
            Result.HALT,
            f"season_only={season_only_score:.1f}, with_hot_recent={with_hot_recent:.1f} "
            f"(expected lift >= +5)",
        )
    return Result(
        f"score_power(Bohm-class hot recents): {season_only_score:.1f} -> {with_hot_recent:.1f}",
        Result.PASS,
    )


def pin_compute_season_hr_floor_smooth() -> Result:
    """B6b smooth log curve: floor(hr) = 26.5 * ln(hr + 1), with 18 HR pinned at 78."""
    from score_batters import compute_season_hr_floor, SMOOTH_HR_FLOOR_C
    import math
    cases = [
        (None, 0.0),
        (0,    0.0),
        (18,   78.0),                                       # calibration anchor
        (8,    SMOOTH_HR_FLOOR_C * math.log(9)),            # ~58.2 (Burger)
        (5,    SMOOTH_HR_FLOOR_C * math.log(6)),            # ~47.5
        (25,   SMOOTH_HR_FLOOR_C * math.log(26)),           # ~86.3
        (40,   SMOOTH_HR_FLOOR_C * math.log(41)),           # ~98.4 (no cap until 100)
    ]
    failures = []
    for hr, want in cases:
        got = compute_season_hr_floor(hr, smooth=True)
        if abs(got - want) > 0.5:
            failures.append(f"hr={hr}: got {got:.2f}, want {want:.2f}")
    if not failures:
        return Result("compute_season_hr_floor(smooth) matches log curve", Result.PASS)
    return Result(
        "compute_season_hr_floor(smooth)", Result.HALT, "; ".join(failures),
    )


def pin_use_smooth_hr_floor_default_off() -> Result:
    """USE_SMOOTH_HR_FLOOR must stay off until backtest validates the curve."""
    from score_batters import USE_SMOOTH_HR_FLOOR
    if USE_SMOOTH_HR_FLOOR is False:
        return Result(
            "USE_SMOOTH_HR_FLOOR default = False (pre-backtest)", Result.PASS
        )
    return Result(
        "USE_SMOOTH_HR_FLOOR default = False",
        Result.HALT,
        f"got {USE_SMOOTH_HR_FLOOR}; flipping the smooth curve on requires "
        "a documented backtest comparison (cliff vs smooth) in WEIGHT_REFIT_LOG.md.",
    )


def pin_use_season_hr_floor_default_on() -> Result:
    """Production runs with the floor ON since 2026-05-03.

    Originally defaulted to False (PR #16 shipped as opt-in pending
    backtest). The 14d harness showed decisive wins on all 4 metrics
    (avg_rank 87.8→82.7, AUC 0.633→0.661, top10_lift 2.45→2.83,
    quint_mono 2→3); 30d was ambiguous because April hitters hadn't yet
    crossed the 5/8/12 HR thresholds. Flipped on 2026-05-03 after
    Soderstrom (4 HR going in, hit his 5th unprotected) and the
    long-running Drake Baldwin pattern confirmed the qualitative case.
    """
    from score_batters import USE_SEASON_HR_FLOOR
    if USE_SEASON_HR_FLOOR is True:
        return Result(
            "USE_SEASON_HR_FLOOR default = True (post-validation)", Result.PASS
        )
    return Result(
        "USE_SEASON_HR_FLOOR default = True",
        Result.HALT,
        f"got {USE_SEASON_HR_FLOOR}; the floor was promoted to default "
        "after 14d harness validation. Reverting requires re-running the "
        "backtest harness and documenting the regression.",
    )


def pin_score_power_floor_lifts_low_score() -> Result:
    """With flag on, an 8-HR batter with weak inputs gets lifted to 60."""
    import score_batters as sb
    prev = sb.USE_SEASON_HR_FLOOR
    sb.USE_SEASON_HR_FLOOR = True
    try:
        # Weak inputs that average well below 60 under either old or new
        # power-scale anchors (the 8-HR tier floor is the test, not the
        # specific avg). 2026-05-03 anchor re-tune dropped these from
        # ~30 to ~17, but the floor lift to 60 is unchanged.
        baldwin = {
            "barrel_pct": 6.0, "exit_velo": 88.0,
            "hr_fb_pct": 8.0, "iso": 0.180, "hr": 8,
        }
        val = sb.score_power(baldwin)
    finally:
        sb.USE_SEASON_HR_FLOOR = prev
    if val == 60.0:
        return Result(
            "score_power(8 HR + weak inputs) -> 60.0 (Drake Baldwin scenario)",
            Result.PASS,
        )
    return Result(
        "score_power(8 HR + weak inputs) -> 60.0",
        Result.HALT,
        f"got {val}; expected 60.0 (8-HR tier floor)",
    )


def pin_score_power_floor_does_not_pull_down() -> Result:
    """An already-elite power score should NOT be reduced by the floor."""
    import score_batters as sb
    prev = sb.USE_SEASON_HR_FLOOR
    sb.USE_SEASON_HR_FLOOR = True
    try:
        # Elite inputs that average well above the 60-floor for 10 HR.
        # Under the 2026-05-03 anchor re-tune these average ~94, which
        # is comfortably above the 60-floor regardless.
        elite = {
            "barrel_pct": 18.0, "exit_velo": 94.0,
            "hr_fb_pct": 22.0, "iso": 0.280, "hr": 10,
        }
        val = sb.score_power(elite)
    finally:
        sb.USE_SEASON_HR_FLOOR = prev
    if val > 70.0:  # well above the 60-floor for 10 HR
        return Result(
            f"score_power(elite + 10 HR) -> {val:.1f} (floor no-op)",
            Result.PASS,
        )
    return Result(
        "score_power(elite) > 70 with floor on",
        Result.HALT,
        f"got {val}; floor pulled down an already-elite score (BUG)",
    )


# ---------------------------------------------------------------------------
# Pitcher recency blend (added 2026-05-13)
# ---------------------------------------------------------------------------

def pin_effective_hr9_season_only_when_no_recent() -> Result:
    """Missing recency → effective HR/9 = season HR/9 unchanged."""
    from pitcher_profile import effective_hr9
    got = effective_hr9(1.89, None, None)
    if got is not None and abs(got - 1.89) < 1e-6:
        return Result("effective_hr9(no recent) -> season only", Result.PASS)
    return Result(
        "effective_hr9(no recent) -> season only",
        Result.HALT,
        f"got {got}; expected 1.89",
    )


def pin_effective_hr9_blend_when_enough_starts() -> Result:
    """recent_starts >= 2 → 0.6*recent + 0.4*season (Singer 5/12 case)."""
    from pitcher_profile import effective_hr9, RECENT_HR9_BLEND_WEIGHT
    # Singer counterfactual the morning of 2026-05-12: recent 3.07, season 1.89
    got = effective_hr9(1.89, 3.07, 3)
    expected = RECENT_HR9_BLEND_WEIGHT * 3.07 + (1 - RECENT_HR9_BLEND_WEIGHT) * 1.89
    if got is not None and abs(got - expected) < 1e-6:
        return Result(
            f"effective_hr9(Singer 5/12) -> {got:.3f} (blend ratio {RECENT_HR9_BLEND_WEIGHT})",
            Result.PASS,
        )
    return Result(
        "effective_hr9 blend",
        Result.HALT,
        f"got {got}; expected {expected:.4f}",
    )


def pin_effective_hr9_below_min_starts_falls_back() -> Result:
    """One recent start isn't enough — fall back to season HR/9."""
    from pitcher_profile import effective_hr9, RECENT_HR9_MIN_STARTS
    # Use 1 start (below min) — should ignore recent even though it's huge
    got = effective_hr9(1.89, 9.00, 1)
    if got is not None and abs(got - 1.89) < 1e-6:
        return Result(
            f"effective_hr9(<{RECENT_HR9_MIN_STARTS} starts) falls back to season",
            Result.PASS,
        )
    return Result(
        "effective_hr9 below-min-starts fallback",
        Result.HALT,
        f"got {got}; expected 1.89 (1 start should NOT trigger blend)",
    )


def pin_aggregate_recent_statcast_basic() -> Result:
    """B6a: bulk Statcast aggregator computes per-batter 14d metrics correctly.

    Synthetic input: 12 batted-ball PAs for one batter (id=10001) with
    1 barrel (launch_speed_angle=6), mean estimated xwOBA of 0.450 on
    those 12, and a known hit/AB mix that yields ISO = 0.250.
    """
    try:
        import pandas as pd
    except ImportError:
        return Result("aggregate_recent_statcast (pandas missing — skipped)", Result.INFO)
    from features_v2 import _aggregate_recent_statcast

    # 12 PAs: 4 singles, 2 doubles, 1 triple, 1 HR, 4 outs.
    # AB = 12, H = 8, TB = 4 + 4 + 3 + 4 = 15. ISO = (15 - 8) / 12 = 0.583.
    # Going to use a simpler mix that lands ISO at exactly 0.250:
    #   8 ABs: 1 single, 1 double, 1 HR, 5 outs. H=3, TB=1+2+4=7. ISO=(7-3)/8=0.500.
    # Simpler: 10 ABs: 2 singles, 1 double, 1 HR, 6 outs.
    #   H=4, TB=2+2+4=8. ISO=(8-4)/10=0.400.
    events = (
        ["single"] * 2 + ["double"] * 1 + ["home_run"] * 1
        + ["field_out"] * 6
    )
    bb_type = ["line_drive"] * 2 + ["fly_ball"] * 2 + ["ground_ball"] * 6
    # One barrel (the HR); others not barreled.
    lsa = [0, 0, 5, 6, 0, 0, 0, 0, 0, 0]
    # estimated xwoba on contact: only 6 fly/line, others ground (Statcast
    # still fills xwoba but typically lower). Use 10 values, mean=0.300.
    xwoba_speed = [0.350, 0.380, 0.500, 1.500, 0.150, 0.150, 0.150, 0.150, 0.150, 0.120]
    df = pd.DataFrame({
        "batter": [10001] * 10,
        "events": events,
        "bb_type": bb_type,
        "launch_speed_angle": lsa,
        "estimated_woba_using_speedangle": xwoba_speed,
    })
    agg = _aggregate_recent_statcast(df, min_batted_balls=10)
    if 10001 not in agg:
        return Result(
            "_aggregate_recent_statcast(synthetic)", Result.HALT,
            f"batter not in output; got keys={list(agg.keys())}",
        )
    row = agg[10001]
    failures = []
    # 1 barrel out of 10 batted balls = 10.0%
    if abs(row.get("recent_barrel_real_14d", -1) - 10.0) > 0.5:
        failures.append(f"barrel% got {row.get('recent_barrel_real_14d')}, want ~10.0")
    # ISO = (8 - 4) / 10 = 0.400
    if abs(row.get("recent_iso_14d", -1) - 0.400) > 0.005:
        failures.append(f"ISO got {row.get('recent_iso_14d')}, want ~0.400")
    # xwoba_contact = mean of 10 values
    expected_xwoba = sum(xwoba_speed) / len(xwoba_speed)
    if abs(row.get("recent_xwoba_contact_14d", -1) - expected_xwoba) > 0.005:
        failures.append(f"xwoba got {row.get('recent_xwoba_contact_14d')}, want ~{expected_xwoba:.3f}")
    if not failures:
        return Result(
            f"_aggregate_recent_statcast(synthetic) computes barrel/iso/xwoba",
            Result.PASS,
        )
    return Result(
        "_aggregate_recent_statcast(synthetic)", Result.HALT, "; ".join(failures),
    )


def pin_aggregate_recent_statcast_empty() -> Result:
    """Empty / None DataFrame returns {} without crashing."""
    from features_v2 import _aggregate_recent_statcast
    if _aggregate_recent_statcast(None) != {}:
        return Result("_aggregate_recent_statcast(None)", Result.HALT, "expected {}")
    try:
        import pandas as pd
    except ImportError:
        return Result("_aggregate_recent_statcast(None) -> {}", Result.PASS)
    if _aggregate_recent_statcast(pd.DataFrame()) != {}:
        return Result("_aggregate_recent_statcast(empty df)", Result.HALT, "expected {}")
    return Result("_aggregate_recent_statcast(empty / None) -> {}", Result.PASS)


def pin_aggregate_recent_statcast_thin_sample_dropped() -> Result:
    """Batters under min_batted_balls threshold are dropped, not reported."""
    try:
        import pandas as pd
    except ImportError:
        return Result("aggregate_recent_statcast thin-sample (pandas missing)", Result.INFO)
    from features_v2 import _aggregate_recent_statcast
    # 3 PAs for one batter; threshold is 10. Should drop.
    df = pd.DataFrame({
        "batter": [20002] * 3,
        "events": ["single", "field_out", "double"],
        "bb_type": ["line_drive", "ground_ball", "line_drive"],
        "launch_speed_angle": [0, 0, 5],
        "estimated_woba_using_speedangle": [0.4, 0.1, 0.5],
    })
    agg = _aggregate_recent_statcast(df, min_batted_balls=10)
    if not agg:
        return Result(
            "_aggregate_recent_statcast drops thin sample (<10 BB)", Result.PASS
        )
    return Result(
        "_aggregate_recent_statcast thin sample dropped",
        Result.HALT,
        f"expected empty, got {agg}",
    )


def pin_effective_era_blend() -> Result:
    """B4: effective_era follows the same blend rules as effective_hr9."""
    from pitcher_profile import effective_era, RECENT_HR9_BLEND_WEIGHT
    season, recent, starts = 3.90, 5.20, 4
    got = effective_era(season, recent, starts)
    expected = RECENT_HR9_BLEND_WEIGHT * recent + (1 - RECENT_HR9_BLEND_WEIGHT) * season
    if got is not None and abs(got - expected) < 1e-6:
        return Result(
            f"effective_era(season=3.90, recent=5.20, starts=4) -> {got:.3f}",
            Result.PASS,
        )
    return Result(
        "effective_era blend", Result.HALT,
        f"got {got}, expected {expected:.4f}",
    )


def pin_effective_k9_blend() -> Result:
    """B4: effective_k9 follows the same blend rules as effective_hr9."""
    from pitcher_profile import effective_k9, RECENT_HR9_BLEND_WEIGHT
    season, recent, starts = 10.5, 7.2, 4
    got = effective_k9(season, recent, starts)
    expected = RECENT_HR9_BLEND_WEIGHT * recent + (1 - RECENT_HR9_BLEND_WEIGHT) * season
    if got is not None and abs(got - expected) < 1e-6:
        return Result(
            f"effective_k9(season=10.5, recent=7.2, starts=4) -> {got:.3f}",
            Result.PASS,
        )
    return Result(
        "effective_k9 blend", Result.HALT,
        f"got {got}, expected {expected:.4f}",
    )


def pin_effective_blends_custom_weight() -> Result:
    """B4: blend_weight / min_starts kwargs override defaults — for the harness."""
    from pitcher_profile import effective_hr9, effective_era, effective_k9
    got_hr  = effective_hr9(2.0, 4.0, 3, blend_weight=0.70, min_starts=3)
    got_era = effective_era(4.0, 6.0, 3, blend_weight=0.70, min_starts=3)
    got_k   = effective_k9(8.0, 5.0, 3, blend_weight=0.70, min_starts=3)
    failures = []
    for name, got, want in [("hr9", got_hr, 3.4), ("era", got_era, 5.4), ("k9", got_k, 5.9)]:
        if got is None or abs(got - want) > 1e-6:
            failures.append(f"{name}: got {got}, want {want}")
    if not failures:
        return Result("effective_* honor custom blend_weight / min_starts", Result.PASS)
    return Result("effective_* custom blend_weight", Result.HALT, "; ".join(failures))


def pin_effective_blend_min_starts_gate() -> Result:
    """B4: custom min_starts gate — 2 starts < min=3 falls back to season only."""
    from pitcher_profile import effective_hr9
    got = effective_hr9(2.0, 4.0, 2, min_starts=3)
    if got is not None and abs(got - 2.0) < 1e-6:
        return Result(
            "effective_hr9(starts=2, min_starts=3) -> season only", Result.PASS
        )
    return Result(
        "effective_hr9 custom min_starts gate", Result.HALT,
        f"got {got}, expected 2.0 (season only — 2 starts below custom min of 3)",
    )


def pin_pitcher_recent_window_defaults() -> Result:
    """B4: production defaults preserved — 21-day calendar window."""
    from pitcher_profile import PITCHER_RECENT_WINDOW_TYPE, PITCHER_RECENT_WINDOW_N
    if PITCHER_RECENT_WINDOW_TYPE == "days" and PITCHER_RECENT_WINDOW_N == 21:
        return Result(
            "PITCHER_RECENT_WINDOW defaults = ('days', 21) (production unchanged)",
            Result.PASS,
        )
    return Result(
        "PITCHER_RECENT_WINDOW defaults", Result.HALT,
        f"got ({PITCHER_RECENT_WINDOW_TYPE!r}, {PITCHER_RECENT_WINDOW_N}); flipping "
        "to last-N-starts requires a documented WEIGHT_REFIT_LOG.md decision.",
    )


def pin_score_pitcher_vulnerability_era_recency_lifts_score() -> Result:
    """B4: a pitcher whose recent ERA has collapsed scores higher than season-only."""
    from pitcher_profile import score_pitcher_vulnerability
    season_only = score_pitcher_vulnerability({
        "name": "P_seasonOnly",
        "hr_per_9": 1.5, "era": 3.50, "hard_hit_pct_allowed": 35, "k_per_9": 9.0,
    })
    with_recent_era = score_pitcher_vulnerability({
        "name": "P_withRecentEra",
        "hr_per_9": 1.5, "era": 3.50, "hard_hit_pct_allowed": 35, "k_per_9": 9.0,
        "recent_era_21d": 6.5, "recent_starts_21d": 4,
    })
    if with_recent_era > season_only + 1:
        return Result(
            f"score_pitcher_vulnerability ERA recency: {season_only:.1f} -> {with_recent_era:.1f}",
            Result.PASS,
        )
    return Result(
        "score_pitcher_vulnerability ERA recency lift", Result.HALT,
        f"season_only={season_only:.1f}, with_recent_era={with_recent_era:.1f} (lift <= +1)",
    )


def pin_score_pitcher_vulnerability_recency_lifts_score() -> Result:
    """score_pitcher_vulnerability blends recent into HR/9 — Singer-style
    case (season 1.89, recent 3.07, 3 starts) ranks above season-only."""
    from pitcher_profile import score_pitcher_vulnerability
    season_only = score_pitcher_vulnerability({
        "name": "P_seasonOnly",
        "hr_per_9": 1.89, "era": 5.63, "hard_hit_pct_allowed": 38, "k_per_9": 6.14,
    })
    with_recency = score_pitcher_vulnerability({
        "name": "P_withRecency",
        "hr_per_9": 1.89, "era": 5.63, "hard_hit_pct_allowed": 38, "k_per_9": 6.14,
        "recent_hr9_21d": 3.07, "recent_starts_21d": 3,
    })
    if with_recency > season_only + 1:   # blend must produce non-trivial lift
        return Result(
            f"score_pitcher_vulnerability recency lift: {season_only:.1f} -> {with_recency:.1f}",
            Result.PASS,
        )
    return Result(
        "score_pitcher_vulnerability recency lift",
        Result.HALT,
        f"season_only={season_only:.1f} with_recency={with_recency:.1f} (expected with_recency > season_only + 1)",
    )


def pin_filter_before_drops_after_date() -> Result:
    """PR 3 (as-of-date infra): _filter_before drops rows on/after as_of_date.

    Three rows dated 04-01, 04-15, 05-01. as_of_date=2026-04-15 should keep
    only the 04-01 row (strictly before, not on-or-after).
    """
    try:
        import pandas as pd
    except ImportError:
        return Result("filter_before (pandas missing)", Result.INFO)
    from pitcher_profile import _filter_before
    df = pd.DataFrame({
        "game_date": ["2026-04-01", "2026-04-15", "2026-05-01"],
        "foo": [1, 2, 3],
    })
    res = _filter_before(df, "2026-04-15")
    if len(res) == 1 and res["game_date"].iloc[0] == "2026-04-01":
        return Result(
            "_filter_before drops rows >= as_of_date (strict before)", Result.PASS
        )
    return Result(
        "_filter_before strict-before semantics", Result.HALT,
        f"expected 1 row dated 2026-04-01, got {len(res)}: "
        f"{res['game_date'].tolist() if len(res) else '[]'}",
    )


def pin_filter_before_none_is_noop() -> Result:
    """PR 3: as_of_date=None returns the df unchanged (production behavior)."""
    try:
        import pandas as pd
    except ImportError:
        return Result("filter_before noop (pandas missing)", Result.INFO)
    from pitcher_profile import _filter_before
    df = pd.DataFrame({"game_date": ["2026-04-01", "2026-05-01"], "foo": [1, 2]})
    res = _filter_before(df, None)
    if len(res) == 2:
        return Result("_filter_before(None) is a no-op", Result.PASS)
    return Result(
        "_filter_before(None) no-op", Result.HALT,
        f"expected 2 rows unchanged, got {len(res)}",
    )


def pin_filter_before_empty_safe() -> Result:
    """PR 3: _filter_before handles None / empty / missing-column gracefully."""
    try:
        import pandas as pd
    except ImportError:
        return Result("filter_before empty-safe (pandas missing)", Result.INFO)
    from pitcher_profile import _filter_before
    failures = []
    if _filter_before(None, "2026-01-01") is not None:
        failures.append("None input did not pass through")
    empty = pd.DataFrame()
    if not _filter_before(empty, "2026-01-01").empty:
        failures.append("empty df was not preserved")
    no_col = pd.DataFrame({"other": [1, 2]})
    if len(_filter_before(no_col, "2026-01-01")) != 2:
        failures.append("df without game_date column should pass through")
    if not failures:
        return Result(
            "_filter_before(None / empty / no game_date col) is safe", Result.PASS
        )
    return Result("_filter_before edge cases", Result.HALT, "; ".join(failures))


def pin_as_of_date_signatures_present() -> Result:
    """PR 3: every public profile fn accepts as_of_date kwarg with default None."""
    import inspect
    from pitcher_profile import (
        _fetch_batter_hr_events, _fetch_pitcher_arsenal_statcast,
        build_victim_profile, build_pitcher_profile,
        build_pitcher_profiles_batch, build_victim_profiles_batch,
    )
    failures = []
    for fn in (_fetch_batter_hr_events, _fetch_pitcher_arsenal_statcast,
               build_victim_profile, build_pitcher_profile,
               build_pitcher_profiles_batch, build_victim_profiles_batch):
        sig = inspect.signature(fn)
        if "as_of_date" not in sig.parameters:
            failures.append(f"{fn.__name__} missing as_of_date param")
            continue
        param = sig.parameters["as_of_date"]
        if param.default is not None:
            failures.append(f"{fn.__name__}.as_of_date default is {param.default!r}, expected None")
        if param.kind not in (inspect.Parameter.KEYWORD_ONLY,
                              inspect.Parameter.POSITIONAL_OR_KEYWORD):
            failures.append(f"{fn.__name__}.as_of_date kind = {param.kind}")
    if not failures:
        return Result(
            "as_of_date kwarg present on all 6 profile fns (default None)",
            Result.PASS,
        )
    return Result("as_of_date signatures", Result.HALT, "; ".join(failures))


def pin_aggregate_victim_profile_weighted() -> Result:
    """DB-backed backfill path: _aggregate_victim_profile computes the
    HR-weighted arsenal average and sample-size confidence correctly.

    Synthetic input: 10 HR events across 3 victim pitchers — 5 off pitcher
    1 (FB velo 95), 3 off pitcher 2 (velo 93), 2 off pitcher 3 (velo 91).
    HR-weighted avg_fb_velo = (95*5 + 93*3 + 91*2) / 10 = 93.6.
    hr_count = 10, n_victim_pitchers = 3 -> confidence tier 0.6
    (hr_count >= 8 but n_victim_pitchers < 4, so not the 0.8 tier).
    """
    from pitcher_profile import _aggregate_victim_profile

    def _event(pid):
        return {
            "pitcher_id": pid,
            "p_throws": "R",
            "pitch_type": "FF",
            "release_speed": 93.0,
            "release_spin_rate": 2250.0,
            "release_extension": 6.2,
        }
    events = [_event(1)] * 5 + [_event(2)] * 3 + [_event(3)] * 2
    arsenals = {
        1: {"avg_fb_velo": 95.0, "fb_usage_pct": 0.55, "breaking_usage_pct": 0.30,
            "offspeed_usage_pct": 0.15, "avg_fb_spin": 2300.0, "avg_extension": 6.3},
        2: {"avg_fb_velo": 93.0, "fb_usage_pct": 0.50, "breaking_usage_pct": 0.30,
            "offspeed_usage_pct": 0.20, "avg_fb_spin": 2250.0, "avg_extension": 6.2},
        3: {"avg_fb_velo": 91.0, "fb_usage_pct": 0.45, "breaking_usage_pct": 0.35,
            "offspeed_usage_pct": 0.20, "avg_fb_spin": 2200.0, "avg_extension": 6.0},
    }
    prof = _aggregate_victim_profile(events, arsenals.get)

    failures = []
    if abs(prof.get("avg_fb_velo", -1) - 93.6) > 0.05:
        failures.append(f"avg_fb_velo got {prof.get('avg_fb_velo')}, want 93.6")
    if prof.get("hr_count") != 10:
        failures.append(f"hr_count got {prof.get('hr_count')}, want 10")
    if prof.get("n_victim_pitchers") != 3:
        failures.append(f"n_victim_pitchers got {prof.get('n_victim_pitchers')}, want 3")
    if abs(prof.get("confidence", -1) - 0.6) > 1e-9:
        failures.append(f"confidence got {prof.get('confidence')}, want 0.6")
    # All 10 events p_throws=R -> hand_R_pct = 1.0
    if abs(prof.get("hand_R_pct", -1) - 1.0) > 1e-9:
        failures.append(f"hand_R_pct got {prof.get('hand_R_pct')}, want 1.0")
    if not failures:
        return Result(
            "_aggregate_victim_profile HR-weighted avg + confidence tier",
            Result.PASS,
        )
    return Result(
        "_aggregate_victim_profile(synthetic)", Result.HALT, "; ".join(failures),
    )


def pin_aggregate_victim_profile_no_arsenal_fallback() -> Result:
    """DB-backed backfill path: with no arsenal data,
    _aggregate_victim_profile falls back to per-event release_speed and
    still returns a usable profile (n_victim_pitchers = 0)."""
    from pitcher_profile import _aggregate_victim_profile
    events = [
        {"pitcher_id": p, "p_throws": "L", "pitch_type": "SL",
         "release_speed": 90.0, "release_spin_rate": 2100.0,
         "release_extension": 6.0}
        for p in (11, 12, 13, 14)
    ]
    prof = _aggregate_victim_profile(events, lambda pid: None)
    failures = []
    # No arsenals -> per-event velo mean = 90.0
    if abs(prof.get("avg_fb_velo", -1) - 90.0) > 0.05:
        failures.append(
            f"avg_fb_velo got {prof.get('avg_fb_velo')}, want 90.0 (per-event fallback)"
        )
    if prof.get("n_victim_pitchers") != 0:
        failures.append(f"n_victim_pitchers got {prof.get('n_victim_pitchers')}, want 0")
    # All events p_throws=L -> hand_R_pct = 0.0
    if abs(prof.get("hand_R_pct", -1) - 0.0) > 1e-9:
        failures.append(f"hand_R_pct got {prof.get('hand_R_pct')}, want 0.0")
    if not failures:
        return Result(
            "_aggregate_victim_profile no-arsenal -> per-event velo fallback",
            Result.PASS,
        )
    return Result(
        "_aggregate_victim_profile no-arsenal fallback", Result.HALT,
        "; ".join(failures),
    )


def pin_weather_archive_cache_roundtrip() -> Result:
    """get_weather's archive cache round-trips data correctly and tolerates
    missing keys. Cache is the durability layer for the Open-Meteo archive
    outages observed multiple times in May 2026."""
    from fetch_daily_data import (
        _weather_archive_cache_get,
        _weather_archive_cache_set,
        _weather_archive_cache_path,
    )
    failures = []

    # Path safety: filenames must not contain spaces or special chars
    # (real venue names like "Globe Life Field" must produce safe filenames).
    p = _weather_archive_cache_path("Globe Life Field", "1999-01-01")
    if " " in p.name or "/" in p.name or "\\" in p.name:
        failures.append(f"unsafe cache filename: {p.name!r}")

    # Round-trip. Use a sentinel venue that won't collide with real data.
    venue = "__PIN_TEST_VENUE__"
    date = "1999-01-01"
    sample = {
        "temperature_f": 72.5, "wind_mph": 8.0,
        "_source": "open_meteo_archive",
    }
    _weather_archive_cache_set(venue, date, sample)
    got = _weather_archive_cache_get(venue, date)
    if got != sample:
        failures.append(f"round-trip mismatch: got {got!r}")

    # Clean up the test cache file so we don't pollute the cache dir.
    try:
        _weather_archive_cache_path(venue, date).unlink()
    except Exception:
        pass

    # Missing key must return None, not raise.
    missing = _weather_archive_cache_get("__NO_SUCH_VENUE__", "1900-01-01")
    if missing is not None:
        failures.append(f"missing key returned {missing!r}, expected None")

    if not failures:
        return Result(
            "weather archive cache round-trips + missing -> None", Result.PASS,
        )
    return Result(
        "weather archive cache", Result.HALT, "; ".join(failures),
    )


def pin_weather_retry_config() -> Result:
    """get_weather retries on 5xx + 429 with a sane backoff schedule, and
    does NOT retry 4xx client errors."""
    from fetch_daily_data import (
        _WEATHER_RETRYABLE_STATUSES, _WEATHER_RETRY_BACKOFF_S,
    )
    failures = []
    # Must retry the 5xx codes Open-Meteo's archive actually emits + 429.
    for code in (429, 500, 502, 503, 504):
        if code not in _WEATHER_RETRYABLE_STATUSES:
            failures.append(f"retryable set missing {code}")
    # Must NOT retry 4xx client errors — they bubble straight to the default.
    for code in (400, 401, 403, 404):
        if code in _WEATHER_RETRYABLE_STATUSES:
            failures.append(f"retryable set should NOT include {code}")
    # Backoff schedule: >=3 attempts, non-decreasing, first attempt no-sleep.
    if len(_WEATHER_RETRY_BACKOFF_S) < 3:
        failures.append(f"backoff too short: {_WEATHER_RETRY_BACKOFF_S!r}")
    if list(_WEATHER_RETRY_BACKOFF_S) != sorted(_WEATHER_RETRY_BACKOFF_S):
        failures.append(f"backoff not monotone: {_WEATHER_RETRY_BACKOFF_S!r}")
    if _WEATHER_RETRY_BACKOFF_S and _WEATHER_RETRY_BACKOFF_S[0] != 0:
        failures.append(
            f"first attempt should have no backoff, got {_WEATHER_RETRY_BACKOFF_S[0]}"
        )
    if not failures:
        return Result(
            f"weather retry config: codes={sorted(_WEATHER_RETRYABLE_STATUSES)}, "
            f"backoff={_WEATHER_RETRY_BACKOFF_S}", Result.PASS,
        )
    return Result(
        "weather retry config", Result.HALT, "; ".join(failures),
    )


def pin_backtest_form_anchors_variants_isolate() -> Result:
    """Form harness: backtest_form_anchors imports and each variant scores
    the Bader 2026-05-23 worked example correctly (hr_10g=3, iso_30g=0.186,
    avg_30g=0.163 - the slumping-AVG-but-HR-active pattern that motivated
    the harness)."""
    try:
        from diagnostics import backtest_form_anchors as bfa
    except Exception as e:
        return Result(
            "backtest_form_anchors import", Result.HALT,
            f"failed: {type(e).__name__}: {e}",
        )
    failures = []
    for name in ("fetch_rows", "score_variants", "compute_metrics", "main"):
        if not hasattr(bfa, name):
            failures.append(f"missing {name}")
    for must in ("current", "avg_floor_180", "no_avg", "2x_hr",
                 "hr_iso_only", "hr_only"):
        if must not in bfa.VARIANTS:
            failures.append(f"VARIANTS missing {must!r}")

    # Bader 2026-05-23 worked example. avg_30g=0.163 is below BOTH the
    # current floor (0.210) and the avg_floor_180 candidate (0.180), so
    # those two variants should match. Dropping AVG entirely should
    # nearly double the score (mean of just HR+ISO, no zero-clamp drag).
    bader = {"recent_hr_10g": 3, "recent_iso_30g": 0.186,
             "recent_avg_30g": 0.163, "ev_trend": None}
    expected = {
        "current":       34.33,    # (60+43+0)/3
        "avg_floor_180": 34.33,    # same - 0.163 still sub-floor
        "no_avg":        51.5,     # (60+43)/2
        "2x_hr":         40.75,    # (2*60+43+0)/4
        "hr_iso_only":   51.5,     # same as no_avg (same inputs)
        "hr_only":       60.0,     # just the HR term
    }
    for variant, want in expected.items():
        got = bfa._form_score(bader, variant)
        if abs(got - want) > 0.5:
            failures.append(f"{variant}: got {got:.2f}, want {want:.2f}")

    if not failures:
        return Result(
            "backtest_form_anchors: 6 variants isolate inputs (Bader worked example)",
            Result.PASS,
        )
    return Result(
        "backtest_form_anchors variants", Result.HALT, "; ".join(failures),
    )


def pin_backtest_power_inputs_isolates_variants() -> Result:
    """B6 harness: backtest_power_inputs imports, exposes 6 variants, has
    disjoint synthetic/real key sets, and _compute_power produces distinct
    scores under tight-anchor overrides."""
    try:
        from diagnostics import backtest_power_inputs as bpi
    except Exception as e:
        return Result(
            "backtest_power_inputs import", Result.HALT,
            f"failed: {type(e).__name__}: {e}",
        )
    failures = []

    # Variant list exposes all expected names (6 baseline + 4 from B12 wider
    # windows = 10 total).
    expected_variants = {
        "synthetic-only", "real-only", "blended",
        "real-tight-anchors", "blended-tight-anchors",
        "synthetic-no-hr-encoded",
        "real-21d", "real-28d", "blended-21d", "blended-28d",
    }
    got = set(bpi.VARIANT_NAMES)
    if got != expected_variants:
        failures.append(
            f"VARIANT_NAMES = {sorted(got)}, want {sorted(expected_variants)}"
        )

    # Key sets disjoint (the honest A/B property).
    syn = set(bpi.SYNTHETIC_KEYS)
    real = set(bpi.REAL_KEYS)
    if syn & real:
        failures.append(f"synthetic/real key sets overlap: {sorted(syn & real)}")

    # Entry points present.
    for name in ("fetch_rows", "score_variants", "compute_metrics",
                 "_compute_power", "TIGHT_REAL_ANCHORS", "main"):
        if not hasattr(bpi, name):
            failures.append(f"missing {name}")

    # _has_signal correctness.
    if bpi._has_signal({"iso": None}, ("iso",)):
        failures.append("_has_signal True on None")
    if bpi._has_signal({"iso": 0}, ("iso",)):
        failures.append("_has_signal True on 0")
    if not bpi._has_signal({"iso": 0.2}, ("iso",)):
        failures.append("_has_signal False on a real value")

    # Tight-anchor override changes the score when the value sits between
    # the default floor (8.0) and the tight floor (10.0).
    row = {"recent_barrel_real_14d": 9.0}
    default = bpi._compute_power(row, ("recent_barrel_real_14d",), None)
    tight = bpi._compute_power(
        row, ("recent_barrel_real_14d",), bpi.TIGHT_REAL_ANCHORS,
    )
    # default: scale(9, 8, 18) -> 10.0
    # tight:   scale(9, 10, 22) -> clamped to 0.0
    if abs(default - 10.0) > 0.1:
        failures.append(f"default barrel=9 got {default:.2f}, want 10.0")
    if abs(tight - 0.0) > 0.1:
        failures.append(f"tight barrel=9 got {tight:.2f}, want 0.0 (clamped)")

    # synthetic-no-hr-encoded variant must NOT include the HR-rate-encoded
    # synthetic inputs (barrel_pct, hr_fb_pct) - that's the point.
    nohr = [v for v in bpi.VARIANTS if v[0] == "synthetic-no-hr-encoded"]
    if not nohr:
        failures.append("synthetic-no-hr-encoded variant absent")
    else:
        keys = set(nohr[0][1])
        leaks = keys & {"barrel_pct", "hr_fb_pct"}
        if leaks:
            failures.append(f"synthetic-no-hr-encoded leaks HR-encoded keys: {leaks}")
        if not {"exit_velo", "iso"} <= keys:
            failures.append(f"synthetic-no-hr-encoded missing SLG-encoded: {keys}")

    if not failures:
        return Result(
            "backtest_power_inputs: 6 variants, tight anchors + no-HR-encoded wired",
            Result.PASS,
        )
    return Result(
        "backtest_power_inputs variants", Result.HALT, "; ".join(failures),
    )


def pin_backfill_orchestrator_imports() -> Result:
    """PR 4: backfill_2025 orchestrator imports cleanly + exposes the
    documented entry points."""
    try:
        from etl import backfill_2025 as bf
    except Exception as e:
        return Result(
            "backfill_2025 import", Result.HALT,
            f"failed: {type(e).__name__}: {e}",
        )
    failures = []
    for name in (
        "backfill_one_date", "backfill_window",
        "ensure_2025_outcomes", "bridge_historical_to_outcomes",
        "DEFAULT_START", "DEFAULT_END", "main",
    ):
        if not hasattr(bf, name):
            failures.append(f"missing {name}")
    if not failures:
        return Result(
            "etl.backfill_2025 imports + exposes entry points", Result.PASS,
        )
    return Result("backfill_2025 entry points", Result.HALT, "; ".join(failures))


def pin_backfill_date_range_complete() -> Result:
    """PR 4: _date_range yields every date inclusive of both endpoints."""
    from etl.backfill_2025 import _date_range
    dates = list(_date_range("2025-04-01", "2025-04-05"))
    if dates == [
        "2025-04-01", "2025-04-02", "2025-04-03",
        "2025-04-04", "2025-04-05",
    ]:
        return Result(
            "_date_range yields inclusive [start, end]", Result.PASS,
        )
    return Result(
        "_date_range coverage", Result.HALT,
        f"got {dates}, expected 5 consecutive dates",
    )


def pin_backfill_default_window_full_season() -> Result:
    """PR 4: defaults cover the 2025 regular season start through end."""
    from etl.backfill_2025 import DEFAULT_START, DEFAULT_END
    if DEFAULT_START == "2025-03-27" and DEFAULT_END == "2025-09-30":
        return Result(
            "backfill default window = 2025-03-27..2025-09-30", Result.PASS,
        )
    return Result(
        "backfill default window", Result.HALT,
        f"got ({DEFAULT_START!r}..{DEFAULT_END!r}); the 2025 regular "
        "season runs 2025-03-27 to 2025-09-30",
    )


def pin_generate_card_accepts_as_of_date() -> Result:
    """PR 4: generate_card has the as_of_date kwarg (default None)."""
    import inspect
    from generate_picks import generate_card
    sig = inspect.signature(generate_card)
    if "as_of_date" not in sig.parameters:
        return Result(
            "generate_card(as_of_date)", Result.HALT,
            "missing as_of_date kwarg",
        )
    param = sig.parameters["as_of_date"]
    if param.default is not None:
        return Result(
            "generate_card(as_of_date) default = None", Result.HALT,
            f"got default={param.default!r}",
        )
    return Result(
        "generate_card accepts as_of_date kwarg (default None)", Result.PASS,
    )


def pin_backfill_parse_duration() -> Result:
    """PR 4 chunk flags: parse_duration handles the documented forms.

    Verifies '3h', '90m', '1h30m', '7200' (int as seconds), and rejects
    malformed input with ValueError.
    """
    from etl.backfill_2025 import parse_duration
    cases = [
        (None,       None),
        ("",         None),
        ("3h",       3 * 3600),
        ("90m",      90 * 60),
        ("1h30m",    3600 + 30 * 60),
        ("30m15s",   30 * 60 + 15),
        ("7200",     7200.0),
    ]
    failures = []
    for inp, want in cases:
        got = parse_duration(inp)
        if got != want and not (got is None and want is None):
            failures.append(f"parse_duration({inp!r}) -> {got}, want {want}")
    # Malformed inputs should raise
    raised = False
    try:
        parse_duration("3xyz")
    except ValueError:
        raised = True
    if not raised:
        failures.append("parse_duration('3xyz') did not raise ValueError")
    raised = False
    try:
        parse_duration("h")  # unit without a number
    except ValueError:
        raised = True
    if not raised:
        failures.append("parse_duration('h') did not raise ValueError")
    if not failures:
        return Result("parse_duration handles '3h' / '90m' / '1h30m' / int", Result.PASS)
    return Result("parse_duration", Result.HALT, "; ".join(failures))


def pin_run_backfill_local_wrapper_present() -> Result:
    """PR 4: the run_backfill_local.py wrapper is present, importable, and
    exposes the documented entry points (pull / push / run_orchestrator / main)."""
    from pathlib import Path
    import importlib.util
    repo = Path(__file__).resolve().parent.parent
    wrapper = repo / "run_backfill_local.py"
    if not wrapper.exists():
        return Result("run_backfill_local.py present", Result.HALT,
                      f"missing at {wrapper}")
    # Import as a module so we can poke its functions without spawning subprocs
    spec = importlib.util.spec_from_file_location("run_backfill_local", wrapper)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    failures = []
    for name in ("pull", "push", "run_orchestrator", "main", "_load_dotenv"):
        if not hasattr(mod, name):
            failures.append(f"missing {name}")
    # Verify the .bat shim exists alongside (Windows convention)
    bat = repo / "run_backfill_2025.bat"
    if not bat.exists():
        failures.append(f"run_backfill_2025.bat shim missing at {bat}")
    if not failures:
        return Result(
            "run_backfill_local.py + run_backfill_2025.bat shim present",
            Result.PASS,
        )
    return Result(
        "run_backfill_local.py wrapper", Result.HALT, "; ".join(failures),
    )


def pin_backfill_window_accepts_chunk_flags() -> Result:
    """PR 4 chunk flags: backfill_window has max_dates + max_runtime_s kwargs."""
    import inspect
    from etl.backfill_2025 import backfill_window
    sig = inspect.signature(backfill_window)
    failures = []
    for name in ("max_dates", "max_runtime_s"):
        if name not in sig.parameters:
            failures.append(f"missing {name}")
            continue
        p = sig.parameters[name]
        if p.default is not None:
            failures.append(f"{name} default = {p.default!r}, want None")
    if not failures:
        return Result(
            "backfill_window accepts max_dates + max_runtime_s (default None)",
            Result.PASS,
        )
    return Result("backfill_window chunk kwargs", Result.HALT, "; ".join(failures))


def pin_fetch_live_slate_accepts_as_of_date() -> Result:
    """PR 4: fetch_live_slate + score_live_slate + score_untiered_starters
    all accept as_of_date with default None."""
    import inspect
    from generate_picks import (
        fetch_live_slate, score_live_slate, score_untiered_starters,
    )
    failures = []
    for fn in (fetch_live_slate, score_live_slate, score_untiered_starters):
        sig = inspect.signature(fn)
        if "as_of_date" not in sig.parameters:
            failures.append(f"{fn.__name__} missing as_of_date")
            continue
        if sig.parameters["as_of_date"].default is not None:
            failures.append(f"{fn.__name__}.as_of_date default != None")
    if not failures:
        return Result(
            "fetch/score_*_slate accept as_of_date (default None)", Result.PASS,
        )
    return Result(
        "fetch_live_slate signature", Result.HALT, "; ".join(failures),
    )


def pin_form_fetch_as_of_date_threaded() -> Result:
    """PR 4 fix (2026-05-22): the batter Form fetch path is as-of-date-aware.

    get_recent_game_log + fetch_form_data_batch must accept as_of_date
    (default None). Without it the 2025 backfill's Form factor — the
    joint-heaviest at weight 0.279 — silently uses end-of-season games
    for a mid-season reconstruction (look-ahead bias).
    """
    import inspect
    from fetch_daily_data import get_recent_game_log
    from generate_picks import fetch_form_data_batch
    failures = []
    for fn in (get_recent_game_log, fetch_form_data_batch):
        sig = inspect.signature(fn)
        p = sig.parameters.get("as_of_date")
        if p is None:
            failures.append(f"{fn.__name__} missing as_of_date kwarg")
        elif p.default is not None:
            failures.append(f"{fn.__name__}.as_of_date default = {p.default!r}, want None")
    if not failures:
        return Result(
            "Form fetch (get_recent_game_log + batch) accepts as_of_date",
            Result.PASS,
        )
    return Result("Form fetch as_of_date threading", Result.HALT, "; ".join(failures))


def pin_get_recent_game_log_filters_before_date() -> Result:
    """PR 4 fix: get_recent_game_log's as_of_date cutoff drops on/after games.

    Exercises the filter logic against a synthetic gameLog so we don't
    need a network call. Monkeypatches requests.get to return a canned
    season log of 5 games; with as_of_date set, only the games strictly
    before it should feed the windows.
    """
    import fetch_daily_data as fdd

    class _FakeResp:
        def raise_for_status(self): pass
        def json(self):
            # 5 games; HR on the LAST two (the "future" ones).
            mk = lambda d, hr: {"date": d, "stat": {
                "atBats": 4, "hits": 1, "doubles": 0, "triples": 0, "homeRuns": hr}}
            return {"stats": [{"splits": [
                mk("2025-04-01", 0), mk("2025-04-05", 0), mk("2025-04-10", 0),
                mk("2025-09-01", 1), mk("2025-09-05", 1),
            ]}]}

    orig = fdd.requests.get
    fdd.requests.get = lambda *a, **k: _FakeResp()
    try:
        # as_of_date cutoff at 2025-05-01: the two September games (each
        # with a HR) must be excluded -> recent_hr_10g should be 0.
        cut = fdd.get_recent_game_log(12345, 2025, as_of_date="2025-05-01")
        # no cutoff: all 5 games -> 2 HR.
        full = fdd.get_recent_game_log(12345, 2025)
    finally:
        fdd.requests.get = orig

    failures = []
    if cut.get("recent_hr_10g") != 0:
        failures.append(f"as_of_date=2025-05-01 -> recent_hr_10g={cut.get('recent_hr_10g')}, "
                        "want 0 (Sept HRs are look-ahead, must be excluded)")
    if full.get("recent_hr_10g") != 2:
        failures.append(f"no cutoff -> recent_hr_10g={full.get('recent_hr_10g')}, want 2")
    if not failures:
        return Result(
            "get_recent_game_log(as_of_date) excludes future games", Result.PASS,
        )
    return Result("get_recent_game_log as_of_date cutoff", Result.HALT, "; ".join(failures))


def pin_weather_archive_threshold_present() -> Result:
    """PR 3: get_weather has the archive-endpoint threshold constant set
    to a non-trivial value (5+ days), and both endpoint URLs are defined.
    """
    import fetch_daily_data as fdd
    failures = []
    for name in ("_OPEN_METEO_FORECAST_URL", "_OPEN_METEO_ARCHIVE_URL",
                 "_OPEN_METEO_ARCHIVE_THRESHOLD_DAYS"):
        if not hasattr(fdd, name):
            failures.append(f"missing module attr {name}")
    if hasattr(fdd, "_OPEN_METEO_ARCHIVE_THRESHOLD_DAYS"):
        t = fdd._OPEN_METEO_ARCHIVE_THRESHOLD_DAYS
        if not (isinstance(t, int) and 3 <= t <= 14):
            failures.append(f"threshold {t!r} outside sane [3, 14] range")
    if hasattr(fdd, "_OPEN_METEO_ARCHIVE_URL"):
        if "archive-api.open-meteo.com" not in fdd._OPEN_METEO_ARCHIVE_URL:
            failures.append(f"archive URL is {fdd._OPEN_METEO_ARCHIVE_URL!r}")
    if not failures:
        return Result(
            "get_weather archive-endpoint routing wired", Result.PASS
        )
    return Result("get_weather archive wiring", Result.HALT, "; ".join(failures))


# ---------------------------------------------------------------------------
# B7 (2026-05-25): IL / scratch filter
# ---------------------------------------------------------------------------

def pin_b7_daily_player_status_table_exists() -> Result:
    """B7: create_tables() creates daily_player_status with the documented
    columns and primary key."""
    import sqlite3
    import tempfile
    from pathlib import Path as _Path
    from etl.db import create_tables
    # Use a temp DB so we don't touch the real one.
    with tempfile.TemporaryDirectory() as td:
        p = _Path(td) / "test.db"
        conn = sqlite3.connect(str(p))
        create_tables(conn)
        cols = {r[1]: r for r in conn.execute(
            "PRAGMA table_info(daily_player_status)").fetchall()}
        # Also verify the daily_lineup.lineup_source migration applied.
        lu_cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(daily_lineup)").fetchall()}
        # And daily_picks gets the three B7 columns.
        dp_cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(daily_picks)").fetchall()}
        conn.close()
    expected = {"date", "player_id", "status_code", "status_description",
                "is_likely_out", "source", "fetched_at"}
    missing = expected - set(cols.keys())
    failures = []
    if missing:
        failures.append(f"daily_player_status missing cols: {sorted(missing)}")
    if "lineup_source" not in lu_cols:
        failures.append("daily_lineup.lineup_source missing")
    for c in ("is_likely_out", "status_description", "promoted_due_to"):
        if c not in dp_cols:
            failures.append(f"daily_picks.{c} missing")
    if not failures:
        return Result("B7: daily_player_status + daily_lineup.lineup_source + "
                      "daily_picks IL cols present", Result.PASS)
    return Result("B7 DB migration", Result.HALT, "; ".join(failures))


def pin_b7_fetch_team_roster_status_parses_payload() -> Result:
    """B7: fetch_team_roster_status walks the MLB roster JSON shape and
    extracts {player_id: {status_code, status_description}} correctly.

    Monkeypatches requests.get with a canned payload — no network call.
    """
    import fetch_daily_data as fdd

    class _FakeResp:
        def raise_for_status(self): pass
        def json(self):
            return {"roster": [
                # Active starter
                {"person": {"id": 111, "fullName": "Active Andy"},
                 "status": {"code": "A", "description": "Active"}},
                # 10-day IL
                {"person": {"id": 222, "fullName": "Injured Ian"},
                 "status": {"code": "D10", "description": "10-Day Injured List"}},
                # Paternity
                {"person": {"id": 333, "fullName": "Paternity Pete"},
                 "status": {"code": "PL", "description": "Paternity List"}},
                # Missing person.id (malformed — must skip gracefully)
                {"person": {"fullName": "Ghost"},
                 "status": {"code": "A", "description": "Active"}},
            ]}

    # Bypass the per-process cache so test reruns don't poison each other.
    fdd._TEAM_ROSTER_STATUS_CACHE.clear()
    orig = fdd.requests.get
    fdd.requests.get = lambda *a, **k: _FakeResp()
    try:
        out = fdd.fetch_team_roster_status(team_id=999, date_str="2026-05-25")
    finally:
        fdd.requests.get = orig
        fdd._TEAM_ROSTER_STATUS_CACHE.clear()

    failures = []
    if 111 not in out:
        failures.append("active player 111 missing")
    elif out[111].get("status_code") != "A":
        failures.append(f"player 111 code = {out[111].get('status_code')}, want A")
    if 222 not in out:
        failures.append("IL player 222 missing")
    elif out[222].get("status_code") != "D10":
        failures.append(f"player 222 code = {out[222].get('status_code')}, want D10")
    elif "Injured" not in (out[222].get("status_description") or ""):
        failures.append(f"player 222 description = {out[222].get('status_description')!r}")
    if 333 not in out:
        failures.append("paternity player 333 missing")
    elif out[333].get("status_code") != "PL":
        failures.append(f"player 333 code = {out[333].get('status_code')}, want PL")
    # Malformed entry without person.id should be silently dropped
    if any(pid is None for pid in out):
        failures.append("malformed entry (no person.id) leaked into output")
    if not failures:
        return Result(
            "B7: fetch_team_roster_status parses /teams/{id}/roster correctly",
            Result.PASS,
        )
    return Result("B7 fetch_team_roster_status", Result.HALT, "; ".join(failures))


def pin_b7_generate_card_filter_skips_il_row() -> Result:
    """B7: generate_card's top-8 cut sets selected=False on is_likely_out=1
    rows but keeps them on the full_board.

    Builds a synthetic full_board with one IL'd batter ranked #1 and a
    healthy backup ranked #2 — verifies the card promotes #2 and tags
    them promoted_due_to='il_filter'.
    """
    # We reach inside generate_card's selection logic by replicating it
    # against a synthetic full_board. (The function is monolithic and
    # tightly coupled to fetch_live_slate; the unit-level test is the
    # promotion logic itself.) Mirror the loop verbatim.
    full_board = [
        {"name": "IL Idris", "player_id": 1001, "batting_order": 3,
         "game_pk": 99001, "composite": 88.0, "is_likely_out": 1,
         "status_description": "10-Day Injured List", "tier": 2},
        {"name": "Healthy Hal", "player_id": 1002, "batting_order": 4,
         "game_pk": 99002, "composite": 80.0, "is_likely_out": 0, "tier": 2},
        {"name": "Healthy Hannah", "player_id": 1003, "batting_order": 5,
         "game_pk": 99003, "composite": 78.0, "is_likely_out": 0, "tier": 2},
    ]
    # Same logic as generate_card's top-8 cut (post-B7).
    card = []
    seen_names: set = set()
    global_game_counts: dict = {}
    n_il_skipped = 0
    n_il_promotions = 0
    for batter in full_board:
        if len(card) >= 8:
            break
        name = batter.get("name", "")
        if name in seen_names:
            continue
        bo = batter.get("batting_order")
        if not (isinstance(bo, int) and 1 <= bo <= 9):
            continue
        if batter.get("is_likely_out"):
            n_il_skipped += 1
            continue
        gpk = batter.get("game_pk")
        if global_game_counts.get(gpk, 0) >= 2:
            continue
        batter["selected"] = True
        if n_il_skipped > 0:
            batter["promoted_due_to"] = "il_filter"
            n_il_promotions += 1
        card.append(batter)
        seen_names.add(name)
        global_game_counts[gpk] = global_game_counts.get(gpk, 0) + 1

    failures = []
    # IL'd batter must NOT be in the card
    card_names = {p["name"] for p in card}
    if "IL Idris" in card_names:
        failures.append("IL'd batter ended up in the card")
    if "Healthy Hal" not in card_names:
        failures.append("rank-2 healthy batter wasn't promoted")
    # IL'd batter should still be on the full_board with composite preserved
    il_row = next((b for b in full_board if b["name"] == "IL Idris"), None)
    if il_row is None or il_row.get("composite") != 88.0:
        failures.append("IL'd batter's composite was mutated or removed from full_board")
    if il_row and il_row.get("selected"):
        failures.append("IL'd batter has selected=True")
    # Promotion tagged
    hal = next((p for p in card if p["name"] == "Healthy Hal"), None)
    if hal is None or hal.get("promoted_due_to") != "il_filter":
        failures.append(f"promotion not tagged: {hal!r}")
    # Counter check: exactly one IL skip happened. Both downstream rows
    # (Hal + Hannah) get the promoted_due_to tag because they're both
    # below the skipped IL row in the rank order — this is intended for
    # the calibration audit (any pick that wouldn't have made the top-8
    # without the skip is a "promotion" for retrospective study).
    if n_il_skipped != 1:
        failures.append(f"skipped counter wrong: {n_il_skipped}, want 1")
    if n_il_promotions != 2:
        failures.append(f"promoted counter wrong: {n_il_promotions}, want 2 "
                        "(Hal + Hannah both below the IL skip)")
    if not failures:
        return Result(
            "B7: top-8 cut filters is_likely_out=1, preserves composite, "
            "tags promoted_due_to='il_filter'", Result.PASS,
        )
    return Result("B7 top-8 IL filter", Result.HALT, "; ".join(failures))


def pin_b7_etl_step_2_5_idempotent() -> Result:
    """B7: ETL Step 2.5 (fetch_roster_status) is idempotent — re-running
    against the same date doesn't double-insert into daily_player_status.

    Runs against an in-memory SQLite DB so no real DB is touched.
    """
    import sqlite3
    import tempfile
    from pathlib import Path as _Path
    from etl.db import create_tables
    import fetch_daily_data as fdd
    import etl.etl_morning as em

    # Two cached payloads we'll inject so the test never hits the network.
    fake_lineups = {
        # game_pk -> entry with team ids + lineup data
        555000: {
            "home": [{"player_id": 1, "name": "A", "lineup_source": "recent:2026-05-24"}],
            "away": [{"player_id": 2, "name": "B", "lineup_source": "recent:2026-05-24"}],
            "lineup_posted": False,
            "home_team_id": 100,
            "away_team_id": 200,
        }
    }
    fake_roster = {
        100: {1: {"status_code": "D10", "status_description": "10-Day Injured List"}},
        200: {2: {"status_code": "A", "status_description": "Active"}},
    }

    with tempfile.TemporaryDirectory() as td:
        p = _Path(td) / "test.db"
        conn = sqlite3.connect(str(p))
        create_tables(conn)
        # Seed daily_lineup with two fallback rows for date 2026-05-25.
        conn.execute("""
            INSERT INTO daily_lineup
            (game_pk, date, side, batting_order, player_id, player_name,
             position, team, lineup_source)
            VALUES (555000, '2026-05-25', 'home', 3, 1, 'A', 'CF', 'X', 'recent:2026-05-24')
        """)
        conn.execute("""
            INSERT INTO daily_lineup
            (game_pk, date, side, batting_order, player_id, player_name,
             position, team, lineup_source)
            VALUES (555000, '2026-05-25', 'away', 4, 2, 'B', 'CF', 'Y', 'recent:2026-05-24')
        """)
        conn.commit()

        # Monkeypatch network entrypoints used by fetch_roster_status:
        #   fetch_lineups_for_date -> deterministic fake_lineups
        #   fetch_team_roster_status -> deterministic fake_roster
        orig_lineups = fdd.fetch_lineups_for_date
        orig_roster = fdd.fetch_team_roster_status
        fdd.fetch_lineups_for_date = lambda *a, **k: fake_lineups
        fdd.fetch_team_roster_status = lambda team_id, date_str: fake_roster.get(team_id, {})
        try:
            em.fetch_roster_status(conn, [{"game_pk": 555000}], "2026-05-25")
            n_after_first = conn.execute(
                "SELECT COUNT(*) FROM daily_player_status WHERE date = ?",
                ("2026-05-25",),
            ).fetchone()[0]
            # Run a second time — should remain n_after_first (not double).
            em.fetch_roster_status(conn, [{"game_pk": 555000}], "2026-05-25")
            n_after_second = conn.execute(
                "SELECT COUNT(*) FROM daily_player_status WHERE date = ?",
                ("2026-05-25",),
            ).fetchone()[0]
            # Check is_likely_out wiring.
            il_row = conn.execute(
                "SELECT status_code, is_likely_out FROM daily_player_status "
                "WHERE date = ? AND player_id = 1", ("2026-05-25",)
            ).fetchone()
            active_row = conn.execute(
                "SELECT status_code, is_likely_out FROM daily_player_status "
                "WHERE date = ? AND player_id = 2", ("2026-05-25",)
            ).fetchone()
        finally:
            fdd.fetch_lineups_for_date = orig_lineups
            fdd.fetch_team_roster_status = orig_roster
            conn.close()

    failures = []
    if n_after_first != 2:
        failures.append(f"first run wrote {n_after_first} rows, want 2")
    if n_after_second != n_after_first:
        failures.append(f"re-run doubled rows: {n_after_first} -> {n_after_second}")
    if il_row is None or il_row[0] != "D10" or il_row[1] != 1:
        failures.append(f"IL row wrong: {il_row!r}")
    if active_row is None or active_row[0] != "A" or active_row[1] != 0:
        failures.append(f"active row wrong: {active_row!r}")
    if not failures:
        return Result(
            "B7: fetch_roster_status idempotent + is_likely_out wired (A=0, D10=1)",
            Result.PASS,
        )
    return Result("B7 Step 2.5 idempotency", Result.HALT, "; ".join(failures))


def pin_b7_load_player_status_lookup_signature() -> Result:
    """B7: load_player_status_lookup is defined in generate_picks and
    callable with a date string. With no DB / empty data, returns {}.
    """
    import inspect
    from generate_picks import load_player_status_lookup
    sig = inspect.signature(load_player_status_lookup)
    failures = []
    if "date_str" not in sig.parameters:
        failures.append("missing date_str arg")
    # Calling against a date with no rows must return {} gracefully.
    out = load_player_status_lookup("1900-01-01")
    if not isinstance(out, dict):
        failures.append(f"got {type(out).__name__}, want dict")
    if not failures:
        return Result(
            "B7: load_player_status_lookup(date) -> dict, safe on empty",
            Result.PASS,
        )
    return Result("B7 load_player_status_lookup", Result.HALT, "; ".join(failures))


# ---------------------------------------------------------------------------
# Phase 1 (2026-05-25): pitch-type archetype matchup sub-signal scaffolding
# ---------------------------------------------------------------------------

def pin_batter_pitch_type_splits_table_exists() -> Result:
    """Phase 1: batter_pitch_type_splits table is created by create_tables."""
    import sqlite3
    import tempfile
    from etl.db import create_tables

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        conn = sqlite3.connect(tmp_path)
        create_tables(conn)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='batter_pitch_type_splits'"
        ).fetchall()
        cols = {
            r[1]
            for r in conn.execute(
                "PRAGMA table_info(batter_pitch_type_splits)"
            ).fetchall()
        }
        conn.close()
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    if not rows:
        return Result(
            "batter_pitch_type_splits table exists", Result.HALT,
            "create_tables did not create the table",
        )
    expected = {
        "player_id", "date_through",
        "fb_slg", "fb_pa", "br_slg", "br_pa", "os_slg", "os_pa",
        "fetched_at",
    }
    missing = expected - cols
    if missing:
        return Result(
            "batter_pitch_type_splits columns", Result.HALT,
            f"missing columns: {sorted(missing)}",
        )
    return Result(
        "batter_pitch_type_splits table created with required columns",
        Result.PASS,
    )


def pin_use_arsenal_subsignal_default_off() -> Result:
    """Phase 1: USE_ARSENAL_SUBSIGNAL must default off until backtest
    validates the lift (Phase 3)."""
    from score_batters import USE_ARSENAL_SUBSIGNAL
    if USE_ARSENAL_SUBSIGNAL is False:
        return Result(
            "USE_ARSENAL_SUBSIGNAL default = False (pre-backtest)",
            Result.PASS,
        )
    return Result(
        "USE_ARSENAL_SUBSIGNAL default = False",
        Result.HALT,
        f"got {USE_ARSENAL_SUBSIGNAL}; flipping the arsenal sub-signal on "
        "requires the Phase 3 backtest evidence + a documented decision "
        "in WEIGHT_REFIT_LOG.md.",
    )


def pin_score_matchup_arsenal_flag_off_no_op() -> Result:
    """Phase 1: with USE_ARSENAL_SUBSIGNAL off, fb_slg/br_slg/os_slg on the
    batter dict are IGNORED by score_matchup — score is byte-identical to
    a batter dict without those keys."""
    import score_batters as sb
    pitcher = {
        "throws": "R", "hr_per_9": 1.5, "hard_hit_pct_allowed": 35,
        "fb_usage_pct": 0.55, "breaking_usage_pct": 0.30, "offspeed_usage_pct": 0.15,
    }
    base = sb.score_matchup({"woba_vs_hand": 0.330}, pitcher)
    with_splits = sb.score_matchup(
        {
            "woba_vs_hand": 0.330,
            "fb_slg": 0.600, "fb_pa": 200,
            "br_slg": 0.500, "br_pa": 150,
            "os_slg": 0.550, "os_pa": 80,
        },
        pitcher,
    )
    if abs(with_splits - base) < 0.01:
        return Result(
            f"score_matchup(splits) ignored when flag off ({base:.1f})",
            Result.PASS,
        )
    return Result(
        "score_matchup arsenal flag-off no-op",
        Result.HALT,
        f"base={base}, with_splits={with_splits}; the splits leaked into "
        "the score with USE_ARSENAL_SUBSIGNAL=False",
    )


def pin_compute_xslg_vs_arsenal_basic() -> Result:
    """Phase 1: _compute_xslg_vs_arsenal blends pitcher usage with batter
    splits using the documented formula, when all 3 groups have sufficient PA.

    Synthetic batter: all 3 groups well above the 30-PA threshold.
    Pitcher: 70/20/10 fb/br/os usage.
    Expected: 0.70*0.550 + 0.20*0.300 + 0.10*0.400 = 0.4850.
    """
    from score_batters import _compute_xslg_vs_arsenal
    batter = {
        "fb_slg": 0.550, "fb_pa": 150,
        "br_slg": 0.300, "br_pa": 80,
        "os_slg": 0.400, "os_pa": 50,
    }
    pitcher = {
        "fb_usage_pct": 0.70,
        "breaking_usage_pct": 0.20,
        "offspeed_usage_pct": 0.10,
    }
    got = _compute_xslg_vs_arsenal(batter, pitcher)
    expected = 0.70 * 0.550 + 0.20 * 0.300 + 0.10 * 0.400
    if got is None:
        return Result(
            "_compute_xslg_vs_arsenal returns None unexpectedly",
            Result.HALT, "with valid inputs",
        )
    if abs(got - expected) > 1e-4:
        return Result(
            "_compute_xslg_vs_arsenal blend",
            Result.HALT,
            f"got {got:.4f}, want {expected:.4f}",
        )
    return Result(
        f"_compute_xslg_vs_arsenal(all groups sufficient) -> {got:.4f}",
        Result.PASS,
    )


def pin_compute_xslg_vs_arsenal_short_sample_returns_none() -> Result:
    """Phase 1: small-sample policy — when ANY of the three pitch-type
    groups is below PITCH_TYPE_SPLIT_MIN_BB (or missing), _compute_xslg_vs_arsenal
    returns None and score_matchup skips the sub-signal entirely.

    NO league-avg fallback (policy changed 2026-05-26 per user feedback:
    league-avg imputation artificially flattens small-sample batters to
    a neutral xSLG, inflating their matchup score). "No data" = "no opinion."
    """
    from score_batters import _compute_xslg_vs_arsenal
    pitcher = {"fb_usage_pct": 0.70, "breaking_usage_pct": 0.20, "offspeed_usage_pct": 0.10}

    # Case 1: fb has enough PA, br is below threshold -> None
    batter = {
        "fb_slg": 0.550, "fb_pa": 150,
        "br_slg": 0.250, "br_pa": 5,    # below MIN_BB
        "os_slg": 0.400, "os_pa": 50,
    }
    got = _compute_xslg_vs_arsenal(batter, pitcher)
    if got is not None:
        return Result(
            "_compute_xslg_vs_arsenal short-br-sample",
            Result.HALT,
            f"got {got:.4f}; expected None (br_pa=5 < 30 threshold should "
            "trigger None+skip, NOT league-avg fill)",
        )

    # Case 2: completely missing group -> None
    batter2 = {
        "fb_slg": 0.550, "fb_pa": 150,
        "br_slg": 0.300, "br_pa": 80,
        # os_slg / os_pa entirely absent
    }
    got2 = _compute_xslg_vs_arsenal(batter2, pitcher)
    if got2 is not None:
        return Result(
            "_compute_xslg_vs_arsenal missing-os",
            Result.HALT,
            f"got {got2:.4f}; expected None when a group is absent",
        )
    return Result(
        "_compute_xslg_vs_arsenal small-sample/missing -> None (no league-avg fill)",
        Result.PASS,
    )


def pin_compute_xslg_vs_arsenal_missing_pitcher_arsenal() -> Result:
    """Phase 1: when pitcher arsenal usage is entirely missing, the helper
    returns None — caller skips the sub-signal cleanly."""
    from score_batters import _compute_xslg_vs_arsenal
    batter = {
        "fb_slg": 0.550, "fb_pa": 150,
        "br_slg": 0.300, "br_pa": 80,
        "os_slg": 0.400, "os_pa": 50,
    }
    pitcher = {"throws": "R"}  # no usage keys
    got = _compute_xslg_vs_arsenal(batter, pitcher)
    if got is None:
        return Result(
            "_compute_xslg_vs_arsenal(no arsenal) -> None (clean skip)",
            Result.PASS,
        )
    return Result(
        "_compute_xslg_vs_arsenal no-arsenal None",
        Result.HALT,
        f"got {got}; expected None when pitcher arsenal absent",
    )


def pin_fetch_batter_pitch_type_splits_signature() -> Result:
    """Phase 1: the ETL function exists with as_of_date kwarg defaulting
    to None."""
    import inspect
    from features_v2 import fetch_batter_pitch_type_splits
    sig = inspect.signature(fetch_batter_pitch_type_splits)
    p = sig.parameters.get("as_of_date")
    if p is None:
        return Result(
            "fetch_batter_pitch_type_splits.as_of_date",
            Result.HALT, "missing as_of_date kwarg",
        )
    if p.default is not None:
        return Result(
            "fetch_batter_pitch_type_splits.as_of_date default = None",
            Result.HALT, f"got default={p.default!r}",
        )
    return Result(
        "fetch_batter_pitch_type_splits(as_of_date=None) signature OK",
        Result.PASS,
    )


def pin_pitch_type_split_min_bb_constant() -> Result:
    """Phase 1: PITCH_TYPE_SPLIT_MIN_BB is set to a reasonable per-group PA
    threshold (>=10). Below this, _compute_xslg_vs_arsenal returns None
    instead of filling with league avg — see
    pin_compute_xslg_vs_arsenal_short_sample_returns_none.
    """
    from features_v2 import PITCH_TYPE_SPLIT_MIN_BB
    if not isinstance(PITCH_TYPE_SPLIT_MIN_BB, int) or PITCH_TYPE_SPLIT_MIN_BB < 10:
        return Result(
            "PITCH_TYPE_SPLIT_MIN_BB threshold",
            Result.HALT,
            f"PITCH_TYPE_SPLIT_MIN_BB={PITCH_TYPE_SPLIT_MIN_BB} too low (want >=10)",
        )
    return Result(
        f"PITCH_TYPE_SPLIT_MIN_BB={PITCH_TYPE_SPLIT_MIN_BB}",
        Result.PASS,
    )


# ---------------------------------------------------------------------------
# Phase 2 (2026-05-25): pitch-type archetype real builder + backfill + harness
# ---------------------------------------------------------------------------

def pin_aggregate_pitch_type_splits_basic() -> Result:
    """Phase 2: _aggregate_pitch_type_splits buckets pitch_type codes into
    FB/BR/OS, computes SLG = TB/AB per bucket, and stamps *_pa = AB count.

    Synthetic 6-event sample for one batter:
      FF  home_run -> FB: 1 AB, 4 TB                  (slg=4.000)
      SI  single   -> FB: 2 AB, 5 TB                  (slg=2.500)
      SL  double   -> BR: 1 AB, 2 TB                  (slg=2.000)
      CU  field_out-> BR: 2 AB, 2 TB                  (slg=1.000)
      CH  strikeout-> OS: 1 AB, 0 TB                  (slg=0.000)
      CH  home_run -> OS: 2 AB, 4 TB                  (slg=2.000)
    """
    try:
        import pandas as pd
    except ImportError:
        return Result("_aggregate_pitch_type_splits (pandas missing — skipped)",
                      Result.INFO, "pandas not installed")
    from features_v2 import _aggregate_pitch_type_splits
    df = pd.DataFrame([
        {"batter": 99999, "pitch_type": "FF", "events": "home_run"},
        {"batter": 99999, "pitch_type": "SI", "events": "single"},
        {"batter": 99999, "pitch_type": "SL", "events": "double"},
        {"batter": 99999, "pitch_type": "CU", "events": "field_out"},
        {"batter": 99999, "pitch_type": "CH", "events": "strikeout"},
        {"batter": 99999, "pitch_type": "CH", "events": "home_run"},
    ])
    out = _aggregate_pitch_type_splits(df, player_ids={99999})
    if 99999 not in out:
        return Result("_aggregate_pitch_type_splits basic", Result.HALT,
                      f"missing batter in output: {out!r}")
    e = out[99999]
    failures = []
    if e.get("fb_pa") != 2 or abs(e.get("fb_slg", 0) - 2.5) > 0.001:
        failures.append(f"fb_pa={e.get('fb_pa')} fb_slg={e.get('fb_slg')}; want 2 / 2.5")
    if e.get("br_pa") != 2 or abs(e.get("br_slg", 0) - 1.0) > 0.001:
        failures.append(f"br_pa={e.get('br_pa')} br_slg={e.get('br_slg')}; want 2 / 1.0")
    if e.get("os_pa") != 2 or abs(e.get("os_slg", 0) - 2.0) > 0.001:
        failures.append(f"os_pa={e.get('os_pa')} os_slg={e.get('os_slg')}; want 2 / 2.0")
    if failures:
        return Result("_aggregate_pitch_type_splits basic", Result.HALT,
                      "; ".join(failures))
    return Result(
        "_aggregate_pitch_type_splits basic (FB/BR/OS bucketed, SLG=TB/AB)",
        Result.PASS,
    )


def pin_aggregate_pitch_type_splits_empty() -> Result:
    """Phase 2: empty/null DataFrames don't blow up — returns {}."""
    from features_v2 import _aggregate_pitch_type_splits
    if _aggregate_pitch_type_splits(None) != {}:
        return Result("aggregate empty None", Result.HALT, "None did not return {}")
    try:
        import pandas as pd
        if _aggregate_pitch_type_splits(pd.DataFrame()) != {}:
            return Result("aggregate empty df", Result.HALT,
                          "empty DF did not return {}")
    except ImportError:
        pass
    return Result("_aggregate_pitch_type_splits empty inputs -> {} (no crash)",
                  Result.PASS)


def pin_fetch_batter_pitch_type_splits_empty_ids() -> Result:
    """Phase 2: empty player_ids list short-circuits to {} without
    hitting the Statcast API. (Defensive — production noon runs send
    only the current slate's batters.)"""
    from features_v2 import fetch_batter_pitch_type_splits
    out = fetch_batter_pitch_type_splits([], as_of_date="2025-06-01")
    if out != {}:
        return Result("fetch_batter_pitch_type_splits([]) -> {}", Result.HALT,
                      f"got {out!r}")
    return Result(
        "fetch_batter_pitch_type_splits empty list -> {} (no Statcast pull)",
        Result.PASS,
    )


def pin_pick_inputs_phase2_columns_exist() -> Result:
    """Phase 2: pick_inputs has fb_slg/fb_pa/br_slg/br_pa/os_slg/os_pa
    after create_tables runs. Lets load_picks_to_db.py persist the
    splits per pick row so backtest_arsenal_inputs can replay variants
    without re-pulling Statcast."""
    import sqlite3
    import tempfile
    from etl.db import create_tables
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        conn = sqlite3.connect(tmp_path)
        create_tables(conn)
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(pick_inputs)").fetchall()
        }
        conn.close()
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    expected = {"fb_slg", "fb_pa", "br_slg", "br_pa", "os_slg", "os_pa"}
    missing = expected - cols
    if missing:
        return Result(
            "pick_inputs Phase 2 columns",
            Result.HALT, f"missing: {sorted(missing)}",
        )
    return Result(
        "pick_inputs.{fb,br,os}_slg + *_pa columns present after migration",
        Result.PASS,
    )


def pin_batter_pitch_type_splits_idempotent_write() -> Result:
    """Phase 2: writing the same (player_id, date_through) row twice
    REPLACES rather than duplicating. INSERT OR REPLACE on the primary
    key is how the backfill orchestrator stays idempotent on re-runs."""
    import sqlite3
    import tempfile
    from etl.db import create_tables
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        conn = sqlite3.connect(tmp_path)
        create_tables(conn)
        sql = """
            INSERT OR REPLACE INTO batter_pitch_type_splits (
                player_id, date_through,
                fb_slg, fb_pa, br_slg, br_pa, os_slg, os_pa
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        conn.execute(sql, (12345, "2025-06-01", 0.500, 100, 0.350, 60, 0.400, 40))
        conn.execute(sql, (12345, "2025-06-01", 0.550, 110, 0.360, 65, 0.410, 45))
        conn.commit()
        row = conn.execute(
            "SELECT fb_slg, fb_pa FROM batter_pitch_type_splits "
            "WHERE player_id = 12345 AND date_through = '2025-06-01'"
        ).fetchall()
        conn.close()
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    if len(row) != 1:
        return Result(
            "batter_pitch_type_splits idempotent write",
            Result.HALT, f"got {len(row)} rows; want exactly 1",
        )
    fb_slg, fb_pa = row[0]
    if abs(fb_slg - 0.550) > 0.001 or fb_pa != 110:
        return Result(
            "batter_pitch_type_splits idempotent value",
            Result.HALT, f"got fb_slg={fb_slg}, fb_pa={fb_pa}; want 0.55 / 110",
        )
    return Result(
        "batter_pitch_type_splits INSERT OR REPLACE is idempotent on PK",
        Result.PASS,
    )


def pin_backfill_pitch_type_splits_imports() -> Result:
    """Phase 2: etl/backfill_pitch_type_splits.py imports and exposes the
    documented entry points (backfill_window, backfill_one_date, main)."""
    try:
        from etl import backfill_pitch_type_splits as bpts
    except Exception as e:
        return Result(
            "backfill_pitch_type_splits import", Result.HALT,
            f"failed: {type(e).__name__}: {e}",
        )
    failures = []
    for name in (
        "backfill_window", "backfill_one_date", "main",
        "parse_duration", "DEFAULT_START", "DEFAULT_END",
        "_active_batters_for_date", "_coverage_for_date",
    ):
        if not hasattr(bpts, name):
            failures.append(f"missing {name}")
    if bpts.DEFAULT_START != "2025-03-27":
        failures.append(f"DEFAULT_START={bpts.DEFAULT_START}; want 2025-03-27")
    if bpts.DEFAULT_END != "2025-09-30":
        failures.append(f"DEFAULT_END={bpts.DEFAULT_END}; want 2025-09-30")
    if failures:
        return Result(
            "backfill_pitch_type_splits skeleton",
            Result.HALT, "; ".join(failures),
        )
    return Result(
        "backfill_pitch_type_splits.py: entry points + 2025 window defaults wired",
        Result.PASS,
    )


def pin_load_picks_persists_pitch_type_splits() -> Result:
    """Phase 2: load_picks_to_db.load_picks writes fb_slg/br_slg/os_slg +
    *_pa from inputs dict into pick_inputs columns. Replay idempotency:
    after one load, re-reading pick_inputs returns identical values."""
    import json
    import sqlite3
    import tempfile
    from etl.db import create_tables
    from load_picks_to_db import load_picks
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_db:
        tmp_db_path = Path(tmp_db.name)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as tmp_json:
        tmp_json_path = Path(tmp_json.name)
        payload = {
            "date": "2025-06-01",
            "picks": [],
            "full_board": [
                {
                    "player_id": 99001,
                    "name": "Test Batter",
                    "team": "TST",
                    "game_pk": 7777777,
                    "composite": 75.0,
                    "inputs": {
                        "barrel_pct": 10.0, "exit_velo": 90.0,
                        "hr_fb_pct": 15.0, "iso": 0.200,
                        "fb_slg": 0.550, "fb_pa": 200,
                        "br_slg": 0.420, "br_pa": 110,
                        "os_slg": 0.385, "os_pa": 55,
                    },
                }
            ],
            "scoring_config": "default",
            "mode": "live",
        }
        json.dump(payload, tmp_json)
    try:
        conn = sqlite3.connect(str(tmp_db_path))
        create_tables(conn)
        conn.close()
        load_picks(tmp_json_path, db_path=tmp_db_path)
        conn = sqlite3.connect(str(tmp_db_path))
        row = conn.execute(
            "SELECT fb_slg, fb_pa, br_slg, br_pa, os_slg, os_pa "
            "FROM pick_inputs WHERE date='2025-06-01' AND batter_id=99001"
        ).fetchone()
        conn.close()
    finally:
        tmp_db_path.unlink(missing_ok=True)
        tmp_json_path.unlink(missing_ok=True)
    if not row:
        return Result(
            "load_picks persists pitch-type splits",
            Result.HALT, "no pick_inputs row written",
        )
    fb_slg, fb_pa, br_slg, br_pa, os_slg, os_pa = row
    if (abs(fb_slg - 0.550) > 0.001 or fb_pa != 200
            or abs(br_slg - 0.420) > 0.001 or br_pa != 110
            or abs(os_slg - 0.385) > 0.001 or os_pa != 55):
        return Result(
            "load_picks splits values",
            Result.HALT,
            f"got fb={fb_slg}/{fb_pa} br={br_slg}/{br_pa} os={os_slg}/{os_pa}; "
            "want 0.55/200 0.42/110 0.385/55",
        )
    return Result(
        "load_picks_to_db persists fb/br/os SLG + PA into pick_inputs",
        Result.PASS,
    )


def pin_load_pitch_type_splits_lookup_empty() -> Result:
    """Phase 2: load_pitch_type_splits_lookup returns {} on a fresh DB
    with no batter_pitch_type_splits rows — safe no-op default."""
    from generate_picks import load_pitch_type_splits_lookup
    out = load_pitch_type_splits_lookup("1999-01-01")
    if not isinstance(out, dict):
        return Result(
            "load_pitch_type_splits_lookup returns dict",
            Result.HALT, f"got type {type(out).__name__}",
        )
    return Result(
        "load_pitch_type_splits_lookup safe on missing data (empty dict)",
        Result.PASS,
    )


def pin_backtest_arsenal_inputs_score_variants() -> Result:
    """Phase 2: score_variants computes scores under all 6 variants on a
    synthetic 2-row dataset. The arsenal-blend variant should differ
    from 'current' when the row carries the full arsenal signal; the
    weight_1.0 variant equals arsenal_only score."""
    from diagnostics.backtest_arsenal_inputs import (
        score_variants, VARIANTS, _arsenal_score, _baseline_matchup,
    )
    rows = [
        # Full signal row: pitcher arsenal usage measured, batter splits
        # all >= 30 PA. Should get all variants scored.
        {
            "date": "2025-06-01", "batter_id": 100, "hit_hr": 1,
            "pitcher_hr_per_9": 1.5, "pitcher_hh_pct": 38,
            "woba_vs_hand": 0.340,
            "archetype_similarity": 60,
            "vegas_team_total_pct": 70,
            "pitcher_fb_usage_pct": 0.55,
            "pitcher_br_usage_pct": 0.30,
            "pitcher_os_usage_pct": 0.15,
            "fb_slg": 0.500, "fb_pa": 100,
            "br_slg": 0.380, "br_pa": 50,
            "os_slg": 0.410, "os_pa": 40,
        },
        # No-arsenal row: pitcher usage missing -> all variants fall back
        # to baseline (None+skip absorbed by _matchup_score).
        {
            "date": "2025-06-01", "batter_id": 200, "hit_hr": 0,
            "pitcher_hr_per_9": 1.2, "pitcher_hh_pct": 33,
            "woba_vs_hand": 0.310, "archetype_similarity": 40,
            "vegas_team_total_pct": 50,
            "pitcher_fb_usage_pct": None,
            "pitcher_br_usage_pct": None,
            "pitcher_os_usage_pct": None,
            "fb_slg": None, "fb_pa": 0,
            "br_slg": None, "br_pa": 0,
            "os_slg": None, "os_pa": 0,
        },
    ]
    out = score_variants(rows)
    if len(out) != 2:
        return Result("score_variants row count", Result.HALT, f"got {len(out)}")
    full = out[0]
    no_arsenal = out[1]
    if not full["has_arsenal"]:
        return Result(
            "score_variants common-subset gate",
            Result.HALT, "full-signal row was flagged has_arsenal=False",
        )
    if no_arsenal["has_arsenal"]:
        return Result(
            "score_variants common-subset gate",
            Result.HALT, "no-arsenal row was flagged has_arsenal=True",
        )
    # All 6 variants present per row.
    for v in VARIANTS:
        if v not in full["matchup"]:
            return Result("variants present", Result.HALT, f"missing {v}")
    # weight_1.0 should equal arsenal score alone (within rounding).
    arsenal_only = _arsenal_score(rows[0])
    if arsenal_only is None:
        return Result("arsenal score derivable", Result.HALT, "got None")
    if abs(full["matchup"]["arsenal_weight_1.0"] - arsenal_only) > 0.01:
        return Result(
            "arsenal_weight_1.0 == arsenal_only",
            Result.HALT,
            f"weight_1.0={full['matchup']['arsenal_weight_1.0']} vs "
            f"arsenal_only={arsenal_only}",
        )
    return Result(
        "backtest_arsenal_inputs.score_variants: 6 variants computed, "
        "weight_1.0 == arsenal_only",
        Result.PASS,
    )


def pin_backtest_arsenal_inputs_skeleton_imports() -> Result:
    """Phase 2: backtest_arsenal_inputs imports and exposes documented
    entry points + 6 variants (baseline + production blend + 4-point
    weight sweep)."""
    try:
        from diagnostics import backtest_arsenal_inputs as bai
    except Exception as e:
        return Result(
            "backtest_arsenal_inputs import", Result.HALT,
            f"failed: {type(e).__name__}: {e}",
        )
    failures = []
    for name in (
        "fetch_rows", "score_variants", "compute_metrics", "main",
        "_xslg_vs_arsenal", "_matchup_score", "_has_arsenal_signal",
        "VARIANTS",
    ):
        if not hasattr(bai, name):
            failures.append(f"missing {name}")
    if hasattr(bai, "VARIANTS"):
        expected = {
            "current", "arsenal_blend",
            "arsenal_weight_0.25", "arsenal_weight_0.5",
            "arsenal_weight_0.75", "arsenal_weight_1.0",
        }
        got = set(bai.VARIANTS)
        if got != expected:
            failures.append(f"VARIANTS = {sorted(got)}, want {sorted(expected)}")
    if not failures:
        return Result(
            "backtest_arsenal_inputs: 6 variants + entry points wired",
            Result.PASS,
        )
    return Result(
        "backtest_arsenal_inputs skeleton", Result.HALT, "; ".join(failures),
    )


# ---------------------------------------------------------------------------
# Phase 1 (2026-05-25): park archetype sub-signal scaffolding
# ---------------------------------------------------------------------------

def pin_batter_park_archetype_table_exists() -> Result:
    """Phase 1: batter_park_archetype table is created by create_tables."""
    import tempfile
    import gc
    from etl.db import create_tables

    tmp_dir = tempfile.mkdtemp(prefix="pin_bpa_table_")
    tmp_path = str(Path(tmp_dir) / "test.db")
    try:
        conn = sqlite3.connect(tmp_path)
        try:
            create_tables(conn)
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='batter_park_archetype'"
            ).fetchall()
            cols = {
                r[1]
                for r in conn.execute(
                    "PRAGMA table_info(batter_park_archetype)"
                ).fetchall()
            }
        finally:
            conn.close()
    finally:
        gc.collect()
        try:
            Path(tmp_path).unlink(missing_ok=True)
            Path(tmp_dir).rmdir()
        except OSError:
            pass

    if not rows:
        return Result(
            "batter_park_archetype table exists", Result.HALT,
            "create_tables did not create the table",
        )
    expected = {
        "player_id", "date_through", "feature_centroid_json",
        "n_hrs_used", "fetched_at",
    }
    missing = expected - cols
    if missing:
        return Result(
            "batter_park_archetype columns", Result.HALT,
            f"missing columns: {sorted(missing)}",
        )
    return Result(
        "batter_park_archetype table created with required columns",
        Result.PASS,
    )


def pin_use_park_archetype_flag_default_off() -> Result:
    """Phase 1: USE_PARK_ARCHETYPE must default off until the Phase 3
    backtest validates the lift."""
    from score_batters import USE_PARK_ARCHETYPE
    if USE_PARK_ARCHETYPE is False:
        return Result(
            "USE_PARK_ARCHETYPE default = False (pre-backtest)",
            Result.PASS,
        )
    return Result(
        "USE_PARK_ARCHETYPE default = False",
        Result.HALT,
        f"got {USE_PARK_ARCHETYPE}; flipping the park-archetype sub-signal "
        "on requires the Phase 3 backtest evidence + a documented "
        "decision in WEIGHT_REFIT_LOG.md.",
    )


def pin_score_park_kwarg_none_safe() -> Result:
    """B16: omitting slate_park_pct and passing slate_park_pct=None produce
    byte-identical results. Production-path safety pin — the kwarg is a
    rescore-path-only knob; live scoring (kwarg defaults to None) must not
    change."""
    import pandas as pd
    from score_batters import score_park

    pf = pd.DataFrame([
        {"venue": "Yankee Stadium",
         "hr_pf_overall": 115, "hr_pf_lhb": 128, "hr_pf_rhb": 105},
    ])
    batter = {"bats": "L"}
    omitted = score_park(batter, "Yankee Stadium", pf)
    none_kwarg = score_park(batter, "Yankee Stadium", pf, slate_park_pct=None)
    if abs(omitted - none_kwarg) < 0.001:
        return Result(
            f"score_park(slate_park_pct=None) byte-identical to omitting "
            f"({omitted:.4f})",
            Result.PASS,
        )
    return Result(
        "score_park kwarg-None safety",
        Result.HALT,
        f"omitted={omitted}, none_kwarg={none_kwarg}; B16 kwarg leaked into "
        "the default scoring path",
    )


def pin_score_weather_kwarg_none_safe() -> Result:
    """B16: omitting slate_weather_pct and passing slate_weather_pct=None
    produce byte-identical results. Production-path safety pin."""
    from score_batters import score_weather

    weather = {
        "temperature_f": 75, "wind_mph": 8, "wind_direction_deg": 120,
        "humidity_pct": 55, "dome": False,
    }
    omitted = score_weather(weather, venue="Yankee Stadium", batter_hand="R")
    none_kwarg = score_weather(
        weather, venue="Yankee Stadium", batter_hand="R",
        slate_weather_pct=None,
    )
    if abs(omitted - none_kwarg) < 0.001:
        return Result(
            f"score_weather(slate_weather_pct=None) byte-identical to "
            f"omitting ({omitted:.4f})",
            Result.PASS,
        )
    return Result(
        "score_weather kwarg-None safety",
        Result.HALT,
        f"omitted={omitted}, none_kwarg={none_kwarg}; B16 kwarg leaked "
        "into the default scoring path",
    )


def pin_score_matchup_kwarg_none_safe() -> Result:
    """B16: omitting slate_pitcher_vulnerability_pct and passing
    slate_pitcher_vulnerability_pct=None produce byte-identical results.
    Production-path safety pin."""
    from score_batters import score_matchup

    batter = {"woba_vs_hand": 0.345, "bats": "L"}
    pitcher = {"hr_per_9": 1.6, "hard_hit_pct_allowed": 38, "throws": "R"}
    omitted = score_matchup(batter, pitcher)
    none_kwarg = score_matchup(
        batter, pitcher, slate_pitcher_vulnerability_pct=None,
    )
    if abs(omitted - none_kwarg) < 0.001:
        return Result(
            f"score_matchup(slate_pitcher_vulnerability_pct=None) "
            f"byte-identical to omitting ({omitted:.4f})",
            Result.PASS,
        )
    return Result(
        "score_matchup kwarg-None safety",
        Result.HALT,
        f"omitted={omitted}, none_kwarg={none_kwarg}; B16 kwarg leaked "
        "into the default scoring path",
    )


def pin_slate_pct_columns_exist() -> Result:
    """B16: after the migration, pick_inputs has slate_park_pct,
    slate_weather_pct, slate_pitcher_vulnerability_pct columns. NULL-safe
    additive; pre-B16 rows stay NULL until backfilled."""
    import tempfile
    import gc
    tmp_dir = tempfile.mkdtemp(prefix="pin_b16_cols_")
    tmp_path = str(Path(tmp_dir) / "test.db")
    try:
        from etl.db import create_tables
        conn = sqlite3.connect(tmp_path)
        try:
            create_tables(conn)
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(pick_inputs)"
            ).fetchall()}
        finally:
            conn.close()
    finally:
        gc.collect()
        try:
            Path(tmp_path).unlink(missing_ok=True)
            Path(tmp_dir).rmdir()
        except OSError:
            pass

    expected = ("slate_park_pct", "slate_weather_pct",
                "slate_pitcher_vulnerability_pct")
    missing = [c for c in expected if c not in cols]
    if missing:
        return Result(
            "pick_inputs slate_*_pct columns (B16)", Result.HALT,
            f"missing: {missing}",
        )
    return Result(
        "pick_inputs.slate_park_pct + slate_weather_pct + "
        "slate_pitcher_vulnerability_pct columns present",
        Result.PASS,
    )


def pin_backfill_slate_pct_idempotent() -> Result:
    """B16: running diagnostics/backfill_slate_pct.py twice on the same row
    produces identical values (idempotent UPDATE). Builds a tiny synthetic
    DB and runs the per-date backfill helper twice."""
    import tempfile
    import gc
    tmp_dir = tempfile.mkdtemp(prefix="pin_b16_backfill_")
    tmp_path = str(Path(tmp_dir) / "test.db")
    try:
        from etl.db import create_tables
        from diagnostics.backfill_slate_pct import (
            _backfill_one_date,
            _seed_park_lookup,
            _seed_park_by_overall,
        )
        conn = sqlite3.connect(tmp_path)
        try:
            create_tables(conn)
            date_str = "2025-04-15"
            # Two batters / two games / two pitchers / two venues with
            # different HR park factors so the rank dict has variation.
            rows = [
                (date_str, 100, 115.0, "L",   # venue 1, LHB
                 78, 10, 60, 0,
                 1.5, 4.0, 38, 8.0, 38, None, None, None, None,
                 "L", "R"),
                (date_str, 101, 90.0, "R",    # venue 2, RHB
                 72, 5, 55, 0,
                 0.9, 3.2, 32, 9.5, 33, None, None, None, None,
                 "R", "R"),
            ]
            for r in rows:
                conn.execute(
                    "INSERT INTO pick_inputs ("
                    "date, batter_id, hr_park_factor, bats, "
                    "temperature_f, wind_mph, humidity_pct, is_dome, "
                    "pitcher_hr_per_9, pitcher_era, pitcher_hh_pct, "
                    "pitcher_k_per_9, pitcher_fb_pct_allowed, "
                    "pitcher_recent_hr9_21d, pitcher_recent_starts_21d, "
                    "pitcher_recent_era_21d, pitcher_recent_k9_21d, "
                    "bats, throws"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    r,
                )
            # Daily picks rows for the JOIN
            conn.execute(
                "INSERT INTO daily_picks "
                "(date, batter_id, batter_name, game_pk, opp_pitcher, "
                " composite, mode, rank_in_board, selected) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (date_str, 100, "B1", 5001, "Pitch One", 50, "live", 1, 1),
            )
            conn.execute(
                "INSERT INTO daily_picks "
                "(date, batter_id, batter_name, game_pk, opp_pitcher, "
                " composite, mode, rank_in_board, selected) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (date_str, 101, "B2", 5002, "Pitch Two", 50, "live", 2, 1),
            )
            conn.commit()

            park_seed = _seed_park_lookup()
            park_seed_by_overall = _seed_park_by_overall()
            _backfill_one_date(
                conn, date_str, park_seed, park_seed_by_overall, dry_run=False
            )
            first = conn.execute(
                "SELECT batter_id, slate_park_pct, slate_weather_pct, "
                "slate_pitcher_vulnerability_pct FROM pick_inputs "
                "WHERE date = ? ORDER BY batter_id",
                (date_str,),
            ).fetchall()
            # Run a second time
            _backfill_one_date(
                conn, date_str, park_seed, park_seed_by_overall, dry_run=False
            )
            second = conn.execute(
                "SELECT batter_id, slate_park_pct, slate_weather_pct, "
                "slate_pitcher_vulnerability_pct FROM pick_inputs "
                "WHERE date = ? ORDER BY batter_id",
                (date_str,),
            ).fetchall()
        finally:
            conn.close()
    finally:
        gc.collect()
        try:
            Path(tmp_path).unlink(missing_ok=True)
            Path(tmp_dir).rmdir()
        except OSError:
            pass

    if first != second:
        return Result(
            "backfill_slate_pct idempotent (B16)", Result.HALT,
            f"first={first}, second={second}; re-running mutated values",
        )
    # Also sanity-check at least one of them got populated
    non_null = sum(
        1 for row in first
        if any(v is not None for v in row[1:])
    )
    if non_null == 0:
        return Result(
            "backfill_slate_pct populated at least one slate_pct", Result.HALT,
            f"all NULL after backfill: {first}",
        )
    return Result(
        "backfill_slate_pct re-runnable, values stable across two passes "
        f"(populated {non_null}/2 rows)",
        Result.PASS,
    )


def pin_score_park_archetype_flag_off_no_op() -> Result:
    """Phase 1: with USE_PARK_ARCHETYPE off, park_archetype_centroid on the
    batter dict is IGNORED by score_park - score is byte-identical to a
    batter dict without the key. This is the load-bearing
    "production scoring is unchanged this PR" guarantee."""
    import pandas as pd
    import score_batters as sb

    park_factors = pd.DataFrame([
        {"venue": "Yankee Stadium",
         "hr_pf_overall": 115, "hr_pf_lhb": 128, "hr_pf_rhb": 105},
    ])
    # The centroid is wildly out-of-distribution; would massively shift
    # the score if the flag accidentally read it.
    bogus_centroid = [99.0, 99.0, 99.0, 99.0, 99.0, 99.0]

    base = sb.score_park({"bats": "L"}, "Yankee Stadium", park_factors)
    with_centroid = sb.score_park(
        {"bats": "L", "park_archetype_centroid": bogus_centroid},
        "Yankee Stadium", park_factors,
    )
    if abs(with_centroid - base) < 0.01:
        return Result(
            f"score_park(centroid) ignored when flag off ({base:.2f})",
            Result.PASS,
        )
    return Result(
        "score_park archetype flag-off no-op",
        Result.HALT,
        f"base={base}, with_centroid={with_centroid}; the centroid leaked "
        "into the score with USE_PARK_ARCHETYPE=False",
    )


def pin_compute_park_archetype_match_none_passes_through() -> Result:
    """Phase 1: helper returns None when either input is None.

    None propagation is the load-bearing semantic - the caller (score_park)
    uses None as the "skip the archetype term, fall back to base park
    logic" signal. NEVER a league-avg fallback."""
    from score_batters import _compute_park_archetype_match
    failures = []
    if _compute_park_archetype_match(None, [1.0, 2.0, 3.0]) is not None:
        failures.append("today_vec=None did not produce None")
    if _compute_park_archetype_match([1.0, 2.0, 3.0], None) is not None:
        failures.append("batter_centroid=None did not produce None")
    if _compute_park_archetype_match(None, None) is not None:
        failures.append("both None did not produce None")
    if _compute_park_archetype_match([], [1.0, 2.0]) is not None:
        failures.append("empty today_vec did not produce None")
    if _compute_park_archetype_match([1.0, 2.0], []) is not None:
        failures.append("empty batter_centroid did not produce None")
    if _compute_park_archetype_match([1.0, 2.0], [1.0, 2.0, 3.0]) is not None:
        failures.append("dimension mismatch did not produce None")
    if not failures:
        return Result(
            "_compute_park_archetype_match(None/empty/mismatch) -> None",
            Result.PASS,
        )
    return Result(
        "_compute_park_archetype_match None propagation",
        Result.HALT, "; ".join(failures),
    )


def pin_compute_park_archetype_match_basic() -> Result:
    """Phase 1: helper produces the documented L2-distance-to-score mapping.

    Identical vectors -> distance 0 -> score 100.
    Vectors at the FAR anchor distance -> score 0.
    Halfway -> ~50.
    """
    from score_batters import (
        _compute_park_archetype_match,
        PARK_ARCHETYPE_DIST_FAR,
    )
    # Distance 0: identical vectors -> score 100.
    v = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    s_identical = _compute_park_archetype_match(v, v)
    if s_identical is None or abs(s_identical - 100.0) > 0.1:
        return Result(
            "_compute_park_archetype_match identical vectors",
            Result.HALT,
            f"expected 100, got {s_identical}",
        )
    # Distance == FAR anchor along one axis -> score 0.
    today_far = [PARK_ARCHETYPE_DIST_FAR, 0.0, 0.0, 0.0, 0.0, 0.0]
    centroid_zero = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    s_far = _compute_park_archetype_match(today_far, centroid_zero)
    if s_far is None or abs(s_far - 0.0) > 0.5:
        return Result(
            "_compute_park_archetype_match at FAR anchor",
            Result.HALT,
            f"expected ~0, got {s_far}",
        )
    return Result(
        f"_compute_park_archetype_match(identical -> {s_identical:.1f}, "
        f"far -> {s_far:.1f})",
        Result.PASS,
    )


def pin_compute_batter_park_archetype_below_threshold_returns_none() -> Result:
    """Phase 1: builder returns None centroid for batters with fewer than
    PARK_ARCHETYPE_MIN_HRS HRs - the None+skip policy, NOT a league-avg
    fallback."""
    import tempfile
    import gc
    from etl.db import create_tables
    from features_v2 import compute_batter_park_archetype, PARK_ARCHETYPE_MIN_HRS

    tmp_dir = tempfile.mkdtemp(prefix="pin_bpa_")
    tmp_path = str(Path(tmp_dir) / "test.db")
    try:
        conn = sqlite3.connect(tmp_path)
        try:
            create_tables(conn)
            # Populate one HR event (well below the 10-HR threshold) so
            # we can verify the under-threshold branch.
            conn.execute(
                "INSERT INTO batter_hr_events "
                "(batter_id, pitcher_id, game_date, game_pk) "
                "VALUES (?, ?, ?, ?)",
                (123, 999, "2024-04-15", 700001),
            )
            conn.execute(
                "INSERT INTO daily_slate (game_pk, date, venue) "
                "VALUES (?, ?, ?)",
                (700001, "2024-04-15", "Yankee Stadium"),
            )
            conn.commit()
        finally:
            conn.close()
        result = compute_batter_park_archetype(
            [123], as_of_date="2026-01-01", db_path=tmp_path,
        )
    finally:
        gc.collect()  # Windows: release any lingering sqlite handles.
        try:
            Path(tmp_path).unlink(missing_ok=True)
            Path(tmp_dir).rmdir()
        except OSError:
            pass  # Cleanup best-effort; the temp dir is short-lived anyway.

    entry = result.get(123)
    if entry is None:
        return Result(
            "compute_batter_park_archetype(below-threshold)",
            Result.HALT, "no entry for batter 123",
        )
    if entry.get("centroid") is not None:
        return Result(
            "compute_batter_park_archetype below threshold",
            Result.HALT,
            f"got centroid={entry['centroid']!r}; expected None for "
            f"{entry.get('n_hrs_used')} HRs (< MIN={PARK_ARCHETYPE_MIN_HRS}). "
            "None+skip policy is the load-bearing constraint.",
        )
    return Result(
        f"compute_batter_park_archetype(n_hrs={entry.get('n_hrs_used')} "
        f"< MIN={PARK_ARCHETYPE_MIN_HRS}) -> centroid=None (skip)",
        Result.PASS,
    )


def pin_park_archetype_constants() -> Result:
    """Phase 1: PARK_ARCHETYPE_MIN_HRS, PARK_FEATURE_KEYS, and
    PARK_FEATURE_STATS are exposed with sane shapes."""
    from features_v2 import (
        PARK_ARCHETYPE_MIN_HRS, PARK_FEATURE_KEYS, PARK_FEATURE_STATS,
    )
    failures = []
    if not isinstance(PARK_ARCHETYPE_MIN_HRS, int):
        failures.append(f"MIN_HRS is {type(PARK_ARCHETYPE_MIN_HRS).__name__}")
    elif not (3 <= PARK_ARCHETYPE_MIN_HRS <= 50):
        failures.append(
            f"MIN_HRS={PARK_ARCHETYPE_MIN_HRS} outside sane [3, 50] range"
        )
    if not isinstance(PARK_FEATURE_KEYS, tuple):
        failures.append(f"PARK_FEATURE_KEYS is {type(PARK_FEATURE_KEYS).__name__}")
    elif len(PARK_FEATURE_KEYS) < 4:
        failures.append(
            f"PARK_FEATURE_KEYS too short: {len(PARK_FEATURE_KEYS)} elements"
        )
    if not isinstance(PARK_FEATURE_STATS, dict):
        failures.append(
            f"PARK_FEATURE_STATS is {type(PARK_FEATURE_STATS).__name__}"
        )
    else:
        for k in PARK_FEATURE_KEYS:
            ms = PARK_FEATURE_STATS.get(k)
            if not isinstance(ms, tuple) or len(ms) != 2:
                failures.append(f"stat for {k} is {ms!r}")
                break
    if not failures:
        return Result(
            f"PARK_FEATURE_KEYS ({len(PARK_FEATURE_KEYS)}), "
            f"MIN_HRS={PARK_ARCHETYPE_MIN_HRS}, stats well-formed",
            Result.PASS,
        )
    return Result(
        "park archetype constants", Result.HALT, "; ".join(failures),
    )


def pin_backtest_park_archetype_skeleton_imports() -> Result:
    """Phase 1: backtest_park_archetype imports and exposes documented
    entry points + 6 variants. Doesn't run it - the SQL fetch requires
    Phase 2 columns."""
    try:
        from diagnostics import backtest_park_archetype as bpa
    except Exception as e:
        return Result(
            "backtest_park_archetype import", Result.HALT,
            f"failed: {type(e).__name__}: {e}",
        )
    failures = []
    for name in (
        "fetch_rows", "score_variants", "compute_metrics", "main",
        "_base_park_score", "_archetype_score", "_park_score",
        "_has_archetype_signal", "VARIANTS",
    ):
        if not hasattr(bpa, name):
            failures.append(f"missing {name}")
    if hasattr(bpa, "VARIANTS"):
        # Phase 2 (2026-05-25) renamed the weighted variants from
        # _low/_high to _0.25/_0.75 and added _resolve_weighted_thresholds
        # so main() anchors them to the AUC-winning threshold from the
        # 5/10/20 sweep.
        expected = {
            "default", "archetype_5hr", "archetype_10hr", "archetype_20hr",
            "archetype_weighted_0.25", "archetype_weighted_0.75",
        }
        got = set(bpa.VARIANTS)
        if got != expected:
            failures.append(f"VARIANTS = {sorted(got)}, want {sorted(expected)}")
    if not failures:
        return Result(
            "backtest_park_archetype: 6 variants + entry points wired",
            Result.PASS,
        )
    return Result(
        "backtest_park_archetype skeleton", Result.HALT, "; ".join(failures),
    )


# ---------------------------------------------------------------------------
# Park archetype Phase 2 pins (2026-05-25)
# ---------------------------------------------------------------------------

def pin_backfill_park_archetype_cli_present() -> Result:
    """Phase 2: etl/backfill_park_archetype imports + exposes the chunked
    CLI flags from backfill_2025 (--start / --end / --max-dates /
    --max-runtime). Run via subprocess --help so we don't hit the DB."""
    import subprocess
    proj = Path(__file__).resolve().parent.parent
    try:
        result = subprocess.run(
            [sys.executable, "-m", "etl.backfill_park_archetype", "--help"],
            capture_output=True, text=True, timeout=30, cwd=str(proj),
        )
    except Exception as e:
        return Result(
            "backfill_park_archetype --help", Result.HALT,
            f"failed: {type(e).__name__}: {e}",
        )
    if result.returncode != 0:
        return Result(
            "backfill_park_archetype --help",
            Result.HALT,
            f"exit {result.returncode}: {result.stderr[:300]}",
        )
    help_text = result.stdout
    missing = [f for f in ("--start", "--end", "--max-dates", "--max-runtime")
               if f not in help_text]
    if missing:
        return Result(
            "backfill_park_archetype CLI flags", Result.HALT,
            f"missing flags: {missing}",
        )
    return Result(
        "backfill_park_archetype: --start/--end/--max-dates/--max-runtime wired",
        Result.PASS,
    )


def pin_backfill_park_archetype_idempotent() -> Result:
    """Phase 2: backfill_one_date is idempotent on (player_id, date_through).
    Running it twice over the same date is safe (INSERT OR REPLACE)."""
    import tempfile
    import gc
    tmp_dir = tempfile.mkdtemp(prefix="pin_bpa_idem_")
    tmp_path = str(Path(tmp_dir) / "test.db")
    try:
        from etl.db import create_tables
        from etl.backfill_park_archetype import backfill_one_date
        conn = sqlite3.connect(tmp_path)
        try:
            create_tables(conn)
            # Single batter, single venue HR -- below 10-HR threshold so
            # the centroid stays None but the row is still persisted.
            conn.execute(
                "INSERT INTO batter_hr_events "
                "(batter_id, pitcher_id, game_date, game_pk) "
                "VALUES (?, ?, ?, ?)",
                (501, 999, "2025-04-15", 700001),
            )
            conn.execute(
                "INSERT INTO daily_slate (game_pk, date, venue) "
                "VALUES (?, ?, ?)",
                (700001, "2025-04-15", "Yankee Stadium"),
            )
            conn.commit()

            # First pass.
            r1 = backfill_one_date(conn, "2025-06-01", tmp_path)
            # Second pass with same args -- should not duplicate rows.
            r2 = backfill_one_date(conn, "2025-06-01", tmp_path)

            n_rows = conn.execute(
                "SELECT COUNT(*) FROM batter_park_archetype "
                "WHERE date_through = '2025-06-01' AND player_id = 501"
            ).fetchone()[0]
        finally:
            conn.close()
    finally:
        gc.collect()
        try:
            Path(tmp_path).unlink(missing_ok=True)
            Path(tmp_dir).rmdir()
        except OSError:
            pass

    if n_rows != 1:
        return Result(
            "backfill_park_archetype idempotence", Result.HALT,
            f"expected 1 row after 2 backfill calls, got {n_rows}",
        )
    if r1["batters"] != r2["batters"]:
        return Result(
            "backfill_park_archetype idempotence", Result.HALT,
            f"counts diverged: r1={r1}, r2={r2}",
        )
    return Result(
        "backfill_park_archetype: re-run is idempotent (INSERT OR REPLACE)",
        Result.PASS,
    )


def pin_pick_inputs_park_archetype_columns_exist() -> Result:
    """Phase 2: pick_inputs has park_archetype_centroid_json +
    park_archetype_n_hrs after create_tables migration. NULL-safe additive."""
    import tempfile
    import gc
    tmp_dir = tempfile.mkdtemp(prefix="pin_pi_pa_")
    tmp_path = str(Path(tmp_dir) / "test.db")
    try:
        from etl.db import create_tables
        conn = sqlite3.connect(tmp_path)
        try:
            create_tables(conn)
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(pick_inputs)"
            ).fetchall()}
        finally:
            conn.close()
    finally:
        gc.collect()
        try:
            Path(tmp_path).unlink(missing_ok=True)
            Path(tmp_dir).rmdir()
        except OSError:
            pass

    missing = [c for c in (
        "park_archetype_centroid_json", "park_archetype_n_hrs",
    ) if c not in cols]
    if missing:
        return Result(
            "pick_inputs park_archetype columns", Result.HALT,
            f"missing: {missing}",
        )
    return Result(
        "pick_inputs.park_archetype_centroid_json + park_archetype_n_hrs added",
        Result.PASS,
    )


def pin_backtest_park_archetype_runs_against_populated_db() -> Result:
    """Phase 2: harness runs against a populated DB and produces the
    6-variant grid with non-zero counts. Builds a tiny synthetic DB
    (5 dates, ~30 batters, mixed venues) so we can validate the wiring
    without depending on the production data layer."""
    import tempfile
    import gc
    import subprocess
    import json as _json
    import random

    tmp_dir = tempfile.mkdtemp(prefix="pin_bpa_run_")
    tmp_path = str(Path(tmp_dir) / "test.db")
    proj = Path(__file__).resolve().parent.parent
    try:
        from etl.db import create_tables
        conn = sqlite3.connect(tmp_path)
        try:
            create_tables(conn)
            # Build a small set of HRs across 5 venues so 3+ batters
            # clear the 5-HR threshold on dates after 2025-04-15.
            venues = ["Yankee Stadium", "Fenway Park", "Coors Field",
                      "Petco Park", "Oracle Park"]
            random.seed(42)
            game_pk_seed = 800000
            for bid in range(601, 630):  # 29 batters
                n_hrs = 6 + (bid % 8)  # 6..13 HRs per batter
                for h in range(n_hrs):
                    gpk = game_pk_seed
                    game_pk_seed += 1
                    venue = venues[(bid + h) % len(venues)]
                    game_date = f"2025-04-{(h % 14) + 1:02d}"
                    conn.execute(
                        "INSERT INTO batter_hr_events "
                        "(batter_id, pitcher_id, game_date, game_pk) "
                        "VALUES (?, ?, ?, ?)",
                        (bid, 999, game_date, gpk),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO daily_slate "
                        "(game_pk, date, venue) VALUES (?, ?, ?)",
                        (gpk, game_date, venue),
                    )

            # Generate pick_inputs + daily_picks + daily_slate + outcomes
            # for 5 evaluation dates (2025-05-01..05) against those same
            # batters. The harness JOINs through daily_picks -> daily_slate
            # to resolve venue, so all three need rows.
            for d_offset in range(5):
                eval_date = f"2025-05-{d_offset + 1:02d}"
                for bid in range(601, 630):
                    # Half the batters "hit a HR" on each date.
                    hit_hr = 1 if (bid + d_offset) % 2 == 0 else 0
                    venue = venues[(bid + d_offset) % len(venues)]
                    eval_gpk = 900000 + d_offset * 100 + (bid % 30)
                    conn.execute(
                        "INSERT OR IGNORE INTO daily_slate "
                        "(game_pk, date, venue) VALUES (?, ?, ?)",
                        (eval_gpk, eval_date, venue),
                    )
                    conn.execute(
                        "INSERT INTO pick_inputs "
                        "(date, batter_id, bats) "
                        "VALUES (?, ?, ?)",
                        (eval_date, bid, "R"),
                    )
                    conn.execute(
                        "INSERT INTO daily_picks "
                        "(date, batter_id, batter_name, game_pk, tier) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (eval_date, bid, f"Batter {bid}", eval_gpk, 1),
                    )
                    conn.execute(
                        "INSERT INTO outcomes "
                        "(date, batter_id, hr_count, game_pk) "
                        "VALUES (?, ?, ?, ?)",
                        (eval_date, bid, hit_hr, eval_gpk),
                    )
            conn.commit()
        finally:
            conn.close()

        # Run the backfill to populate batter_park_archetype.
        bf = subprocess.run(
            [sys.executable, "-m", "etl.backfill_park_archetype",
             "--start", "2025-05-01", "--end", "2025-05-05",
             "--db", tmp_path],
            capture_output=True, text=True, timeout=60, cwd=str(proj),
        )
        if bf.returncode != 0:
            return Result(
                "backfill_park_archetype run", Result.HALT,
                f"exit {bf.returncode}: {bf.stderr[:400]}",
            )

        # Decorate pick_inputs by JOINing back from batter_park_archetype.
        # Mimics load_picks_to_db's enrichment without exercising the full
        # picks-JSON flow.
        conn = sqlite3.connect(tmp_path)
        try:
            conn.execute("""
                UPDATE pick_inputs SET
                  park_archetype_centroid_json = (
                    SELECT feature_centroid_json FROM batter_park_archetype
                    WHERE player_id = pick_inputs.batter_id
                      AND date_through = pick_inputs.date
                  ),
                  park_archetype_n_hrs = (
                    SELECT n_hrs_used FROM batter_park_archetype
                    WHERE player_id = pick_inputs.batter_id
                      AND date_through = pick_inputs.date
                  )
            """)
            conn.commit()
        finally:
            conn.close()

        # Run the harness.
        out = subprocess.run(
            [sys.executable, "-m", "diagnostics.backtest_park_archetype",
             "--start", "2025-05-01", "--end", "2025-05-05",
             "--db", tmp_path],
            capture_output=True, text=True, timeout=60, cwd=str(proj),
        )
        if out.returncode != 0:
            return Result(
                "backtest_park_archetype run", Result.HALT,
                f"exit {out.returncode}: stderr={out.stderr[:300]} "
                f"stdout={out.stdout[:300]}",
            )
        # Sanity: the report contains all 6 variant rows.
        missing = [v for v in (
            "default", "archetype_5hr", "archetype_10hr", "archetype_20hr",
            "archetype_weighted_0.25", "archetype_weighted_0.75",
        ) if v not in out.stdout]
        if missing:
            return Result(
                "backtest_park_archetype variant grid", Result.HALT,
                f"missing variants in report: {missing}; "
                f"got: {out.stdout[:600]}",
            )
    finally:
        gc.collect()
        try:
            Path(tmp_path).unlink(missing_ok=True)
            Path(tmp_dir).rmdir()
        except OSError:
            pass

    return Result(
        "backtest_park_archetype: 6 variants render against populated DB",
        Result.PASS,
    )


# ---------------------------------------------------------------------------
# 2026-05-26: Form archetype Phase 1 (sub-signal scaffolding)
# ---------------------------------------------------------------------------

def pin_use_form_archetype_default_off() -> Result:
    """USE_FORM_ARCHETYPE must stay False until Phase 3 backtest validates it."""
    from score_batters import USE_FORM_ARCHETYPE
    if USE_FORM_ARCHETYPE is False:
        return Result(
            "USE_FORM_ARCHETYPE default = False (Phase 1)", Result.PASS,
        )
    return Result(
        "USE_FORM_ARCHETYPE default = False",
        Result.HALT,
        f"got {USE_FORM_ARCHETYPE}; flipping the form-archetype sub-signal on "
        "requires a documented Phase-3 backtest decision in WEIGHT_REFIT_LOG.md.",
    )


def pin_score_form_archetype_flag_off_no_op() -> Result:
    """With flag OFF, form_archetype_* keys on the batter dict are IGNORED.

    Verifies that the flag-OFF score is byte-identical to a batter dict
    without the archetype keys — load-bearing for "production scoring
    unchanged" in this PR.
    """
    import score_batters as sb
    base_inputs = {
        "recent_hr_10g": 2.0,
        "recent_iso_30g": 0.180,
        "ev_trend": None,
    }
    bare = sb.score_form(base_inputs)
    with_keys = sb.score_form({
        **base_inputs,
        # Extreme values — if they leak in they'd swing the score.
        "form_archetype_today_vector": [0.4, 12.0, 11.0, 35.0, 4, 1, 0.270],
        "form_archetype_centroid": [0.4, 12.0, 11.0, 35.0, 4, 1, 0.270],
    })
    if abs(bare - with_keys) < 0.01:
        return Result(
            f"score_form ignores archetype keys when flag off ({bare:.1f})",
            Result.PASS,
        )
    return Result(
        "score_form flag-off no-op",
        Result.HALT,
        f"bare={bare}, with_archetype_keys={with_keys}; archetype keys leaked "
        "into the score with USE_FORM_ARCHETYPE=False.",
    )


def pin_compute_form_archetype_match_returns_none_on_missing() -> Result:
    """Helper returns None when either input vector is None — no fallback."""
    from score_batters import _compute_form_archetype_match
    cases = [
        (None, [0.4, 12, 11, 35, 4, 1, 0.27]),
        ([0.4, 12, 11, 35, 4, 1, 0.27], None),
        (None, None),
    ]
    failures = []
    for today, centroid in cases:
        got = _compute_form_archetype_match(today, centroid)
        if got is not None:
            failures.append(f"today={today}, centroid={centroid}: got {got}, want None")

    # Also: all-None per-slot inputs should also return None (no information).
    n_feats = 7
    all_none_today = [None] * n_feats
    all_none_centroid = [None] * n_feats
    got = _compute_form_archetype_match(all_none_today, all_none_centroid)
    if got is not None:
        failures.append(f"all-None vectors: got {got}, want None")

    if not failures:
        return Result(
            "_compute_form_archetype_match None+skip (no league-avg fallback)",
            Result.PASS,
        )
    return Result(
        "_compute_form_archetype_match None propagation", Result.HALT,
        "; ".join(failures),
    )


def pin_compute_form_archetype_match_basic() -> Result:
    """Helper returns a 0-100 score when both vectors are present.

    Identical vectors -> L2=0 -> similarity=1.0 -> score=100.
    Distant vectors -> low similarity -> low score.
    """
    from score_batters import (
        _compute_form_archetype_match,
        FORM_ARCHETYPE_SIM_LO,
        FORM_ARCHETYPE_SIM_HI,
    )
    v = [0.350, 10.0, 11.0, 35.0, 4, 1, 0.270]
    same = _compute_form_archetype_match(v, v)
    if same is None or abs(same - 100.0) > 0.01:
        return Result(
            "_compute_form_archetype_match identical -> 100",
            Result.HALT,
            f"got {same}, want 100.0 (L2=0, sim=1.0 maps to 100 with anchors "
            f"{FORM_ARCHETYPE_SIM_LO}-{FORM_ARCHETYPE_SIM_HI})",
        )

    # Very distant vector -> similarity should be low
    distant = [0.0, 0.0, 0.0, 0.0, 100, 100, 0.0]
    distant_score = _compute_form_archetype_match(v, distant)
    if distant_score is None or distant_score >= 50.0:
        return Result(
            "_compute_form_archetype_match distant -> low score",
            Result.HALT,
            f"distant_score={distant_score}, expected < 50.0",
        )
    return Result(
        f"_compute_form_archetype_match basic (identical->100, distant->{distant_score:.1f})",
        Result.PASS,
    )


def pin_form_archetype_constants_present() -> Result:
    """Phase 1 constants exist with the documented values + shapes."""
    import features_v2 as fv2
    failures = []
    for name, want_type in (
        ("FORM_ARCHETYPE_FEATURES", list),
        ("FORM_ARCHETYPE_MIN_HRS", int),
        ("FORM_ARCHETYPE_LOOKBACK_SEASONS", int),
        ("FORM_ARCHETYPE_DEFAULT_WINDOW", int),
    ):
        if not hasattr(fv2, name):
            failures.append(f"missing {name}")
            continue
        val = getattr(fv2, name)
        if not isinstance(val, want_type):
            failures.append(f"{name} type {type(val).__name__}, want {want_type.__name__}")

    if hasattr(fv2, "FORM_ARCHETYPE_FEATURES"):
        feats = fv2.FORM_ARCHETYPE_FEATURES
        if len(feats) != 7:
            failures.append(f"FORM_ARCHETYPE_FEATURES has {len(feats)} elements, want 7")
        # Spot-check a few of the documented feature names
        for must in ("recent_xwoba_14d", "recent_swstr_pct_7d", "days_since_last_hr"):
            if must not in feats:
                failures.append(f"FORM_ARCHETYPE_FEATURES missing {must!r}")

    if hasattr(fv2, "FORM_ARCHETYPE_DEFAULT_WINDOW"):
        if fv2.FORM_ARCHETYPE_DEFAULT_WINDOW != 7:
            failures.append(
                f"FORM_ARCHETYPE_DEFAULT_WINDOW = {fv2.FORM_ARCHETYPE_DEFAULT_WINDOW}, "
                f"design doc specifies 7 (sweep dimension is window_days arg, not constant)"
            )

    if not hasattr(fv2, "compute_batter_form_archetype"):
        failures.append("compute_batter_form_archetype not exported")

    if not failures:
        return Result("Form-archetype Phase 1 constants present", Result.PASS)
    return Result(
        "Form-archetype constants", Result.HALT, "; ".join(failures),
    )


def pin_form_archetype_no_overlap_with_form_inputs() -> Result:
    """Archetype features MUST NOT overlap with score_form's base inputs.

    Load-bearing guardrail: if the archetype features overlap with the
    base Form mean inputs, the sub-signal would double-count the same
    underlying signal. See docs/form_archetype_design.md "Risk callout —
    feature non-overlap with score_form".

    Current score_form base inputs (post-B11): recent_hr_10g,
    recent_iso_30g, ev_trend. recent_avg_30g was dropped by B11 and is
    explicitly allowed back in the archetype as a state-descriptor.
    """
    from features_v2 import FORM_ARCHETYPE_FEATURES
    # Source of truth: the base inputs read by score_form (post-B11).
    SCORE_FORM_BASE_INPUTS = {"recent_hr_10g", "recent_iso_30g", "ev_trend"}
    overlap = SCORE_FORM_BASE_INPUTS & set(FORM_ARCHETYPE_FEATURES)
    if not overlap:
        return Result(
            "FORM_ARCHETYPE_FEATURES disjoint from score_form base inputs",
            Result.PASS,
            f"score_form: {sorted(SCORE_FORM_BASE_INPUTS)}; "
            f"archetype: {FORM_ARCHETYPE_FEATURES}",
        )
    return Result(
        "FORM_ARCHETYPE_FEATURES overlap with score_form",
        Result.HALT,
        f"overlap with base inputs: {sorted(overlap)} — double-counting risk. "
        "See docs/form_archetype_design.md non-overlap guardrail.",
    )


def pin_batter_form_archetype_table_exists() -> Result:
    """create_tables creates the batter_form_archetype table with the
    documented schema (composite PK on player_id, date_through, window_days).
    """
    import sqlite3
    from etl.db import create_tables
    conn = sqlite3.connect(":memory:")
    create_tables(conn)
    try:
        cols = conn.execute(
            "PRAGMA table_info(batter_form_archetype)"
        ).fetchall()
    finally:
        conn.close()
    if not cols:
        return Result(
            "batter_form_archetype table created",
            Result.HALT,
            "table not present after create_tables",
        )
    col_names = {c[1] for c in cols}
    pk_cols = {c[1] for c in cols if c[5]}  # c[5] is `pk` index (>0 = part of PK)
    failures = []
    for must in ("player_id", "date_through", "window_days",
                 "feature_centroid_json", "n_hrs_used", "fetched_at"):
        if must not in col_names:
            failures.append(f"missing column {must}")
    for must_pk in ("player_id", "date_through", "window_days"):
        if must_pk not in pk_cols:
            failures.append(f"{must_pk} not part of PRIMARY KEY")
    if not failures:
        return Result(
            "batter_form_archetype table + composite PK present",
            Result.PASS,
        )
    return Result(
        "batter_form_archetype schema", Result.HALT, "; ".join(failures),
    )


def pin_backtest_form_archetype_skeleton_imports() -> Result:
    """Phase 1 harness: backtest_form_archetype imports, exposes the
    full 3x3 sweep + the default variant, and has the documented
    structural helpers (fetch_rows, score_variants, compute_metrics, main).
    """
    try:
        from diagnostics import backtest_form_archetype as bfa
    except Exception as e:
        return Result(
            "backtest_form_archetype import", Result.HALT,
            f"failed: {type(e).__name__}: {e}",
        )
    failures = []
    for name in ("fetch_rows", "score_variants", "compute_metrics", "main",
                 "VARIANTS", "ARCHETYPE_SWEEP", "_phase1_guard"):
        if not hasattr(bfa, name):
            failures.append(f"missing {name}")
    # Sweep shape: 9 (window, min_hrs) combos
    if hasattr(bfa, "ARCHETYPE_SWEEP") and len(bfa.ARCHETYPE_SWEEP) != 9:
        failures.append(f"ARCHETYPE_SWEEP has {len(bfa.ARCHETYPE_SWEEP)} entries, want 9")
    # VARIANTS = default + 9 sweep = 10
    if hasattr(bfa, "VARIANTS") and len(bfa.VARIANTS) != 10:
        failures.append(f"VARIANTS has {len(bfa.VARIANTS)} entries, want 10 (default + 3x3)")
    # Spot-check key variant names
    if hasattr(bfa, "VARIANTS"):
        for must in ("default", "archetype_7d_10hr", "archetype_21d_20hr"):
            if must not in bfa.VARIANTS:
                failures.append(f"VARIANTS missing {must!r}")
    if not failures:
        return Result(
            "backtest_form_archetype skeleton: 3x3 sweep + default + helpers",
            Result.PASS,
        )
    return Result(
        "backtest_form_archetype skeleton", Result.HALT, "; ".join(failures),
    )


# ---------------------------------------------------------------------------
# 2026-05-26: Form archetype Phase 2 (backfill + harness wiring)
# ---------------------------------------------------------------------------

def pin_backfill_form_archetype_imports() -> Result:
    """Phase 2: etl/backfill_form_archetype imports and exposes the documented
    orchestrator surface (backfill_window, backfill_one_date_window, main,
    parse_duration, ALL_WINDOWS).
    """
    try:
        from etl import backfill_form_archetype as bfa
    except Exception as e:
        return Result(
            "etl/backfill_form_archetype import", Result.HALT,
            f"failed: {type(e).__name__}: {e}",
        )
    failures = []
    for name in ("backfill_window", "backfill_one_date_window", "main",
                 "parse_duration", "_resolve_windows", "ALL_WINDOWS",
                 "DEFAULT_START", "DEFAULT_END"):
        if not hasattr(bfa, name):
            failures.append(f"missing {name}")
    if hasattr(bfa, "ALL_WINDOWS"):
        if tuple(bfa.ALL_WINDOWS) != (7, 14, 21):
            failures.append(
                f"ALL_WINDOWS = {bfa.ALL_WINDOWS}, want (7, 14, 21) "
                "to match the 3x3 backtest sweep"
            )
    if hasattr(bfa, "DEFAULT_START"):
        if bfa.DEFAULT_START != "2025-03-27":
            failures.append(
                f"DEFAULT_START = {bfa.DEFAULT_START!r}, want '2025-03-27' "
                "(2025 regular season opener — matches etl/backfill_2025.py)"
            )
    if hasattr(bfa, "DEFAULT_END"):
        if bfa.DEFAULT_END != "2025-09-30":
            failures.append(
                f"DEFAULT_END = {bfa.DEFAULT_END!r}, want '2025-09-30'"
            )
    if not failures:
        return Result(
            "etl/backfill_form_archetype Phase 2 surface present",
            Result.PASS,
        )
    return Result(
        "etl/backfill_form_archetype Phase 2", Result.HALT,
        "; ".join(failures),
    )


def pin_backfill_form_archetype_cli_flags() -> Result:
    """Phase 2: the CLI accepts every flag the user asked for —
    --start / --end / --window-days / --max-dates / --max-runtime.

    Invokes the script as a subprocess with --help and confirms each flag
    appears in the output. Subprocess avoids monkey-patching argparse.
    """
    import subprocess
    repo_root = Path(__file__).resolve().parent.parent
    try:
        r = subprocess.run(
            [sys.executable, "-m", "etl.backfill_form_archetype", "--help"],
            cwd=str(repo_root),
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        return Result(
            "backfill_form_archetype --help", Result.HALT,
            f"subprocess failed: {type(e).__name__}: {e}",
        )
    out = (r.stdout or "") + (r.stderr or "")
    failures = []
    for flag in ("--start", "--end", "--window-days",
                 "--max-dates", "--max-runtime"):
        if flag not in out:
            failures.append(f"missing flag {flag}")
    # Spot-check parse_duration on a couple inputs.
    try:
        from etl.backfill_form_archetype import parse_duration
        if parse_duration("3h") != 3 * 3600:
            failures.append("parse_duration('3h') wrong")
        if parse_duration("1h30m") != 5400:
            failures.append("parse_duration('1h30m') wrong")
        if parse_duration(None) is not None:
            failures.append("parse_duration(None) should be None")
    except Exception as e:
        failures.append(f"parse_duration: {type(e).__name__}: {e}")
    if not failures:
        return Result(
            "backfill_form_archetype CLI flags + parse_duration",
            Result.PASS,
        )
    return Result(
        "backfill_form_archetype CLI", Result.HALT, "; ".join(failures),
    )


def pin_form_archetype_pick_inputs_columns() -> Result:
    """Phase 2 migration: pick_inputs has the 3 new form_archetype columns
    after create_tables runs. NULL-safe additive — populated by
    load_picks_to_db when generate_picks attaches the centroid.
    """
    import sqlite3
    from etl.db import create_tables
    conn = sqlite3.connect(":memory:")
    create_tables(conn)
    try:
        cols = {
            r[1]: r[2]  # column name -> type
            for r in conn.execute("PRAGMA table_info(pick_inputs)").fetchall()
        }
    finally:
        conn.close()
    failures = []
    want = {
        "form_archetype_centroid_json": "TEXT",
        "form_archetype_window":        "INTEGER",
        "form_archetype_n_hrs":         "INTEGER",
    }
    for name, want_type in want.items():
        if name not in cols:
            failures.append(f"pick_inputs missing column {name}")
            continue
        got_type = cols[name].upper()
        if want_type.upper() not in got_type:
            failures.append(
                f"pick_inputs.{name} type {got_type}, want {want_type}"
            )
    if not failures:
        return Result(
            "pick_inputs form_archetype_* columns present (Phase 2)",
            Result.PASS,
        )
    return Result(
        "pick_inputs Phase 2 columns", Result.HALT, "; ".join(failures),
    )


def pin_batter_form_archetype_idempotent() -> Result:
    """Phase 2 backfill: INSERT OR REPLACE on (player_id, date_through,
    window_days) means re-running with the same data yields the same rows.
    Synthetic insert / re-insert verifies the composite PK + idempotency.
    """
    import sqlite3
    import json as _json
    from etl.db import create_tables

    conn = sqlite3.connect(":memory:")
    create_tables(conn)
    try:
        ins = """
            INSERT OR REPLACE INTO batter_form_archetype
                (player_id, date_through, window_days,
                 feature_centroid_json, n_hrs_used)
            VALUES (?, ?, ?, ?, ?)
        """
        centroid1 = _json.dumps([0.35, 8.5, 10.2, 38.0, 5, 2, 0.260])
        centroid2 = _json.dumps([0.40, 9.0, 10.5, 40.0, 6, 2, 0.270])

        # First insert
        conn.execute(ins, (12345, "2025-06-01", 7, centroid1, 12))
        # Replay with same key, different payload — must REPLACE, not duplicate
        conn.execute(ins, (12345, "2025-06-01", 7, centroid2, 13))
        conn.commit()

        rows = conn.execute(
            """
            SELECT feature_centroid_json, n_hrs_used
            FROM batter_form_archetype
            WHERE player_id = ? AND date_through = ? AND window_days = ?
            """,
            (12345, "2025-06-01", 7),
        ).fetchall()

        # Different windows should NOT collide
        conn.execute(ins, (12345, "2025-06-01", 14, centroid1, 12))
        conn.execute(ins, (12345, "2025-06-01", 21, centroid1, 12))
        conn.commit()
        n_per_batter_date = conn.execute(
            "SELECT COUNT(*) FROM batter_form_archetype "
            "WHERE player_id = ? AND date_through = ?",
            (12345, "2025-06-01"),
        ).fetchone()[0]
    finally:
        conn.close()

    failures = []
    if len(rows) != 1:
        failures.append(f"got {len(rows)} rows for same PK, want 1 (replace)")
    elif rows[0][1] != 13:
        failures.append(
            f"n_hrs_used = {rows[0][1]}, want 13 (the REPLACE value)"
        )
    if n_per_batter_date != 3:
        failures.append(
            f"3 windows for one (batter, date) -> {n_per_batter_date} rows, "
            "want 3 (different window_days must NOT collide on PK)"
        )
    if not failures:
        return Result(
            "batter_form_archetype INSERT OR REPLACE idempotent on PK",
            Result.PASS,
        )
    return Result(
        "batter_form_archetype idempotency", Result.HALT,
        "; ".join(failures),
    )


def pin_backtest_form_archetype_runs_against_sample_db() -> Result:
    """Phase 2 harness: against an in-memory DB with synthetic centroids +
    pick_inputs rows, the backtest harness produces the full 10-row variant
    grid (default + 9 archetype variants) without crashing.

    This is a wiring smoke test — the *numbers* on the grid are random
    on synthetic data, but the SHAPE (10 rows, every variant has n>0)
    is the load-bearing thing.
    """
    import sqlite3
    import json as _json
    from datetime import datetime, timedelta
    from etl.db import create_tables
    from diagnostics import backtest_form_archetype as bfa

    conn = sqlite3.connect(":memory:")
    create_tables(conn)

    # Synthetic data: 5 dates, 30 batters per date, 7 batters HR per date.
    # Centroids exist for 25 of 30 batters at each (date, window).
    try:
        import random
        random.seed(42)
        dates = [f"2025-06-0{i}" for i in range(1, 6)]  # 5 dates
        batter_ids = list(range(50001, 50031))  # 30 batters
        n_hr_per_date = 7

        for date_str in dates:
            hr_batters = set(random.sample(batter_ids, n_hr_per_date))
            for bid in batter_ids:
                conn.execute(
                    """
                    INSERT INTO pick_inputs (date, batter_id,
                        recent_hr_10g, recent_iso_30g, ev_trend)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (date_str, bid,
                     random.uniform(0.0, 3.0),
                     random.uniform(0.100, 0.250),
                     random.uniform(-2.0, 2.0)),
                )
                if bid in hr_batters:
                    conn.execute(
                        """
                        INSERT INTO outcomes
                            (date, batter_id, game_pk, hr_count)
                        VALUES (?, ?, ?, ?)
                        """,
                        (date_str, bid, 99999, 1),
                    )

        # Centroids on the prior date for 25/30 batters, each window.
        for date_str in dates:
            d = datetime.strptime(date_str, "%Y-%m-%d")
            dt = (d - timedelta(days=1)).strftime("%Y-%m-%d")
            for bid in batter_ids[:25]:  # 25 of 30
                centroid = [
                    round(random.uniform(0.300, 0.400), 3),
                    round(random.uniform(5.0, 15.0), 2),
                    round(random.uniform(8.0, 14.0), 2),
                    round(random.uniform(30.0, 45.0), 2),
                    random.randint(2, 8),
                    random.randint(1, 4),
                    round(random.uniform(0.220, 0.300), 3),
                ]
                for w in (7, 14, 21):
                    conn.execute(
                        """
                        INSERT INTO batter_form_archetype
                            (player_id, date_through, window_days,
                             feature_centroid_json, n_hrs_used)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (bid, dt, w,
                         _json.dumps(centroid),
                         random.randint(5, 25)),
                    )
        conn.commit()

        # Run the harness end-to-end against this in-memory DB.
        rows = bfa.fetch_rows(conn, dates[0], dates[-1])
        if len(rows) == 0:
            return Result(
                "backtest_form_archetype runs against sample DB",
                Result.HALT,
                "fetch_rows returned 0 rows from sample DB",
            )

        centroids_by_window = {
            w: bfa._load_window_centroids(conn, dates[0], dates[-1], w)
            for w in (7, 14, 21)
        }

        scored = bfa.score_variants(rows, centroids_by_window)
        common = [s for s in scored if s["has_hr10"]]
        results = {v: bfa.compute_metrics(common, v) for v in bfa.VARIANTS}
    finally:
        conn.close()

    failures = []
    # 10 variant rows expected: default + 9 sweep entries
    if len(results) != 10:
        failures.append(
            f"got {len(results)} variants, want 10 (default + 3x3 sweep)"
        )
    for v in bfa.VARIANTS:
        if v not in results:
            failures.append(f"missing variant {v}")
            continue
        if results[v]["n"] == 0:
            failures.append(f"variant {v} has n=0")
    # Every archetype variant should have at least SOME rows with the
    # sub-signal active (otherwise wiring is broken).
    archetype_variants = [v for v in bfa.VARIANTS if v != "default"]
    if all(results[v]["n_archetype_active"] == 0 for v in archetype_variants):
        failures.append(
            "all archetype variants have 0 active rows — centroid join "
            "didn't fire (Phase 2 wiring broken)"
        )

    if not failures:
        return Result(
            "backtest_form_archetype Phase 2 wiring (10 variants, "
            f"n>0 on every row, archetype active on >=1 variant)",
            Result.PASS,
        )
    return Result(
        "backtest_form_archetype Phase 2 wiring",
        Result.HALT, "; ".join(failures),
    )


def pin_compute_batter_form_archetype_bulk_pull_at_most_once() -> Result:
    """2026-05-26: compute_batter_form_archetype MUST NOT call
    pybaseball.statcast_batter() at all, and MUST call pybaseball.statcast()
    at most ONCE per invocation regardless of the (batter, HR) count.

    Catastrophic regression guard. The 2026-05-26 bug ran 12,000 API calls
    per (date, window) by calling statcast_batter() inside a per-HR loop —
    222 centroids written in 4h19m, full backfill extrapolated to ~94 days.
    The fix is to bulk-pull ONCE and slice in-memory. This pin nails that
    contract: monkey-patch both API entry points, ensure statcast() is
    called <=1 time and statcast_batter() is NEVER called.

    Uses a synthetic in-memory DB so the test runs even when DB_PATH is
    absent / empty.
    """
    import sqlite3
    import sys as _sys
    import pandas as pd
    from datetime import datetime, timedelta
    from etl.db import create_tables

    failures: list[str] = []

    # Build a temp DB with batter_hr_events for a handful of synthetic pids,
    # enough HRs to clear MIN_HRS. We monkey-patch DB_PATH so the builder
    # reads from our DB.
    import tempfile, os
    tmp_dir = tempfile.mkdtemp(prefix="form_arch_test_")
    tmp_db = Path(tmp_dir) / "test.db"
    conn = sqlite3.connect(str(tmp_db))
    try:
        create_tables(conn)
        # 3 batters * 15 HRs each -> all clear FORM_ARCHETYPE_MIN_HRS=10
        as_of = "2025-09-01"
        as_of_dt = datetime.strptime(as_of, "%Y-%m-%d")
        pids = [60001, 60002, 60003]
        for pid in pids:
            for i in range(15):
                # HRs spread across the season prior to as_of_date
                hr_date = (as_of_dt - timedelta(days=10 + i * 7)).strftime("%Y-%m-%d")
                conn.execute(
                    """
                    INSERT INTO batter_hr_events
                        (batter_id, pitcher_id, game_date, game_pk, pitch_type)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (pid, 700000 + i, hr_date, 999000 + i, "FF"),
                )
        conn.commit()
    finally:
        conn.close()

    # Build a synthetic Statcast frame covering the lookback span and the
    # three batters. ~30 pitch-level rows per batter per day for one of
    # their HR windows is enough for _per_hr_state_snapshot to succeed.
    rows = []
    for pid in pids:
        for i in range(15):
            window_end = (as_of_dt - timedelta(days=10 + i * 7 + 1))
            for d in range(7):
                gd = (window_end - timedelta(days=d)).strftime("%Y-%m-%d")
                for k in range(8):
                    rows.append({
                        "batter": pid,
                        "game_date": gd,
                        "events": "single" if k == 0 else (
                            "field_out" if k == 1 else None
                        ),
                        "estimated_woba_using_speedangle": 0.42,
                        "launch_speed_angle": 6.0 if k == 2 else None,
                        "description": "swinging_strike" if k == 3 else "ball",
                        "hc_x": 100.0 if k == 4 else None,
                        "stand": "R",
                        "bb_type": "fly_ball" if k in (0, 2, 4) else None,
                    })
    synthetic_df = pd.DataFrame(rows)

    # Monkey-patch pybaseball entry points.
    import pybaseball  # noqa: F401
    n_statcast_calls = {"count": 0}
    n_statcast_batter_calls = {"count": 0}

    def fake_statcast(*args, **kwargs):
        n_statcast_calls["count"] += 1
        return synthetic_df

    def fake_statcast_batter(*args, **kwargs):
        n_statcast_batter_calls["count"] += 1
        # Even if accidentally called, return the per-batter slice so the
        # test doesn't blow up before the assertion fires.
        if args and len(args) >= 3:
            return synthetic_df[synthetic_df["batter"] == args[2]]
        return synthetic_df

    import features_v2 as _fv2
    import etl.db as _etldb

    orig_statcast = getattr(pybaseball, "statcast", None)
    orig_statcast_batter = getattr(pybaseball, "statcast_batter", None)
    orig_db_path = _etldb.DB_PATH

    try:
        pybaseball.statcast = fake_statcast
        pybaseball.statcast_batter = fake_statcast_batter
        # Also patch the etl.db DB_PATH the builder reads from.
        _etldb.DB_PATH = tmp_db

        # Disable the on-disk bulk cache for this test by setting TTL to 0
        # — we want to verify a FRESH invocation calls statcast exactly once.
        orig_ttl = _fv2.TTL_FORM_ARCHETYPE_BULK
        _fv2.TTL_FORM_ARCHETYPE_BULK = 0

        try:
            result = _fv2.compute_batter_form_archetype(
                player_ids=pids,
                as_of_date=as_of,
                window_days=7,
            )
        finally:
            _fv2.TTL_FORM_ARCHETYPE_BULK = orig_ttl

        # 1. statcast called at most ONCE (the bulk pull). It may be 0 if
        # the on-disk cache hit unexpectedly; both 0 and 1 are acceptable
        # (the contract is "no more than once").
        if n_statcast_calls["count"] > 1:
            failures.append(
                f"statcast() called {n_statcast_calls['count']} times, "
                "want <=1 (regression to per-batter API spam)"
            )
        # 2. statcast_batter MUST NEVER be called.
        if n_statcast_batter_calls["count"] != 0:
            failures.append(
                f"statcast_batter() called "
                f"{n_statcast_batter_calls['count']} times — "
                "this is the 2026-05-26 bug; should be 0"
            )
        # 3. The return shape is the same as the legacy implementation.
        if not isinstance(result, dict):
            failures.append(f"return type {type(result).__name__}, want dict")
        elif set(result.keys()) != set(pids):
            failures.append(
                f"return keys {sorted(result.keys())}, want {sorted(pids)} "
                "(must cover every requested pid)"
            )
        else:
            # All three test batters had 15 HRs each above the MIN_HRS gate;
            # at least one should produce a centroid (the synthetic frame
            # covers their windows).
            n_with_centroid = sum(
                1 for v in result.values()
                if v is not None and v.get("feature_centroid") is not None
            )
            if n_with_centroid == 0:
                failures.append(
                    "no batters returned a centroid — synthetic frame slicing "
                    "broke (in-memory groupby/slice path is wired wrong)"
                )
    finally:
        # Restore.
        if orig_statcast is not None:
            pybaseball.statcast = orig_statcast
        if orig_statcast_batter is not None:
            pybaseball.statcast_batter = orig_statcast_batter
        _etldb.DB_PATH = orig_db_path
        try:
            tmp_db.unlink()
            os.rmdir(tmp_dir)
        except Exception:
            pass

    if not failures:
        return Result(
            f"compute_batter_form_archetype bulk pull (statcast called "
            f"{n_statcast_calls['count']}x, statcast_batter "
            f"{n_statcast_batter_calls['count']}x)",
            Result.PASS,
        )
    return Result(
        "compute_batter_form_archetype bulk pull contract",
        Result.HALT,
        "; ".join(failures),
    )


def pin_backfill_form_archetype_reset_flag() -> Result:
    """2026-05-26: --reset CLI flag wipes batter_form_archetype rows in the
    backfill window before populating. Lets the user clear the partial
    bad-data from the 12,000-API-call bug without dropping the whole table.

    Verifies: (1) the flag is present in --help, (2) _reset_centroids()
    deletes rows in range and leaves out-of-range rows intact.
    """
    import subprocess
    import json as _json
    import sqlite3
    from etl.db import create_tables
    from etl.backfill_form_archetype import _reset_centroids

    failures: list[str] = []

    # --- Surface check ---
    repo_root = Path(__file__).resolve().parent.parent
    try:
        r = subprocess.run(
            [sys.executable, "-m", "etl.backfill_form_archetype", "--help"],
            cwd=str(repo_root),
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        return Result(
            "backfill_form_archetype --reset flag", Result.HALT,
            f"subprocess failed: {type(e).__name__}: {e}",
        )
    if "--reset" not in (r.stdout or "") + (r.stderr or ""):
        failures.append("--reset flag missing from --help output")

    # --- Behavior check ---
    conn = sqlite3.connect(":memory:")
    create_tables(conn)
    try:
        ins = """
            INSERT OR REPLACE INTO batter_form_archetype
                (player_id, date_through, window_days,
                 feature_centroid_json, n_hrs_used)
            VALUES (?, ?, ?, ?, ?)
        """
        for date_str in ("2025-05-01", "2025-06-01", "2025-07-01", "2025-08-01"):
            for w in (7, 14, 21):
                conn.execute(ins, (60099, date_str, w,
                                   _json.dumps([0.4] * 7), 12))
        conn.commit()
        n_before = conn.execute(
            "SELECT COUNT(*) FROM batter_form_archetype"
        ).fetchone()[0]
        if n_before != 12:
            failures.append(f"pre-reset row count {n_before}, want 12")

        # Wipe only the June-July range.
        n_deleted = _reset_centroids(conn, "2025-06-01", "2025-07-31")
        if n_deleted != 6:  # 2 dates * 3 windows
            failures.append(
                f"reset deleted {n_deleted} rows, want 6 (2 dates x 3 windows)"
            )

        # Bookend rows must survive.
        survivors = conn.execute(
            "SELECT DISTINCT date_through FROM batter_form_archetype "
            "ORDER BY date_through"
        ).fetchall()
        survivor_dates = [r[0] for r in survivors]
        if "2025-05-01" not in survivor_dates:
            failures.append("reset wiped out-of-range date 2025-05-01")
        if "2025-08-01" not in survivor_dates:
            failures.append("reset wiped out-of-range date 2025-08-01")
        if "2025-06-01" in survivor_dates:
            failures.append("reset did NOT wipe in-range date 2025-06-01")
    finally:
        conn.close()

    if not failures:
        return Result(
            "backfill_form_archetype --reset flag + _reset_centroids()",
            Result.PASS,
        )
    return Result(
        "backfill_form_archetype --reset", Result.HALT,
        "; ".join(failures),
    )


def pin_get_db_resolution_and_fail_loud() -> Result:
    """B24 (2026-06-01): etl.db DB resolution + get_db() fail-loud contract.

    Three behaviors, all guarding against the stray-DB divergence that
    spawned three copies of hr_bets.db (canonical, `.claude\\worktrees\\data\\`,
    in-repo) and kept B16's slate_pct backfill off the canonical DB:

      1. get_db() with NO arg (canonical default) must FAIL LOUD with
         FileNotFoundError when the canonical DB is absent — never silently
         mkdir+create a stray empty DB the pipeline then writes picks into.
         Also asserts no file was created as a side effect.
      2. get_db(explicit_path) must PRESERVE create-on-demand (bootstrap /
         --db throwaway DBs depend on it).
      3. DB_PATH must honor the HR_BETS_DB env override at import time, from
         any cwd. Verified hermetically in a subprocess so we never mutate
         this process's already-imported etl.db.

    HALT severity: a regression here silently re-diverges the canonical DB.
    """
    import os
    import shutil
    import subprocess
    import tempfile
    import etl.db as _etldb

    failures: list[str] = []
    repo_root = str(Path(__file__).resolve().parent.parent)

    # --- 1 & 2: fail-loud on default, preserve-create on explicit ---------
    tmp_dir = Path(tempfile.mkdtemp(prefix="b24_getdb_"))
    missing_default = tmp_dir / "nope" / "canonical.db"   # parent absent too
    explicit_new = tmp_dir / "explicit_created.db"
    orig_db_path = _etldb.DB_PATH
    try:
        _etldb.DB_PATH = missing_default
        # 1. default path, file absent -> FileNotFoundError, no file created.
        try:
            conn = _etldb.get_db()
            conn.close()
            failures.append(
                "get_db() did NOT raise on a missing canonical DB "
                "(silent stray-DB creation regressed)"
            )
        except FileNotFoundError:
            pass  # expected
        except Exception as e:
            failures.append(
                f"get_db() raised {type(e).__name__}, want FileNotFoundError"
            )
        if missing_default.exists():
            failures.append(
                "get_db() created the DB file despite it being the "
                "fail-loud default path"
            )
        # 2. explicit path, file absent -> created + usable (preserve).
        try:
            conn = _etldb.get_db(explicit_new)
            conn.execute("CREATE TABLE IF NOT EXISTS _t (x INTEGER)")
            conn.close()
            if not explicit_new.exists():
                failures.append(
                    "get_db(explicit_path) did not create the DB file "
                    "(bootstrap/--db path regressed)"
                )
        except Exception as e:
            failures.append(
                f"get_db(explicit_path) raised {type(e).__name__}: {e} "
                "(must preserve create-on-demand)"
            )
    finally:
        _etldb.DB_PATH = orig_db_path
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # --- 3: HR_BETS_DB env override resolves DB_PATH (hermetic subprocess) -
    sentinel = str(Path(tempfile.gettempdir()) / "b24_env_sentinel" / "hr_bets.db")
    child_env = dict(os.environ)
    child_env["HR_BETS_DB"] = sentinel
    try:
        proc = subprocess.run(
            [sys.executable, "-c", "from etl.db import DB_PATH; print(DB_PATH)"],
            capture_output=True, text=True, cwd=repo_root, env=child_env,
            timeout=60,
        )
        if proc.returncode != 0:
            failures.append(
                f"env-override subprocess failed (rc={proc.returncode}): "
                f"{(proc.stderr or '').strip()[:160]}"
            )
        elif Path(proc.stdout.strip()) != Path(sentinel):
            failures.append(
                f"DB_PATH with HR_BETS_DB set = {proc.stdout.strip()!r}, "
                f"want {sentinel!r} (env override not honored)"
            )
    except Exception as e:
        failures.append(
            f"env-override subprocess check crashed: {type(e).__name__}: {e}"
        )

    if failures:
        return Result("get_db resolution + fail-loud (B24)", Result.HALT,
                      "; ".join(failures))
    return Result(
        "get_db resolution + fail-loud (B24)", Result.PASS,
        "fail-loud on missing default, create on explicit, HR_BETS_DB honored",
    )


PIN_TESTS: list[Callable[[], Result]] = [
    pin_score_power_empty,
    pin_score_power_all_zero,
    pin_score_power_elite,
    # 2026-05-27: B17 anchor recalibration pins
    pin_score_power_barrel_anchors,
    pin_score_power_hr_fb_anchors,
    pin_score_power_iso_anchors,
    pin_score_power_xwoba_anchors,
    pin_score_power_recent_xwoba_anchors,
    pin_score_lineup_position_table,
    pin_score_matchup_no_data,
    pin_platoon_dampener_table,
    pin_score_matchup_rookie_bonus,
    pin_compute_slate_context_empty,
    pin_compute_slate_context_skip_missing_pitcher,
    pin_compute_slate_context_two_signal_pitcher,
    pin_shrink_to_career_basic,
    pin_shrink_to_career_no_career_pass_through,
    pin_shrink_to_career_huge_sample_barely_shrinks,
    pin_use_career_prior_default_off,
    pin_compute_season_hr_floor_tiers,
    pin_compute_season_hr_floor_smooth,
    pin_use_smooth_hr_floor_default_off,
    pin_use_recent_statcast_blend_default_off,
    pin_score_power_recent_blend_flag_off_no_op,
    pin_score_power_recent_blend_flag_on_lifts_score,
    pin_use_season_hr_floor_default_on,
    pin_score_power_floor_lifts_low_score,
    pin_score_power_floor_does_not_pull_down,
    # 2026-05-13: pitcher recency blend
    pin_effective_hr9_season_only_when_no_recent,
    pin_effective_hr9_blend_when_enough_starts,
    pin_effective_hr9_below_min_starts_falls_back,
    pin_score_pitcher_vulnerability_recency_lifts_score,
    # 2026-05-21: B6a recent quality-contact aggregation
    pin_aggregate_recent_statcast_basic,
    pin_aggregate_recent_statcast_empty,
    pin_aggregate_recent_statcast_thin_sample_dropped,
    # 2026-05-21: B4 — recency extended to ERA + K/9, configurable window
    pin_effective_era_blend,
    pin_effective_k9_blend,
    pin_effective_blends_custom_weight,
    pin_effective_blend_min_starts_gate,
    pin_pitcher_recent_window_defaults,
    pin_score_pitcher_vulnerability_era_recency_lifts_score,
    # 2026-05-21: PR 3 — as-of-date infrastructure
    pin_filter_before_drops_after_date,
    pin_filter_before_none_is_noop,
    pin_filter_before_empty_safe,
    pin_as_of_date_signatures_present,
    pin_weather_archive_threshold_present,
    # 2026-05-21: PR 4 — 2025 backfill orchestrator
    pin_backfill_orchestrator_imports,
    pin_backfill_date_range_complete,
    pin_backfill_default_window_full_season,
    pin_generate_card_accepts_as_of_date,
    pin_fetch_live_slate_accepts_as_of_date,
    pin_form_fetch_as_of_date_threaded,
    pin_get_recent_game_log_filters_before_date,
    pin_backfill_parse_duration,
    pin_backfill_window_accepts_chunk_flags,
    pin_run_backfill_local_wrapper_present,
    # 2026-05-22: DB-backed victim/arsenal backfill path
    pin_aggregate_victim_profile_weighted,
    pin_aggregate_victim_profile_no_arsenal_fallback,
    # 2026-05-22: B6 power input-source backtest harness
    pin_backtest_power_inputs_isolates_variants,
    # 2026-05-23: weather resilience (archive cache + broader retry)
    pin_weather_archive_cache_roundtrip,
    pin_weather_retry_config,
    # 2026-05-23: Form anchor + weighting backtest harness
    pin_backtest_form_anchors_variants_isolate,
    # 2026-05-25: B7 — IL / scratch filter
    pin_b7_daily_player_status_table_exists,
    pin_b7_fetch_team_roster_status_parses_payload,
    pin_b7_generate_card_filter_skips_il_row,
    pin_b7_etl_step_2_5_idempotent,
    pin_b7_load_player_status_lookup_signature,
    # 2026-05-25: Phase 1 — pitch-type archetype matchup sub-signal scaffolding
    pin_batter_pitch_type_splits_table_exists,
    pin_use_arsenal_subsignal_default_off,
    pin_score_matchup_arsenal_flag_off_no_op,
    pin_compute_xslg_vs_arsenal_basic,
    pin_compute_xslg_vs_arsenal_short_sample_returns_none,
    pin_compute_xslg_vs_arsenal_missing_pitcher_arsenal,
    pin_fetch_batter_pitch_type_splits_signature,
    pin_pitch_type_split_min_bb_constant,
    pin_backtest_arsenal_inputs_skeleton_imports,
    # 2026-05-25: Phase 1 — park archetype sub-signal scaffolding
    pin_batter_park_archetype_table_exists,
    pin_use_park_archetype_flag_default_off,
    # 2026-05-27: B16 — slate-pct kwargs + columns + backfill
    pin_score_park_kwarg_none_safe,
    pin_score_weather_kwarg_none_safe,
    pin_score_matchup_kwarg_none_safe,
    pin_slate_pct_columns_exist,
    pin_backfill_slate_pct_idempotent,
    pin_score_park_archetype_flag_off_no_op,
    pin_compute_park_archetype_match_none_passes_through,
    pin_compute_park_archetype_match_basic,
    pin_compute_batter_park_archetype_below_threshold_returns_none,
    pin_park_archetype_constants,
    pin_backtest_park_archetype_skeleton_imports,
    # 2026-05-26: Form archetype Phase 1 (sub-signal scaffolding)
    pin_use_form_archetype_default_off,
    pin_score_form_archetype_flag_off_no_op,
    pin_compute_form_archetype_match_returns_none_on_missing,
    pin_compute_form_archetype_match_basic,
    pin_form_archetype_constants_present,
    pin_form_archetype_no_overlap_with_form_inputs,
    pin_batter_form_archetype_table_exists,
    pin_backtest_form_archetype_skeleton_imports,
    # 2026-05-25: Pitch-type Phase 2 — real builder + 2025 backfill + harness wiring
    pin_aggregate_pitch_type_splits_basic,
    pin_aggregate_pitch_type_splits_empty,
    pin_fetch_batter_pitch_type_splits_empty_ids,
    pin_pick_inputs_phase2_columns_exist,
    pin_batter_pitch_type_splits_idempotent_write,
    pin_backfill_pitch_type_splits_imports,
    pin_load_picks_persists_pitch_type_splits,
    pin_load_pitch_type_splits_lookup_empty,
    pin_backtest_arsenal_inputs_score_variants,
    # 2026-05-26: Form archetype Phase 2 (backfill + harness wiring)
    pin_backfill_form_archetype_imports,
    pin_backfill_form_archetype_cli_flags,
    pin_form_archetype_pick_inputs_columns,
    pin_batter_form_archetype_idempotent,
    pin_backtest_form_archetype_runs_against_sample_db,
    # 2026-05-26: URGENT — bulk Statcast pull (no per-batter API spam)
    pin_compute_batter_form_archetype_bulk_pull_at_most_once,
    pin_backfill_form_archetype_reset_flag,
    # 2026-05-25: Phase 2 — park archetype backfill + harness wiring
    pin_backfill_park_archetype_cli_present,
    pin_backfill_park_archetype_idempotent,
    pin_pick_inputs_park_archetype_columns_exist,
    pin_backtest_park_archetype_runs_against_populated_db,
    # 2026-06-01: B24 — canonical DB anchor + fail-loud on the default path
    pin_get_db_resolution_and_fail_loud,
]


# ---------------------------------------------------------------------------
# DB sanity probes — run against actual SQLite if present
# ---------------------------------------------------------------------------

def _db_path() -> Optional[Path]:
    p = Path(__file__).resolve().parent.parent.parent / "data" / "hr_bets.db"
    return p if p.exists() else None


def db_lineup_batting_order_capped() -> Result:
    """daily_lineup must never have batting_order > 9 (HIGH #1 fix)."""
    db = _db_path()
    if not db:
        return Result(
            "daily_lineup.batting_order <= 9 (DB missing — skipped)",
            Result.INFO, str(db),
        )
    conn = sqlite3.connect(str(db))
    n = conn.execute(
        "SELECT COUNT(*) FROM daily_lineup WHERE batting_order > 9"
    ).fetchone()[0]
    conn.close()
    if n == 0:
        return Result("daily_lineup.batting_order <= 9", Result.PASS, "0 rows out-of-range")
    return Result(
        "daily_lineup.batting_order <= 9",
        Result.HALT,
        f"{n} rows have batting_order > 9 — HIGH #1 fix may have regressed",
    )


def db_pitcher_league_mean_count() -> Result:
    """Count pick_inputs rows with the exact league-mean signature.

    A real fraction (>5%) suggests the pitcher fetch is silently failing
    and rows are landing with the league-mean fallback. With audit HIGH #3
    fixed (skip-on-missing), this number should drop sharply.
    """
    db = _db_path()
    if not db:
        return Result(
            "pick_inputs league-mean rows (DB missing — skipped)",
            Result.INFO, str(db),
        )
    conn = sqlite3.connect(str(db))
    try:
        # Column names vary across schema versions; probe defensively.
        n_total = conn.execute(
            "SELECT COUNT(*) FROM pick_inputs WHERE date >= date('now', '-30 days')"
        ).fetchone()[0]
        n_lm = conn.execute("""
            SELECT COUNT(*) FROM pick_inputs
            WHERE date >= date('now', '-30 days')
              AND pitcher_hr_per_9 = 1.2
              AND pitcher_hh_pct = 35
        """).fetchone()[0]
    except sqlite3.OperationalError as e:
        return Result(
            "pick_inputs league-mean rows", Result.INFO,
            f"schema mismatch: {e}",
        )
    finally:
        conn.close()
    if n_total == 0:
        return Result("pick_inputs league-mean rows", Result.INFO, "no recent rows")
    pct = n_lm / n_total * 100
    if pct < 5:
        return Result(
            "pick_inputs league-mean rate < 5%",
            Result.PASS,
            f"{n_lm}/{n_total} ({pct:.1f}%)",
        )
    return Result(
        "pick_inputs league-mean rate < 5%",
        Result.WARN,
        f"{n_lm}/{n_total} ({pct:.1f}%) — pitcher fetch may be silently failing",
    )


def db_weather_fallback_check() -> Result:
    """Flag daily_slate rows that match the etl_morning fallback signature exactly."""
    db = _db_path()
    if not db:
        return Result(
            "daily_slate weather fallback (DB missing — skipped)",
            Result.INFO, str(db),
        )
    conn = sqlite3.connect(str(db))
    try:
        n = conn.execute("""
            SELECT COUNT(*) FROM daily_slate
            WHERE date >= date('now', '-7 days')
              AND temperature_f = 68 AND wind_mph = 5
              AND wind_dir_deg = 0 AND COALESCE(dome, 0) = 0
        """).fetchone()[0]
    except sqlite3.OperationalError as e:
        return Result(
            "daily_slate weather fallback", Result.INFO,
            f"schema mismatch: {e}",
        )
    finally:
        conn.close()
    if n == 0:
        return Result("daily_slate weather fallback (last 7d)", Result.PASS, "0 fallback rows")
    return Result(
        "daily_slate weather fallback (last 7d)",
        Result.WARN,
        f"{n} games match the (68, 5, 0, dome=0) fallback — Open-Meteo may have failed for those",
    )


def db_daily_picks_starter_coverage() -> Result:
    """Most recent daily_picks date should have meaningful starter coverage.

    Checks: do we have at least 9 confirmed starters per game in the slate?
    A persistent gap (e.g., 5 of 9 missing for a game) suggests HIGH #2
    fix regressed.
    """
    db = _db_path()
    if not db:
        return Result(
            "daily_picks starter coverage (DB missing — skipped)",
            Result.INFO, str(db),
        )
    conn = sqlite3.connect(str(db))
    try:
        latest = conn.execute("SELECT MAX(date) FROM daily_picks").fetchone()[0]
        if not latest:
            return Result("daily_picks starter coverage", Result.INFO, "no daily_picks rows")
        # Per game on the latest date, count distinct starters
        rows = conn.execute("""
            SELECT game_pk, COUNT(DISTINCT batter_id) AS n_starters
            FROM daily_picks
            WHERE date = ?
              AND batting_order BETWEEN 1 AND 9
            GROUP BY game_pk
        """, (latest,)).fetchall()
    except sqlite3.OperationalError as e:
        return Result(
            "daily_picks starter coverage", Result.INFO,
            f"schema mismatch: {e}",
        )
    finally:
        conn.close()
    if not rows:
        return Result("daily_picks starter coverage", Result.INFO, "no games on latest date")
    short_games = [(gpk, n) for gpk, n in rows if n < 9]
    if not short_games:
        return Result(
            f"daily_picks starter coverage ({latest})",
            Result.PASS,
            f"{len(rows)} games, all >= 9 starters",
        )
    return Result(
        f"daily_picks starter coverage ({latest})",
        Result.WARN,
        f"{len(short_games)}/{len(rows)} games have <9 starters: "
        + ", ".join(f"gpk={g} n={n}" for g, n in short_games[:5]),
    )


DB_PROBES: list[Callable[[], Result]] = [
    db_lineup_batting_order_capped,
    db_pitcher_league_mean_count,
    db_weather_fallback_check,
    db_daily_picks_starter_coverage,
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_section(name: str, checks: list[Callable[[], Result]]) -> list[Result]:
    print(f"\n{BOLD}{name}{RESET}")
    print(f"  {DIM}{'-' * 68}{RESET}")
    results = []
    for check in checks:
        try:
            r = check()
        except Exception as e:
            r = Result(check.__name__, Result.HALT, f"crashed: {type(e).__name__}: {e}")
        print(r)
        results.append(r)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke tests + DB sanity probes for MLB HR Bets")
    parser.add_argument("--pin-only", action="store_true", help="Skip DB checks")
    parser.add_argument("--db-only", action="store_true", help="Skip pin tests")
    parser.add_argument("--strict", action="store_true", help="WARNs cause non-zero exit too")
    args = parser.parse_args()

    print(f"{BOLD}MLB HR Bets — smoke runner{RESET}")
    print(f"  {DIM}Pin tests + DB sanity probes for the daily pipeline.{RESET}")

    all_results = []
    if not args.db_only:
        all_results += run_section("Pin tests (scoring functions)", PIN_TESTS)
    if not args.pin_only:
        all_results += run_section("DB sanity probes", DB_PROBES)

    halts = [r for r in all_results if r.status == Result.HALT]
    warns = [r for r in all_results if r.status == Result.WARN]
    passes = [r for r in all_results if r.status == Result.PASS]
    infos = [r for r in all_results if r.status == Result.INFO]

    print()
    print(f"  {BOLD}Summary:{RESET}  "
          f"{GREEN}{len(passes)} PASS{RESET}  "
          f"{YELLOW}{len(warns)} WARN{RESET}  "
          f"{RED}{len(halts)} HALT{RESET}  "
          f"{DIM}{len(infos)} INFO{RESET}")
    print()

    if halts:
        return 2
    if args.strict and warns:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
