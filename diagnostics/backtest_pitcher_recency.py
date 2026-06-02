#!/usr/bin/env python3
"""
backtest_pitcher_recency.py — sweep pitcher-recency window/blend candidates.

What this answers
-----------------
What's the right pitcher-recency config? Candidates vary along three axes:

  * window_type x window_n  — calendar days vs last-N-starts
  * blend_weight             — how much weight on the recent vs season value
  * min_starts               — sample-size gate below which we fall back to season

For each candidate, we re-score every historical slate in the window using
the same scoring functions production uses (compute_slate_context +
score_pitcher_vulnerability), but with the candidate config in place of
production's (days=21, blend=0.60, min_starts=2).

We project the matchup-score delta onto each batter row, recompute the
composite, re-rank the board, and intersect the top 8 (and top 30) with
the actual outcomes table. Output is a per-candidate metrics table.

How it differs from backtest_factors.py
---------------------------------------
backtest_factors re-scores using whatever recent_*_21d values were stored
on the day. To sweep candidates, we need to RE-COMPUTE those values from
the underlying gameLog under each candidate (different window, different
aggregation). So this script does its own gameLog fetch + aggregation.

Output
------
Console table sorted by top-8 hit rate (primary) then AUC (tiebreak).
Optional --json-out to dump full results for diffing across runs.

Usage
-----
    python diagnostics/backtest_pitcher_recency.py
    python diagnostics/backtest_pitcher_recency.py --start 2026-04-15 --end 2026-05-20
    python diagnostics/backtest_pitcher_recency.py --baseline-only
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# Project root importable from diagnostics/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fetch_daily_data import get_recent_pitcher_game_log, MLB_STATS_API
from pitcher_profile import (
    effective_hr9, effective_era, effective_k9,
    RECENT_HR9_BLEND_WEIGHT, RECENT_HR9_MIN_STARTS,
)
from score_batters import WEIGHT_CONFIGS, percentile_rank_dict
from etl.db import DB_PATH, CACHE_DIR  # single anchor (B26)
import requests


DEFAULT_START = "2026-04-15"
DEFAULT_END   = "2026-05-20"

# When estimating matchup-score deltas from pitcher-vulnerability deltas,
# v2 matchup has 4 signals (vuln + sim + total + woba) so vuln's share
# is 1/4 = 0.25. The harness applies this conservatively. UNDER-states
# the lift for batters with fewer matchup signals (v1, no Vegas, etc).
VULN_SHARE_V2 = 0.25


# ---------------------------------------------------------------------------
# Candidate matrix
# ---------------------------------------------------------------------------
# Each candidate is (label, window_type, window_n, blend_weight, min_starts).
# Baseline first; candidates after, sorted from "minimal change" to "biggest"
# so the diff vs baseline is easy to read in the table.
CANDIDATES = [
    # Baseline: production today
    ("21d/60-40/2  (baseline)",        "days",   21, 0.60, 2),
    # Same window, more recency weight
    ("21d/70-30/2",                    "days",   21, 0.70, 2),
    # Last-N-starts windows
    ("5 starts/60-40/3",               "starts",  5, 0.60, 3),
    ("5 starts/70-30/3  (rec'd)",      "starts",  5, 0.70, 3),
    ("3 starts/100-0/3",               "starts",  3, 1.00, 3),
    ("7 starts/60-40/4",               "starts",  7, 0.60, 4),
]


# ---------------------------------------------------------------------------
# Load historical slate data
# ---------------------------------------------------------------------------

def load_slates(conn: sqlite3.Connection, start: str, end: str) -> pd.DataFrame:
    """Load every pick_inputs row JOINed with daily_picks/outcomes for the window."""
    sql = """
        SELECT
            pi.date,
            pi.batter_id          AS player_id,
            dp.batter_name        AS batter_name,
            dp.team               AS batter_team,
            dp.game_pk            AS game_pk,
            dp.opp_pitcher        AS opp_pitcher,
            dp.composite          AS old_composite,
            dp.matchup_score      AS old_matchup,
            dp.rank_in_board      AS old_rank,
            CASE WHEN o.hr_count > 0 THEN 1 ELSE 0 END AS hit_hr
        FROM pick_inputs pi
        INNER JOIN daily_picks dp
            ON dp.date = pi.date AND dp.batter_id = pi.batter_id
        INNER JOIN outcomes o
            ON o.date = pi.date AND o.batter_id = pi.batter_id
        WHERE pi.date BETWEEN ? AND ?
        ORDER BY pi.date, dp.rank_in_board
    """
    return pd.read_sql_query(sql, conn, params=(start, end))


def load_slate_pitchers(conn: sqlite3.Connection, start: str, end: str) -> dict:
    """Return {date: {pitcher_name: {pitcher_id, season stats}}}.

    Joins daily_picks (opp_pitcher_id) with season_pitching for season stats.
    """
    cur = conn.execute("""
        SELECT DISTINCT
            dp.date,
            dp.opp_pitcher        AS pname,
            dp.opp_pitcher_id     AS pid,
            sp.ip, sp.era, sp.hr_per_9, sp.k_per_9, sp.hard_hit_pct
        FROM daily_picks dp
        LEFT JOIN season_pitching sp
            ON sp.pitcher_id = dp.opp_pitcher_id AND sp.season = 2026
        WHERE dp.date BETWEEN ? AND ?
          AND dp.opp_pitcher_id IS NOT NULL
          AND dp.opp_pitcher_id > 0
    """, (start, end))
    out: dict = defaultdict(dict)
    for row in cur.fetchall():
        date, pname, pid, ip, era, hr9, k9, hh = row
        if not pname or not pid:
            continue
        out[date][pname] = {
            "pitcher_id": pid,
            "ip": ip,
            "era": era,
            "hr_per_9": hr9,
            "k_per_9": k9,
            "hard_hit_pct_allowed": hh,
        }
    return out


# ---------------------------------------------------------------------------
# GameLog cache — one fetch per pitcher per scoring date
# ---------------------------------------------------------------------------

# Persistent on-disk cache so repeat runs don't pay the MLB API roundtrip.
GAMELOG_CACHE_DIR = CACHE_DIR / "diagnostics_pitcher_gamelog"


def _gamelog_path(pid: int, season: int) -> Path:
    GAMELOG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return GAMELOG_CACHE_DIR / f"{season}_{pid}.json"


def fetch_full_gamelog(pid: int, season: int) -> list[dict]:
    """Pull the full season gameLog for a pitcher, returning the raw splits.

    Cached to disk — gameLogs are append-only, so re-fetching every backtest
    run is wasteful. If the file exists, just read it.
    """
    path = _gamelog_path(pid, season)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass

    url = f"{MLB_STATS_API}/people/{pid}/stats"
    params = {"stats": "gameLog", "season": season, "group": "pitching", "gameType": "R"}
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        splits = (data.get("stats", []) or [{}])[0].get("splits", []) or []
    except Exception as e:
        print(f"  [gamelog] failed for pid={pid}: {e}", file=sys.stderr)
        splits = []

    try:
        path.write_text(json.dumps(splits))
    except Exception:
        pass
    return splits


def aggregate_under_candidate(
    splits: list[dict],
    as_of_date: str,
    window_type: str,
    window_n: int,
) -> dict:
    """Re-aggregate cached gameLog splits under the candidate's window.

    Mirrors fetch_daily_data.get_recent_pitcher_game_log's aggregation but
    operates on already-fetched splits. as_of_date is strictly exclusive.
    """
    if window_type == "days":
        as_of_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
        cutoff = (as_of_dt - timedelta(days=window_n)).strftime("%Y-%m-%d")
        candidate = [g for g in splits
                     if cutoff <= (g.get("date") or "") < as_of_date]
    else:
        # window_type == 'starts': last N games_started strictly before as_of_date
        kept = []
        for g in reversed(splits):
            gdate = g.get("date") or ""
            if not gdate or gdate >= as_of_date:
                continue
            s = g.get("stat", {}) or {}
            if int(s.get("gamesStarted", 0) or 0) == 0:
                continue
            kept.append(g)
            if len(kept) >= window_n:
                break
        candidate = kept

    hr = er = k = outs = starts = 0
    for g in candidate:
        s = g.get("stat", {}) or {}
        ip_str = str(s.get("inningsPitched", "0") or "0")
        try:
            whole, _, frac = ip_str.partition(".")
            o = int(whole) * 3 + (int(frac) if frac else 0)
        except (ValueError, TypeError):
            o = 0
        gs = int(s.get("gamesStarted", 0) or 0)
        if gs == 0 and o == 0:
            continue
        hr    += int(s.get("homeRuns", 0) or 0)
        er    += int(s.get("earnedRuns", 0) or 0)
        k     += int(s.get("strikeOuts", 0) or 0)
        outs  += o
        starts += gs

    ip = outs / 3.0
    if ip < 1.0:
        return {"recent_starts": starts, "recent_hr_per_9": None,
                "recent_era": None, "recent_k_per_9": None}
    return {
        "recent_starts":   starts,
        "recent_hr_per_9": hr * 9.0 / ip,
        "recent_era":      er * 9.0 / ip,
        "recent_k_per_9":  k * 9.0 / ip,
    }


# ---------------------------------------------------------------------------
# Re-scoring under a candidate
# ---------------------------------------------------------------------------

def recompute_pitcher_pct_for_date(
    pitchers_today: dict,
    recents_today: dict,
    blend_weight: float,
    min_starts: int,
) -> dict:
    """Mirror compute_slate_context's pitcher_pct under the candidate blend.

    Returns {pname: pct (0-100)}.
    """
    raw_by_name: dict = {}
    for pname, p in pitchers_today.items():
        r = recents_today.get(pname, {})
        components: list = []

        hr9 = effective_hr9(
            p.get("hr_per_9"), r.get("recent_hr_per_9"), r.get("recent_starts"),
            blend_weight=blend_weight, min_starts=min_starts,
        )
        if hr9 is not None and hr9 > 0:
            components.append(hr9 * 30.0)

        era = effective_era(
            p.get("era"), r.get("recent_era"), r.get("recent_starts"),
            blend_weight=blend_weight, min_starts=min_starts,
        )
        if era is not None and era > 0:
            components.append(era * 5.0)

        hh = p.get("hard_hit_pct_allowed")
        if hh is not None and hh > 0:
            components.append(hh * 0.6)

        k9 = effective_k9(
            p.get("k_per_9"), r.get("recent_k_per_9"), r.get("recent_starts"),
            blend_weight=blend_weight, min_starts=min_starts,
        )
        if k9 is not None and k9 > 0:
            components.append(-k9 * 2.0)

        if len(components) < 2:
            continue
        raw_by_name[pname] = sum(components)

    return percentile_rank_dict(raw_by_name)


def project_new_board(
    slates_df: pd.DataFrame,
    pitchers_by_date: dict,
    candidate: tuple,
) -> pd.DataFrame:
    """For each row, compute new_composite under the candidate's recency config.

    Returns the input df with new_composite + delta + new_rank_per_date columns.
    """
    _, window_type, window_n, blend_weight, min_starts = candidate
    w = WEIGHT_CONFIGS["default"]

    rows_out = []
    seen_pitchers: dict = {}    # cache cached gamelog splits

    for date, day_df in slates_df.groupby("date"):
        pitchers_today = pitchers_by_date.get(date, {})

        # Compute recents under candidate for every pitcher today (cached)
        recents_today: dict = {}
        for pname, p in pitchers_today.items():
            pid = p["pitcher_id"]
            if pid not in seen_pitchers:
                seen_pitchers[pid] = fetch_full_gamelog(pid, 2026)
            splits = seen_pitchers[pid]
            recents_today[pname] = aggregate_under_candidate(
                splits, as_of_date=date,
                window_type=window_type, window_n=window_n,
            )

        # New pitcher_pct under candidate
        new_pct = recompute_pitcher_pct_for_date(
            pitchers_today, recents_today, blend_weight, min_starts,
        )
        # Baseline pitcher_pct under production config — same loader, different params
        baseline_pct = recompute_pitcher_pct_for_date(
            pitchers_today,
            {pn: aggregate_under_candidate(seen_pitchers[pitchers_today[pn]["pitcher_id"]],
                                            as_of_date=date,
                                            window_type="days", window_n=21)
             for pn in pitchers_today},
            blend_weight=RECENT_HR9_BLEND_WEIGHT,
            min_starts=RECENT_HR9_MIN_STARTS,
        )

        for _, row in day_df.iterrows():
            opp = row.get("opp_pitcher") or ""
            old_comp = row.get("old_composite") or 0
            old_match = row.get("old_matchup") or 0
            if opp not in new_pct or opp not in baseline_pct:
                new_comp = old_comp
                new_match = old_match
            else:
                pct_delta = new_pct[opp] - baseline_pct[opp]
                match_delta = pct_delta * VULN_SHARE_V2
                new_match = max(0, min(100, old_match + match_delta))
                new_comp = old_comp + w["matchup"] * (new_match - old_match)

            rows_out.append({
                **row.to_dict(),
                "new_matchup": new_match,
                "new_composite": new_comp,
            })

    df = pd.DataFrame(rows_out)
    # Re-rank within each date
    df["new_rank"] = df.groupby("date")["new_composite"].rank(
        method="first", ascending=False
    ).astype(int)
    return df


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def auc(scores: np.ndarray, labels: np.ndarray) -> float | None:
    """ROC-AUC via Mann-Whitney U. Returns None if either class is empty."""
    pos = scores[labels == 1]
    neg = scores[labels == 0]
    if len(pos) == 0 or len(neg) == 0:
        return None
    all_vals = np.concatenate([pos, neg])
    ranks = pd.Series(all_vals).rank().values
    rank_pos = ranks[:len(pos)].sum()
    u = rank_pos - len(pos) * (len(pos) + 1) / 2
    return u / (len(pos) * len(neg))


def metrics_for_board(df: pd.DataFrame, rank_col: str) -> dict:
    """top-8 / top-30 hit rate + AUC on the rank column for the slate."""
    hr = df[df[rank_col] <= 8]
    top8_rate = hr["hit_hr"].mean() if len(hr) else 0.0
    hr30 = df[df[rank_col] <= 30]
    top30_rate = hr30["hit_hr"].mean() if len(hr30) else 0.0

    # AUC: score ranks (lower = better, so flip sign) vs hit_hr labels
    # Use -rank so the AUC is in the conventional "higher score -> HR" direction.
    scores_neg_rank = -df[rank_col].astype(float).values
    auc_val = auc(scores_neg_rank, df["hit_hr"].astype(int).values)

    return {
        "top8_hit_rate": float(top8_rate),
        "top30_hit_rate": float(top30_rate),
        "auc": float(auc_val) if auc_val is not None else None,
        "n_slate_days": int(df["date"].nunique()),
        "n_rows": int(len(df)),
        "n_hr": int(df["hit_hr"].sum()),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end",   default=DEFAULT_END)
    ap.add_argument("--db",    default=str(DB_PATH))
    ap.add_argument("--json-out", default=None,
                    help="Write per-candidate results to JSON for diffing")
    ap.add_argument("--baseline-only", action="store_true",
                    help="Only re-run the baseline candidate (smoke test path)")
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"DB not found at {args.db}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    print(f"Loading slates from {args.start} to {args.end} ...")
    slates_df = load_slates(conn, args.start, args.end)
    print(f"  {len(slates_df)} batter-day rows across {slates_df['date'].nunique()} dates")
    if len(slates_df) == 0:
        sys.exit(1)

    pitchers_by_date = load_slate_pitchers(conn, args.start, args.end)
    print(f"  pitcher coverage: {sum(len(v) for v in pitchers_by_date.values())} slate-pitchers")

    # Baseline: outcome rates against the originally-stored rank
    base_top8 = slates_df[slates_df["old_rank"] <= 8]["hit_hr"].mean()
    base_top30 = slates_df[slates_df["old_rank"] <= 30]["hit_hr"].mean()
    print()
    print(f"As-stored baseline (production at the time):")
    print(f"  top-8 hit rate:  {base_top8:.3f}")
    print(f"  top-30 hit rate: {base_top30:.3f}")
    print()

    candidates = CANDIDATES[:1] if args.baseline_only else CANDIDATES

    results = []
    for cand in candidates:
        label = cand[0]
        print(f"Scoring candidate: {label}")
        proj = project_new_board(slates_df, pitchers_by_date, cand)
        m = metrics_for_board(proj, rank_col="new_rank")
        m["candidate"] = label
        m["window_type"]  = cand[1]
        m["window_n"]     = cand[2]
        m["blend_weight"] = cand[3]
        m["min_starts"]   = cand[4]
        results.append(m)

    # Table
    print()
    print("=" * 92)
    print(f"  {'candidate':<28} {'top-8':>7} {'top-30':>7} {'AUC':>6}  {'n_rows':>7} {'n_HR':>6} {'days':>5}")
    print("-" * 92)
    for r in sorted(results, key=lambda x: (-x["top8_hit_rate"], -(x["auc"] or 0))):
        auc_s = f"{r['auc']:.3f}" if r["auc"] is not None else "  -  "
        print(f"  {r['candidate']:<28} {r['top8_hit_rate']:>7.3f} "
              f"{r['top30_hit_rate']:>7.3f} {auc_s:>6}  "
              f"{r['n_rows']:>7} {r['n_hr']:>6} {r['n_slate_days']:>5}")
    print("=" * 92)
    print()
    print("Pick the top row (highest top-8 hit rate; AUC tiebreak), document the")
    print("decision in WEIGHT_REFIT_LOG.md, then flip pitcher_profile's")
    print("PITCHER_RECENT_WINDOW_TYPE + _WINDOW_N + RECENT_HR9_BLEND_WEIGHT + ")
    print("RECENT_HR9_MIN_STARTS to match.")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps({
            "window": [args.start, args.end],
            "baseline_as_stored": {
                "top8_hit_rate": float(base_top8),
                "top30_hit_rate": float(base_top30),
            },
            "candidates": results,
        }, indent=2))
        print(f"Wrote {args.json_out}")


if __name__ == "__main__":
    main()
