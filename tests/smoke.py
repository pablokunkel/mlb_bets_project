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


PIN_TESTS: list[Callable[[], Result]] = [
    pin_score_power_empty,
    pin_score_power_all_zero,
    pin_score_power_elite,
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
    pin_backfill_parse_duration,
    pin_backfill_window_accepts_chunk_flags,
    pin_run_backfill_local_wrapper_present,
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
