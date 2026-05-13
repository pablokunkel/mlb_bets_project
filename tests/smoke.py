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
    pin_use_season_hr_floor_default_on,
    pin_score_power_floor_lifts_low_score,
    pin_score_power_floor_does_not_pull_down,
    # 2026-05-13: pitcher recency blend
    pin_effective_hr9_season_only_when_no_recent,
    pin_effective_hr9_blend_when_enough_starts,
    pin_effective_hr9_below_min_starts_falls_back,
    pin_score_pitcher_vulnerability_recency_lifts_score,
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
