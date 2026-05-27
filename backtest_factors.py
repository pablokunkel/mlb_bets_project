#!/usr/bin/env python3
"""
backtest_factors.py — re-score historical pick_inputs using TODAY's
score_* functions and report per-factor predictive accuracy.

Why this exists: the live model ships new anchors and bug fixes regularly.
Stored historical scores reflect the model from the day they were computed,
not the model we'd use today. To know whether the CURRENT model would have
been predictive over the last N days, we have to re-score history with the
current scoring functions.

For each factor (power, matchup, park, form, weather, lineup) and each
window (7d / 30d / season-to-date), we bin batters into quintiles by the
re-scored value and compute:

  - n         : sample size in the bin
  - hr_rate   : actual HR rate (from outcomes)
  - lift      : top-quintile HR rate / bottom-quintile HR rate
  - monotonic : True if HR rate is non-decreasing across bins
  - auc       : ROC-AUC of the factor as a score → did it rank HR-hitters above non-HR-hitters

A well-calibrated factor: lift > 1.3, monotonic, AUC > 0.55.
A dead factor:           lift ≈ 1.0 OR non-monotonic OR AUC ≈ 0.50.

Output:
  - mlb_hr_bet_site/data/factor_accuracy.json (consumed by Performance tab)
  - Console table

Usage:
  python backtest_factors.py                # default: 7d, 30d, season windows
  python backtest_factors.py --days 14      # custom single window
  python backtest_factors.py --no-export    # skip JSON write (console only)
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from score_batters import (
    score_power,
    score_matchup,
    score_park,
    score_form,
    score_weather,
    score_lineup_position,
)

DB_PATH = Path(__file__).parent.parent / "data" / "hr_bets.db"
JSON_OUT = Path(__file__).parent / "mlb_hr_bet_site" / "data" / "factor_accuracy.json"


def _parse_centroid_json(raw):
    """Deserialize a JSON centroid blob into a list; None on bad/missing input.

    Phase 2 helper for backtest_factors.rescore_row — the centroid lives in
    pick_inputs.form_archetype_centroid_json as a JSON string. score_form
    expects the parsed list-of-floats. Silent None on parse failure so old
    rows (pre-Phase-2 migration, no JSON column) fall through to skip.
    """
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_history(db_path: Path, since: str) -> pd.DataFrame:
    """
    Pull every pick_inputs row JOINed with daily_picks (for team, game_pk),
    daily_slate (for venue), and outcomes (for hit_hr) since the given date.

    B16 (2026-05-27): also reads pi.bats / pi.throws (no longer hardcoded
    "R" in rescore_row, B19 fold-in) and the three slate_*_pct columns so
    rescore_row can pass them as kwargs to score_park / score_weather /
    score_matchup — making historical rescoring byte-identical to
    production for rows from 2026-05-27+.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found at {db_path}")
    conn = sqlite3.connect(str(db_path))
    sql = """
        SELECT
            pi.date,
            pi.batter_id                AS player_id,
            pi.barrel_pct, pi.exit_velo, pi.hr_fb_pct, pi.iso,
            pi.xwoba_contact, pi.pull_fb_pct,
            pi.recent_hr_10g, pi.recent_iso_30g, pi.recent_avg_30g,
            pi.recent_window_days, pi.ev_trend,
            pi.recent_barrel_real_14d, pi.recent_xwoba_contact_14d, pi.recent_iso_14d,
            pi.pitcher_hr_per_9, pi.pitcher_era, pi.pitcher_hh_pct,
            pi.pitcher_k_per_9, pi.pitcher_fb_pct_allowed,
            pi.pitcher_recent_hr9_21d, pi.pitcher_recent_starts_21d,
            pi.pitcher_recent_era_21d, pi.pitcher_recent_k9_21d,
            pi.woba_vs_hand, pi.archetype_similarity, pi.vegas_team_total_pct,
            pi.platoon_advantage,
            pi.hr_park_factor,
            pi.temperature_f, pi.wind_mph, pi.wind_direction_deg,
            pi.humidity_pct, pi.is_dome,
            pi.batting_order,
            pi.season_hr,
            pi.bats, pi.throws,
            pi.slate_park_pct, pi.slate_weather_pct,
            pi.slate_pitcher_vulnerability_pct,
            dp.batter_name, dp.team AS batter_team, dp.game_pk,
            ds.venue AS game_venue,
            CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END AS hit_hr
        FROM pick_inputs pi
        INNER JOIN daily_picks dp
            ON dp.date = pi.date AND dp.batter_id = pi.batter_id
        INNER JOIN outcomes o
            ON o.date = pi.date AND o.batter_id = pi.batter_id
        LEFT JOIN daily_slate ds
            ON ds.game_pk = dp.game_pk AND ds.date = dp.date
        WHERE pi.date >= ?
        ORDER BY pi.date, pi.batter_id
    """
    df = pd.read_sql_query(sql, conn, params=(since,))
    conn.close()
    return df


