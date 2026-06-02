#!/usr/bin/env python3
"""
counterfactual_recency_2026_05_12.py — replay 2026-05-12 with pitcher recency.

What this answers: "If pitcher_recent_hr9_21d had been live the morning of
2026-05-12, where would CJ Abrams / James Wood (and the Singer-faced Nats
in general) have landed?"

How it works:
  1. Load the actual 2026-05-12 board (380 batters, frozen composites).
  2. For each slate pitcher, look up season HR/9 from the DB and recent HR/9
     from the MLB API gameLog with today_str='2026-05-12' (excludes the 5/12
     game itself — same window the live pipeline would see at noon on 5/12).
  3. Recompute compute_slate_context's pitcher_vuln raw + percentile with the
     new effective HR/9 (60/40 blend), leaving every other component
     (ERA, HH%, K/9, FB%) untouched.
  4. For each batter, project the matchup_score delta as the difference in
     vulnerability percentile times the share of matchup signals that's
     vulnerability (2/4 = 0.50 when v2 fires with 4 signals).
  5. Recompute composite with the existing default weights and re-rank.

This is an estimate, not a full re-run. compute_slate_context's exact raw
shifts can affect other batters' relative rank too — the projection above
just isolates the recency effect at the pitcher-vulnerability layer.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

# Make project root importable from diagnostics/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fetch_daily_data import get_recent_pitcher_game_log
from pitcher_profile import effective_hr9
from score_batters import WEIGHT_CONFIGS, percentile_rank_dict
from etl.db import SITE_DATA_DIR, get_db  # single anchor (B26)


PICKS_JSON = SITE_DATA_DIR / "picks_latest.json"
TARGET_DATE = "2026-05-12"


def load_board() -> tuple[list[dict], list[dict]]:
    """Return (selected_picks, full_board) from picks_latest.json."""
    with PICKS_JSON.open() as f:
        d = json.load(f)
    if d.get("date") != TARGET_DATE:
        raise RuntimeError(f"picks_latest.json is for {d.get('date')}, not {TARGET_DATE}")
    return d["picks"], d["full_board"]


def load_pitcher_stats(conn: sqlite3.Connection, pitcher_names: list[str]) -> dict:
    """Read season_pitching for each pitcher in the slate."""
    q = ",".join("?" * len(pitcher_names))
    rows = conn.execute(
        f"""SELECT pitcher_id, pitcher_name, ip, era, hr_per_9, k_per_9, hard_hit_pct
            FROM season_pitching WHERE season=2026 AND pitcher_name IN ({q})""",
        pitcher_names,
    ).fetchall()
    return {
        r["pitcher_name"]: {
            "pitcher_id": r["pitcher_id"],
            "ip": r["ip"],
            "era": r["era"],
            "hr_per_9": r["hr_per_9"],
            "k_per_9": r["k_per_9"],
            "hard_hit_pct": r["hard_hit_pct"],
        }
        for r in rows
    }


def vuln_raw_components(p: dict, hr9_eff: float | None) -> list[float]:
    """Replica of compute_slate_context's per-pitcher component list."""
    components = []
    if hr9_eff is not None and hr9_eff > 0:
        components.append(hr9_eff * 30.0)
    era = p.get("era")
    if era is not None and era > 0:
        components.append(era * 5.0)
    hh = p.get("hard_hit_pct")
    if hh is not None and hh > 0:
        components.append(hh * 0.6)
    k9 = p.get("k_per_9")
    if k9 is not None and k9 > 0:
        components.append(-k9 * 2.0)
    return components


