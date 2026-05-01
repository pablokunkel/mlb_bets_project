#!/usr/bin/env python3
"""
backfill_features_v2_bulk.py — Fast Statcast backfill via Savant leaderboard CSVs.

Where backfill_features_v2.py does per-player API calls (slow, ~5s each),
this version pulls everything in 2 HTTP calls against Baseball Savant's
leaderboard CSV endpoints. Trade-off: less granular than per-player, and
pull-FB% isn't on these leaderboards (stays null in historical data; the
live path still computes it via per-player Statcast).

Pulls:
  - Batter xwOBA (full-season expected wOBA — proxy for xwOBA on contact)
    from /leaderboard/expected_statistics
  - Pitcher FBLD% (fly-ball + line-drive percent) from /leaderboard/statcast
    when the pitcher endpoint is targeted

Output: raw_data_v2.csv with three new columns:
  xwoba_contact, pull_fb_pct (always null here), fb_pct_allowed

Usage:
    python backfill_features_v2_bulk.py --season 2026
"""

import argparse
import csv
import io
import sys
from pathlib import Path

import pandas as pd
import requests


SAVANT_BASE = "https://baseballsavant.mlb.com/leaderboard"


def fetch_batter_xwoba(season: int) -> dict[int, float]:
    """Pull every batter's xwOBA from Savant's expected_statistics CSV."""
    url = f"{SAVANT_BASE}/expected_statistics"
    params = {
        "type": "batter", "year": str(season), "min": "1", "csv": "true",
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    out = {}
    for _, row in df.iterrows():
        pid = row.get("player_id")
        xwoba = row.get("est_woba")
        if pd.notna(pid) and pd.notna(xwoba):
            out[int(pid)] = float(xwoba)
    return out


def fetch_pitcher_batted_ball(season: int) -> dict[int, float]:
    """
    Pull FBLD% (fly + line-drive percentage allowed) for every pitcher.
    The 'fbld' column in /leaderboard/statcast represents the share of
    batted balls that are fly balls or line drives — a strong proxy
    for fly-ball% allowed since LD/FB carry more HR risk than GB.

    NOTE: Savant requires type=pitcher (not player_type=pitcher) here.
    """
    url = f"{SAVANT_BASE}/statcast"
    params = {
        "year": str(season), "abs": "10", "csv": "true",
        "type": "pitcher", "min": "10",
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    out = {}
    for _, row in df.iterrows():
        pid = row.get("player_id")
        fbld = row.get("fbld")
        if pd.notna(pid) and pd.notna(fbld):
            pct = float(fbld)
            if pct < 1:
                pct *= 100
            out[int(pid)] = round(pct, 1)
    return out


def parse_int(v):
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def backfill(in_path: Path, out_path: Path, season: int) -> None:
    print(f"Reading {in_path}...")
    with open(in_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    print(f"  Loaded {len(rows)} rows")

    print(f"\nFetching Savant expected_statistics (xwOBA) for {season}...")
    xwoba = fetch_batter_xwoba(season)
    print(f"  Got xwOBA for {len(xwoba)} batters")

    print(f"\nFetching Savant statcast leaderboard (FBLD%) for {season}...")
    fbld = fetch_pitcher_batted_ball(season)
    print(f"  Got FBLD% for {len(fbld)} pitchers")

    # Coverage stats against rows in the CSV
    bid_set = {parse_int(r.get("player_id")) for r in rows if parse_int(r.get("player_id"))}
    pid_set = {parse_int(r.get("opp_pitcher_id")) for r in rows if parse_int(r.get("opp_pitcher_id"))}
    bid_match = bid_set & set(xwoba.keys())
    pid_match = pid_set & set(fbld.keys())
    print(f"\nCoverage:")
    print(f"  xwOBA:    {len(bid_match)}/{len(bid_set)} batters in CSV ({len(bid_match)/max(len(bid_set),1):.1%})")
    print(f"  FB%/LD%:  {len(pid_match)}/{len(pid_set)} pitchers in CSV ({len(pid_match)/max(len(pid_set),1):.1%})")

    # Write enriched CSV
    out_fields = list(rows[0].keys()) + ["xwoba_contact", "pull_fb_pct", "fb_pct_allowed"]
    seen = set()
    out_fields = [f for f in out_fields if not (f in seen or seen.add(f))]

    print(f"\nWriting {out_path}...")
    n_xwoba_filled = n_fbld_filled = 0
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields)
        writer.writeheader()
        for r in rows:
            bid = parse_int(r.get("player_id"))
            ppid = parse_int(r.get("opp_pitcher_id"))
            r["xwoba_contact"] = xwoba.get(bid, "")
            r["pull_fb_pct"] = ""  # not populated in bulk path; live still computes it
            r["fb_pct_allowed"] = fbld.get(ppid, "")
            if r["xwoba_contact"] != "":
                n_xwoba_filled += 1
            if r["fb_pct_allowed"] != "":
                n_fbld_filled += 1
            writer.writerow(r)

    print(f"  xwoba_contact:  {n_xwoba_filled}/{len(rows)} rows ({n_xwoba_filled/max(len(rows),1):.1%})")
    print(f"  fb_pct_allowed: {n_fbld_filled}/{len(rows)} rows ({n_fbld_filled/max(len(rows),1):.1%})")
    print(f"  pull_fb_pct:    0/{len(rows)} rows (bulk path can't fetch — live still computes)")
    print(f"\nDone -> {out_path}")


def main():
    ap = argparse.ArgumentParser(description="Fast Statcast backfill via Savant leaderboard CSVs")
    ap.add_argument("--in", dest="in_path", default=str(Path(__file__).parent / "raw_data.csv"))
    ap.add_argument("--out", dest="out_path", default=str(Path(__file__).parent / "raw_data_v2.csv"))
    ap.add_argument("--season", type=int, default=2026)
    args = ap.parse_args()
    backfill(Path(args.in_path), Path(args.out_path), args.season)


if __name__ == "__main__":
    main()