# ---------------------------------------------------------------------------
# Re-scoring
# ---------------------------------------------------------------------------

def rescore_row(row: pd.Series) -> dict:
    """
    Reconstruct batter/pitcher/weather dicts from a pick_inputs row and
    re-compute each factor score with the CURRENT score_* functions.
    Returns {power, matchup, park, form, weather, lineup}.
    """
    # B16 (2026-05-27, bundles B19): pi.bats and pi.throws have been stored
    # since 2026-05-03; use them when present, fall back to "R" only when
    # the column is NULL (pre-2026-05-03 rows). Park's L/R adjustment and
    # the v1 matchup platoon bonus now reflect actual handedness on every
    # row written since the column was added.
    bats = row.get("bats") or "R"
    throws = row.get("throws") or "R"
    batter = {
        "barrel_pct": row.get("barrel_pct"),
        "exit_velo": row.get("exit_velo"),
        "hr_fb_pct": row.get("hr_fb_pct"),
        "iso": row.get("iso"),
        "xwoba_contact": row.get("xwoba_contact"),
        "pull_fb_pct": row.get("pull_fb_pct"),
        "recent_hr_10g": row.get("recent_hr_10g"),
        "recent_iso_30g": row.get("recent_iso_30g"),
        "recent_avg_30g": row.get("recent_avg_30g"),
        "recent_window_days": row.get("recent_window_days"),
        "ev_trend": row.get("ev_trend"),
        # B6a (2026-05-21): rolling 14d quality-contact. NULL for rows
        # older than the migration; score_power skips through. When
        # USE_RECENT_STATCAST_BLEND is on, score_power blends these
        # alongside the season inputs.
        "recent_barrel_real_14d": row.get("recent_barrel_real_14d"),
        "recent_xwoba_contact_14d": row.get("recent_xwoba_contact_14d"),
        "recent_iso_14d": row.get("recent_iso_14d"),
        "woba_vs_hand": row.get("woba_vs_hand"),
        # B19 (2026-05-27): bats is now persisted; row.get("bats", "R") used
        # to be a stale hardcoded "R" before this PR.
        "bats": bats,
        # B8 (2026-05-20): outcomes-cumulative season HR drives the
        # SEASON_HR_FLOOR_TIERS lookup in score_power. Pre-B8, pick_inputs
        # had no hr/season_hr column so this re-score path silently never
        # applied the floor — backtest rank correlations were on
        # floor-less scores while production was floor-applied since
        # 2026-05-03. NULL for rows older than the B8 migration; floor
        # falls through to no-op there (matches the pre-B8 backtest
        # behavior, so old rows remain comparable).
        "season_hr": row.get("season_hr"),
        # Phase 2 form-archetype (2026-05-26): persisted centroid for
        # this batter at the row's date. Read as JSON; score_form only
        # consumes it when USE_FORM_ARCHETYPE is True (off by default).
        # NULL for rows older than Phase 2 — score_form skips cleanly.
        "form_archetype_centroid": _parse_centroid_json(
            row.get("form_archetype_centroid_json"),
        ),
        "form_archetype_window": row.get("form_archetype_window"),
        "form_archetype_n_hrs": row.get("form_archetype_n_hrs"),
    }
    # Audit LOW: drop the `1.2` / `35` league-mean defaults so missing
    # pitcher_hr_per_9 / pitcher_hh_pct skip-on-missing through the v1
    # score_matchup fallback (which was fixed in HIGH #3 to handle
    # None correctly). Pre-fix, a re-score against an old pick_inputs
    # row with NULL pitcher fields silently substituted league mean,
    # producing a different score than the live path would have today.
    pitcher = {
        "hr_per_9": row.get("pitcher_hr_per_9"),
        "era": row.get("pitcher_era"),
        "k_per_9": row.get("pitcher_k_per_9"),
        "hard_hit_pct_allowed": row.get("pitcher_hh_pct"),
        "fb_pct_allowed": row.get("pitcher_fb_pct_allowed"),
        # B4 (2026-05-21): persisted recent pitcher signals enable the
        # backtest harness to compare blend behavior under a candidate
        # window (last-N-starts) against the production baseline (21d).
        # score_pitcher_vulnerability honors these via effective_*().
        # NULL for rows older than the B4 migration; the blend falls
        # through to season-only there (matches pre-B4 behavior).
        "recent_hr9_21d": row.get("pitcher_recent_hr9_21d"),
        "recent_starts_21d": row.get("pitcher_recent_starts_21d"),
        "recent_era_21d": row.get("pitcher_recent_era_21d"),
        "recent_k9_21d": row.get("pitcher_recent_k9_21d"),
        # B19 (2026-05-27): throws now persisted in pick_inputs.
        "throws": throws,
    }
    weather = {
        "temperature_f": row.get("temperature_f", 68),
        "wind_mph": row.get("wind_mph", 5),
        "wind_direction_deg": row.get("wind_direction_deg"),
        "humidity_pct": row.get("humidity_pct"),
        "dome": bool(row.get("is_dome", 0)),
    }

    venue = row.get("game_venue", "") or ""
    pf_df = pd.DataFrame()  # use slate-relative path = off; falls back to anchored

    bo_raw = row.get("batting_order")
    try:
        bo = int(bo_raw) if bo_raw is not None and str(bo_raw).strip() else None
    except (ValueError, TypeError):
        bo = None

    # B16 (2026-05-27): pull the persisted slate percentiles. When non-NULL,
    # the score_* functions use these directly as kwargs, byte-matching
    # the production-day composite. NULL on pre-B16 rows -> kwargs default
    # to None and score_* falls back to the legacy anchored path (matches
    # pre-B16 backtest behavior, so old rows stay comparable).
    spp_raw = row.get("slate_park_pct")
    swp_raw = row.get("slate_weather_pct")
    spv_raw = row.get("slate_pitcher_vulnerability_pct")
    slate_park_pct = float(spp_raw) if spp_raw is not None and not pd.isna(spp_raw) else None
    slate_weather_pct = float(swp_raw) if swp_raw is not None and not pd.isna(swp_raw) else None
    slate_pitcher_vulnerability_pct = (
        float(spv_raw) if spv_raw is not None and not pd.isna(spv_raw) else None
    )

    return {
        "power": score_power(batter),
        "matchup": score_matchup(
            batter, pitcher,
            slate_pitcher_vulnerability_pct=slate_pitcher_vulnerability_pct,
        ),
        "park": score_park(batter, venue, pf_df, slate_park_pct=slate_park_pct),
        "form": score_form(batter),
        "weather": score_weather(
            weather, venue=venue, batter_hand=bats,
            slate_weather_pct=slate_weather_pct,
        ),
        "lineup": score_lineup_position(bo),
    }


