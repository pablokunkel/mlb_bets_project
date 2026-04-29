#!/usr/bin/env python3
"""
park_factors_seed.py — Curated MLB HR park factors with L/R handedness splits.

This is the SEED data set used to populate the park_factors table when we
can't pull live data. It's based on publicly-known park effects from the
Baseball Savant Statcast Park Factors leaderboard (3-year rolling, 2022-2024
window) combined with well-documented handedness effects.

Columns:
    venue           : Canonical MLB venue name (matches fetch_daily_data.VENUE_COORDS)
    hr_pf_overall   : Overall HR park factor (100 = league average)
    hr_pf_lhb       : HR park factor for left-handed batters
    hr_pf_rhb       : HR park factor for right-handed batters
    notes           : Why the split looks the way it does

Guidelines for the splits:
  - "Short porch" parks (Yankee Stadium RF, GABP RF, Citizens Bank RF,
    Minute Maid Crawford Boxes) get large LHB boosts
  - "Green Monster" Fenway rewards RHB on fly balls
  - "Triples Alley" parks (Oracle, Comerica, PNC) punish LHB hard
  - Coors is the universal boost — both sides see ~30% more HRs, with
    a slight LHB edge historically
  - When in doubt, split 50/50 to the overall factor

Replace this file with a live pull from Baseball Savant whenever possible.
The ETL step etl_nightly.sync_park_factors() will try a live source first
and fall back to this seed.
"""

from __future__ import annotations

import pandas as pd

# Format: (venue, overall, LHB, RHB, notes)
PARK_FACTORS_SEED: list[tuple[str, int, int, int, str]] = [
    # ─── Extreme HR parks ────────────────────────────────────────────────
    ("Coors Field",              130, 132, 128, "Thin air boosts both sides; slight LHB edge historically"),
    ("Great American Ball Park", 118, 125, 112, "325 ft RF porch; LHB heavy boost"),
    ("Yankee Stadium",           115, 128, 105, "314 ft RF porch; LHB one of biggest park effects in MLB"),
    ("Globe Life Field",         112, 115, 110, "Symmetrical but hitter-friendly; slight LHB edge"),
    ("Fenway Park",               110, 100, 118, "Monster helps RHB HRs; deep RF hurts LHB"),
    ("Oriole Park at Camden Yards", 108, 104, 112, "LF push-back in 2022 flipped this to RHB-favoring"),
    ("Wrigley Field",            108, 107, 109, "Wind-dependent; roughly neutral splits"),
    ("Rogers Centre",            107, 108, 106, "Post-renovation neutral; slight LHB edge"),
    ("Citizens Bank Park",       107, 115, 100, "329 ft RF wall; LHB boost"),
    ("Minute Maid Park",         106, 112, 101, "Crawford Boxes + short LF used to favor RHB, but RF line is short for LHB"),

    # ─── Slightly hitter-friendly ────────────────────────────────────────
    ("Dodger Stadium",           105, 103, 107, "Slight overall boost; mild RHB edge"),
    ("American Family Field",    105, 102, 108, "Retractable roof; slight RHB edge from LF dimensions"),
    ("Chase Field",              104, 101, 107, "Dry air; deeper RF hurts LHB slightly"),
    ("Truist Park",              104, 100, 108, "Slight RHB edge from LF dimensions"),
    ("Nationals Park",           103, 106, 100, "Slight LHB edge from RF geometry"),
    ("Guaranteed Rate Field",    103, 105, 101, "Slight LHB edge"),
    ("Busch Stadium",            102, 102, 102, "Neutral across handedness"),
    ("Target Field",             101, 101, 101, "Neutral"),

    # ─── Neutral ─────────────────────────────────────────────────────────
    ("Kauffman Stadium",         100, 100, 100, "Baseline"),
    ("Angel Stadium",             99, 99, 99, "Near-neutral"),
    ("Citi Field",                98, 94, 102, "Deep RF hurts LHB; modest RHB edge"),
    ("Progressive Field",         97, 96, 98, "Slight overall suppression, balanced splits"),
    ("Tropicana Field",           96, 96, 96, "Dome neutral"),
    ("Comerica Park",             96, 92, 100, "Deep CF/LCF hurts LHB; balanced RHB"),

    # ─── Pitcher-friendly ────────────────────────────────────────────────
    ("PNC Park",                  95, 88, 102, "Deep RF (Clemente Wall) crushes LHB"),
    ("T-Mobile Park",              93, 90, 96, "Marine air + deep RCF suppress LHB"),
    ("Petco Park",                 92, 90, 94, "Marine air; slight LHB penalty"),
    ("loanDepot park",             90, 89, 91, "Marine air, dead ball; both sides suppressed"),
    ("Oakland Coliseum",           88, 85, 91, "Large foul territory + marine air; LHB hit harder"),

    # ─── Extreme pitcher parks ───────────────────────────────────────────
    ("Oracle Park",                82, 72, 90, "Triples Alley obliterates LHB; RHB roughly average"),
]


def get_seed_dataframe() -> pd.DataFrame:
    """Return the seed park factors as a DataFrame."""
    df = pd.DataFrame(
        PARK_FACTORS_SEED,
        columns=["venue", "hr_pf_overall", "hr_pf_lhb", "hr_pf_rhb", "notes"],
    )
    # Backwards compat: older code expects a `hr_park_factor` column
    df["hr_park_factor"] = df["hr_pf_overall"]
    return df


if __name__ == "__main__":
    df = get_seed_dataframe()
    print(f"\n  {len(df)} parks in seed data")
    print(f"  {'Venue':<32} {'Overall':>8} {'LHB':>6} {'RHB':>6}  Notes")
    print("  " + "-" * 80)
    for _, row in df.iterrows():
        print(f"  {row['venue']:<32} {row['hr_pf_overall']:>8} "
              f"{row['hr_pf_lhb']:>6} {row['hr_pf_rhb']:>6}  {row['notes']}")

    # Quick sanity checks
    print("\n  Split-vs-overall deltas (|LHB - RHB|):")
    df["spread"] = (df["hr_pf_lhb"] - df["hr_pf_rhb"]).abs()
    for _, row in df.sort_values("spread", ascending=False).head(8).iterrows():
        print(f"    {row['venue']:<32} LHB={row['hr_pf_lhb']} RHB={row['hr_pf_rhb']} spread={row['spread']}")