def main():
    conn = get_db()  # canonical DB; fail-loud if absent (B26)

    selected, board = load_board()
    # Map opp_pitcher (display name) → batter rows
    pitchers_in_slate = sorted({r["opp_pitcher"] for r in board if r.get("opp_pitcher")})
    print(f"Slate pitchers ({len(pitchers_in_slate)}): {pitchers_in_slate}")
    print()

    season = load_pitcher_stats(conn, pitchers_in_slate)

    # Fetch recency (gameLog) for each pitcher with today_str='2026-05-12'
    # so the window EXCLUDES the 5/12 game itself — same view the live noon
    # run would have had.
    print("Fetching MLB API gameLogs (today_str=2026-05-12, days=21)...")
    recent = {}
    for name in pitchers_in_slate:
        pid = season.get(name, {}).get("pitcher_id")
        if not pid:
            recent[name] = {}
            continue
        recent[name] = get_recent_pitcher_game_log(
            pid, 2026, today_str=TARGET_DATE, days=21
        )

    # Build OLD and NEW vulnerability percentile dicts
    old_raw, new_raw = {}, {}
    print()
    print("Per-pitcher: season HR/9 -> blended HR/9 (recent HR/9, starts)")
    print(f"{'pitcher':22s} {'sznHR9':>7} {'recHR9':>7} {'starts':>6} {'effHR9':>7}")
    for name in pitchers_in_slate:
        p = season.get(name, {})
        r = recent.get(name, {}) or {}
        szn_hr9 = p.get("hr_per_9")
        rec_hr9 = r.get("recent_hr_per_9")
        starts  = r.get("recent_starts")

        eff = effective_hr9(szn_hr9, rec_hr9, starts)

        old_components = vuln_raw_components(p, szn_hr9)
        new_components = vuln_raw_components(p, eff)
        if len(old_components) >= 2:
            old_raw[name] = sum(old_components)
        if len(new_components) >= 2:
            new_raw[name] = sum(new_components)

        print(
            f"{name[:22]:22s} "
            f"{szn_hr9 if szn_hr9 is not None else '-':>7} "
            f"{rec_hr9 if rec_hr9 is not None else '-':>7} "
            f"{starts if starts is not None else '-':>6} "
            f"{eff if eff is not None else '-':>7}"
        )

    old_pct = percentile_rank_dict(old_raw)
    new_pct = percentile_rank_dict(new_raw)

    print()
    print("Pitcher vulnerability percentile: OLD -> NEW")
    print(f"{'pitcher':22s} {'OLD pct':>8} {'NEW pct':>8} {'diff':>7}")
    for name in sorted(pitchers_in_slate, key=lambda n: -(new_pct.get(n, 0))):
        if name not in new_pct or name not in old_pct:
            continue
        delta = new_pct[name] - old_pct[name]
        print(f"{name[:22]:22s} {old_pct[name]:>8.1f} {new_pct[name]:>8.1f} {delta:>+7.1f}")

    # Estimate per-batter matchup_score delta.
    # In matchup_v2 with 4 signals (vuln + similarity + Vegas + woba), the
    # vulnerability share is 1/4 = 0.25. With 3 signals it's 1/3 ~= 0.33.
    # We don't know per-batter signal counts from picks_latest.json, so use
    # 0.25 as a conservative average — this UNDER-states the lift for
    # batters with fewer matchup signals available.
    VULN_SHARE = 0.25

    # Default config weights (current production)
    w = WEIGHT_CONFIGS["default"]

    # Apply DELTA to the original composite rather than recomputing it
    # from scratch — board entries serialize lineup_score as null (computed
    # from batting_order at score time), so a full recompute would drop
    # the lineup contribution. Linear-delta is exact under the constant-
    # weight assumption: composite is linear in matchup_score, so:
    #   new_composite = old_composite + w_matchup * (new_match - old_match)
    rescored = []
    for r in board:
        opp = r.get("opp_pitcher") or ""
        old_comp = r.get("composite") or 0
        if opp not in old_pct or opp not in new_pct:
            rescored.append({**r, "new_matchup": r.get("matchup_score"),
                             "new_composite": old_comp, "delta": 0.0})
            continue

        pct_delta = new_pct[opp] - old_pct[opp]
        match_delta = pct_delta * VULN_SHARE
        old_match = r.get("matchup_score") or 0
        new_match = max(0, min(100, old_match + match_delta))

        composite = old_comp + w["matchup"] * (new_match - old_match)
        rescored.append({**r, "new_matchup": new_match,
                         "new_composite": composite,
                         "delta": composite - old_comp})

    rescored.sort(key=lambda x: -(x.get("new_composite") or 0))

    print()
    print("NEW TOP 12 BOARD (recency-aware):")
    print(f"{'rank':>4} {'name':25s} {'team':4s} {'opp':20s} {'oldComp':>7} {'newComp':>7} {'diff':>6} {'oldM':>5} {'newM':>5}")
    for i, r in enumerate(rescored[:12], start=1):
        print(
            f"{i:>4} {(r.get('batter_name') or '')[:25]:25s} "
            f"{r.get('team',''):4s} "
            f"{(r.get('opp_pitcher') or '')[:20]:20s} "
            f"{(r.get('composite') or 0):>7.1f} "
            f"{(r.get('new_composite') or 0):>7.1f} "
            f"{r.get('delta', 0):>+6.1f} "
            f"{(r.get('matchup_score') or 0):>5.1f} "
            f"{(r.get('new_matchup') or r.get('matchup_score') or 0):>5.1f}"
        )

    # Highlight Singer-faced batters specifically
    print()
    print("SINGER-FACED NATS BATTERS — old vs new rank/composite:")
    print(f"{'name':25s} {'oldRank':>8} {'newRank':>8} {'oldComp':>8} {'newComp':>8} {'diff':>6}")
    new_ranks = {r["batter_id"]: i + 1 for i, r in enumerate(rescored)}
    for r in rescored:
        if (r.get("opp_pitcher") or "") == "Brady Singer":
            print(
                f"{(r.get('batter_name') or '')[:25]:25s} "
                f"{r.get('rank_in_board', 0):>8} "
                f"{new_ranks.get(r['batter_id'], 0):>8} "
                f"{(r.get('composite') or 0):>8.1f} "
                f"{(r.get('new_composite') or 0):>8.1f} "
                f"{r.get('delta', 0):>+6.1f}"
            )


if __name__ == "__main__":
    main()