def rescore_all(df: pd.DataFrame) -> pd.DataFrame:
    """Apply rescore_row to every row, return a wide DataFrame."""
    print(f"  Re-scoring {len(df)} rows with current model...")
    rescored = df.apply(rescore_row, axis=1, result_type="expand")
    rescored.columns = [f"new_{c}" for c in rescored.columns]
    return pd.concat([df.reset_index(drop=True), rescored.reset_index(drop=True)], axis=1)


# ---------------------------------------------------------------------------
# Per-factor accuracy metrics
# ---------------------------------------------------------------------------

def factor_quintile_table(df: pd.DataFrame, factor_col: str) -> dict:
    """
    Bin df by factor_col into quintiles, compute n + HR rate per bin.
    Returns dict with bins[], lift, monotonic, auc, n_total.
    """
    sub = df[df[factor_col].notna() & df["hit_hr"].notna()].copy()
    if len(sub) < 25:
        return {"bins": [], "lift": None, "monotonic": None, "auc": None, "n_total": len(sub)}

    # Quintile binning. Pass labels=False so qcut returns integer indices
    # 0..n-1; this avoids the "labels must be one fewer than bin edges"
    # error when duplicates="drop" collapses bins (happens for discrete
    # factors like lineup_score where many batters share the same value).
    try:
        sub["bin"] = pd.qcut(sub[factor_col], q=5, labels=False, duplicates="drop")
    except ValueError:
        # Last-resort: factor has ≤1 unique value, can't bin at all
        return {"bins": [], "lift": None, "monotonic": None, "auc": None, "n_total": len(sub)}

    bins = []
    for b in sorted(sub["bin"].dropna().unique()):
        chunk = sub[sub["bin"] == b]
        lo = float(chunk[factor_col].min())
        hi = float(chunk[factor_col].max())
        n = int(len(chunk))
        hr_rate = float(chunk["hit_hr"].mean())
        bins.append({
            "bin": int(b) + 1,  # display 1-indexed (Q1..Q5)
            "score_lo": round(lo, 1),
            "score_hi": round(hi, 1),
            "n": n,
            "hr_rate": round(hr_rate, 4),
        })

    if len(bins) < 2:
        return {"bins": bins, "lift": None, "monotonic": None, "auc": None, "n_total": len(sub)}

    top = bins[-1]["hr_rate"]
    bot = bins[0]["hr_rate"]
    lift = round(top / bot, 2) if bot > 0 else None

    # Monotonic non-decreasing across bins?
    rates = [b["hr_rate"] for b in bins]
    monotonic = all(rates[i] <= rates[i + 1] for i in range(len(rates) - 1))

    # ROC-AUC: probability that a HR-hitter has higher score than a non-hitter.
    # Computed without sklearn so the script has zero extra deps.
    pos = sub[sub["hit_hr"] == 1][factor_col].values
    neg = sub[sub["hit_hr"] == 0][factor_col].values
    auc = mann_whitney_auc(pos, neg)

    return {
        "bins": bins,
        "lift": lift,
        "monotonic": bool(monotonic),
        "auc": round(auc, 3) if auc is not None else None,
        "n_total": int(len(sub)),
    }


