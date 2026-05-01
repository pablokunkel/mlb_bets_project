#!/usr/bin/env python3
"""
backfill_features_v2.py — Add the 3 free Statcast features to raw_data.csv.

For each (player_id, season) and (pitcher_id, season) pair in raw_data.csv,
fetches:
  - xwoba_contact + pull_fb_pct      (per batter)
  - fb_pct_allowed                   (per pitcher)

Writes a new CSV (raw_data_v2.csv by default) with the original columns
plus the three new ones. Safe to re-run — uses features_v2.py's 24h cache.

Vegas implied totals can't be backfilled from free APIs, so those rows
stay null in the historical CSV. Vegas signal will start contributing
once it's wired up live and accumulates fresh data.

Usage:
    python backfill_features_v2.py                  # default in/out paths
    python backfill_features_v2.py --in raw_data.csv --out raw_data_v2.csv
    python backfill_features_v2.py --season 2026
    python backfill_features_v2.py --limit 100      # quick smoke test
"""

import argparse
import csv
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from features_v2 import (
    fetch_batter_advanced_stats,
    fetch_pitcher_batted_ball_profile,
)


def parse_int(v):
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def backfill(in_path: Path, out_path: Path, season: int, limit: int | None) -> None:
    print(f"Reading {in_path}...")
    rows = []
    with open(in_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    print(f"  Loaded {len(rows)} rows")

    if limit:
        rows = rows[:limit]
        print(f"  Limiting to {limit} rows for smoke test")

    # Collect unique batter and pitcher IDs
    batter_ids = sorted({parse_int(r.get("player_id")) for r in rows if parse_int(r.get("player_id"))})
    pitcher_ids = sorted({parse_int(r.get("opp_pitcher_id")) for r in rows if parse_int(r.get("opp_pitcher_id"))})
    print(f"  Unique batters: {len(batter_ids)}, unique pitchers: {len(pitcher_ids)}")

    # Fetch per-batter advanced stats
    print(f"\nFetching batter advanced stats (xwOBA on contact + pull-FB%)...")
    batter_adv: dict[int, dict] = {}
    for i, pid in enumerate(batter_ids, 1):
        adv = fetch_batter_advanced_stats(pid, season)
        if adv:
            batter_adv[pid] = adv
        if i % 25 == 0 or i == len(batter_ids):
            print(f"  [{i}/{len(batter_ids)}] {len(batter_adv)} with data")
        # Statcast can rate-limit on bursts; small sleep keeps us polite
        time.sleep(0.05)

    # Fetch per-pitcher batted-ball profile
    print(f"\nFetching pitcher batted-ball profile (FB%/GB% allowed)...")
    pitcher_bb: dict[int, dict] = {}
    for i, pid in enumerate(pitcher_ids, 1):
        bb = fetch_pitcher_batted_ball_profile(pid, season)
        if bb:
            pitcher_bb[pid] = bb
        if i % 25 == 0 or i == len(pitcher_ids):
            print(f"  [{i}/{len(pitcher_ids)}] {len(pitcher_bb)} with data")
        time.sleep(0.05)

    # Write enriched CSV
    out_fields = list(rows[0].keys()) + [
        "xwoba_contact", "pull_fb_pct", "fb_pct_allowed"
    ]
    # Dedup any duplicates that might already exist
    seen = set()
    out_fields = [f for f in out_fields if not (f in seen or seen.add(f))]

    print(f"\nWriting {out_path}...")
    enriched_count = defaultdict(int)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields)
        writer.writeheader()
        for r in rows:
            pid = parse_int(r.get("player_id"))
            ppid = parse_int(r.get("opp_pitcher_id"))
            adv = batter_adv.get(pid, {})
            bb = pitcher_bb.get(ppid, {})

            r["xwoba_contact"] = adv.get("xwoba_contact", "")
            r["pull_fb_pct"] = adv.get("pull_fb_pct", "")
            r["fb_pct_allowed"] = bb.get("fb_pct_allowed", "")

            if r["xwoba_contact"] != "":
                enriched_count["xwoba_contact"] += 1
            if r["pull_fb_pct"] != "":
                enriched_count["pull_fb_pct"] += 1
            if r["fb_pct_allowed"] != "":
                enriched_count["fb_pct_allowed"] += 1

            writer.writerow(r)

    print(f"\nEnrichment summary:")
    print(f"  xwoba_contact:  {enriched_count['xwoba_contact']}/{len(rows)} rows ({enriched_count['xwoba_contact']/max(len(rows),1):.1%})")
    print(f"  pull_fb_pct:    {enriched_count['pull_fb_pct']}/{len(rows)} rows ({enriched_count['pull_fb_pct']/max(len(rows),1):.1%})")
    print(f"  fb_pct_allowed: {enriched_count['fb_pct_allowed']}/{len(rows)} rows ({enriched_count['fb_pct_allowed']/max(len(rows),1):.1%})")
    print(f"\nDone -> {out_path}")


def main():
    ap = argparse.ArgumentParser(description="Backfill v2 Statcast features into raw_data.csv")
    ap.add_argument("--in", dest="in_path", default=str(Path(__file__).parent / "raw_data.csv"))
    ap.add_argument("--out", dest="out_path", default=str(Path(__file__).parent / "raw_data_v2.csv"))
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--limit", type=int, default=None, help="Smoke-test row limit")
    args = ap.parse_args()
    backfill(Path(args.in_path), Path(args.out_path), args.season, args.limit)


if __name__ == "__main__":
    main()