def mann_whitney_auc(pos: np.ndarray, neg: np.ndarray) -> float | None:
    """
    Compute ROC-AUC via the Mann-Whitney U formulation. AUC = P(pos > neg)
    + 0.5 * P(pos == neg). Returns None if either group is empty.
    """
    if len(pos) == 0 or len(neg) == 0:
        return None
    all_vals = np.concatenate([pos, neg])
    ranks = pd.Series(all_vals).rank().values
    rank_pos = ranks[: len(pos)].sum()
    n_p, n_n = len(pos), len(neg)
    u = rank_pos - n_p * (n_p + 1) / 2
    return u / (n_p * n_n)


# ---------------------------------------------------------------------------
# Window orchestration
# ---------------------------------------------------------------------------

FACTORS = ["power", "matchup", "park", "form", "weather", "lineup"]


def build_window_report(df: pd.DataFrame, label: str, days: int | None) -> dict:
    """Run quintile analysis for each factor on the windowed slice."""
    if days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        sub = df[df["date"] >= cutoff].copy()
    else:
        sub = df.copy()

    if len(sub) == 0:
        return {"label": label, "days": days, "n": 0, "factors": {}}

    out = {
        "label": label,
        "days": days,
        "n": int(len(sub)),
        "date_min": str(sub["date"].min()),
        "date_max": str(sub["date"].max()),
        "overall_hr_rate": round(float(sub["hit_hr"].mean()), 4),
        "factors": {},
    }
    for f in FACTORS:
        out["factors"][f] = factor_quintile_table(sub, f"new_{f}")
    return out


def print_console_table(report: dict) -> None:
    print()
    print("=" * 78)
    print(f"  {report['label']:<30}  n={report['n']}  hr_rate={report['overall_hr_rate']:.2%}")
    print("=" * 78)
    print(f"  {'FACTOR':<10} {'LIFT':>6} {'AUC':>6} {'MONO':>5}  {'BIN BREAKDOWN (n / hr_rate)':<40}")
    print("  " + "-" * 76)
    for f in FACTORS:
        r = report["factors"][f]
        if not r["bins"]:
            print(f"  {f:<10}  (insufficient data: n={r['n_total']})")
            continue
        bin_str = " ".join(
            f"Q{b['bin']}:{b['n']}/{b['hr_rate']*100:.1f}%" for b in r["bins"]
        )
        lift = f"{r['lift']:.2f}x" if r["lift"] is not None else "  —  "
        auc = f"{r['auc']:.3f}" if r["auc"] is not None else "  —  "
        mono = "✓" if r["monotonic"] else "✗"
        print(f"  {f:<10} {lift:>6} {auc:>6} {mono:>5}  {bin_str}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--days",
        type=int,
        default=None,
        help="If set, run a single window of N days. Default: 7d / 30d / season",
    )
    ap.add_argument("--since", default=None, help="Override season cutoff (YYYY-MM-DD)")
    ap.add_argument(
        "--no-export",
        action="store_true",
        help="Skip writing factor_accuracy.json",
    )
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()

    db_path = Path(args.db)

    # Load enough history to cover season-to-date (default 90d safety buffer)
    season_cutoff = args.since or (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
    print(f"Loading pick_inputs since {season_cutoff} from {db_path}")
    df = load_history(db_path, since=season_cutoff)
    print(f"  Loaded {len(df)} rows ({df['date'].nunique()} distinct dates)")

    if len(df) == 0:
        print("No data — aborting.")
        sys.exit(1)

    # Re-score using current model
    df = rescore_all(df)

    # Define windows
    if args.days:
        windows = [(f"Last {args.days}d", args.days)]
    else:
        windows = [
            ("Last 7d", 7),
            ("Last 30d", 30),
            ("Season-to-date", None),
        ]

    reports = [build_window_report(df, label, days) for label, days in windows]

    for r in reports:
        print_console_table(r)

    if not args.no_export:
        JSON_OUT.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat() + "Z",
            "model_version": "current",  # bump when score_* anchors change
            "windows": reports,
        }
        # Atomic write — project is in OneDrive and partial writes get truncated
        tmp = JSON_OUT.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(JSON_OUT)
        print(f"Wrote {JSON_OUT}")


if __name__ == "__main__":
    main()
