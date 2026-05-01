#!/usr/bin/env python3
"""
normalize_team_names.py — Convert daily_picks.team from full names to
3-letter abbrevs in place. Idempotent (rows already in abbrev form are
left alone).

Why: backfill_from_csv.py inherited full team names from raw_data.csv
("Cincinnati Reds"), while generate_picks.py + load_picks_to_db.py emit
3-letter abbrevs ("CIN"). The dashboard renders both fine, but the
history view looks inconsistent. Easier to normalize the smaller set
(20 days from raw_data) than to inject a name lookup at every read site.

Usage:
    python normalize_team_names.py            # writes
    python normalize_team_names.py --dry-run  # preview only
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from etl.db import get_db


# Same map generate_picks.py uses (TEAM_ABBREV_TO_FULL inverted).
FULL_TO_ABBREV = {
    "Arizona Diamondbacks": "ARI", "Atlanta Braves": "ATL",
    "Baltimore Orioles": "BAL", "Boston Red Sox": "BOS",
    "Chicago Cubs": "CHC", "Chicago White Sox": "CWS",
    "Cincinnati Reds": "CIN", "Cleveland Guardians": "CLE",
    "Colorado Rockies": "COL", "Detroit Tigers": "DET",
    "Houston Astros": "HOU", "Kansas City Royals": "KC",
    "Los Angeles Angels": "LAA", "Los Angeles Dodgers": "LAD",
    "Miami Marlins": "MIA", "Milwaukee Brewers": "MIL",
    "Minnesota Twins": "MIN", "New York Mets": "NYM",
    "New York Yankees": "NYY", "Oakland Athletics": "OAK",
    "Athletics": "OAK",  # alt name some feeds use
    "Philadelphia Phillies": "PHI", "Pittsburgh Pirates": "PIT",
    "San Diego Padres": "SD", "San Francisco Giants": "SF",
    "Seattle Mariners": "SEA", "St. Louis Cardinals": "STL",
    "Tampa Bay Rays": "TB", "Texas Rangers": "TEX",
    "Toronto Blue Jays": "TOR", "Washington Nationals": "WSH",
}

VALID_ABBREVS = set(FULL_TO_ABBREV.values())


def main():
    ap = argparse.ArgumentParser(description="Normalize daily_picks.team to abbrev")
    ap.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = ap.parse_args()

    conn = get_db()

    # Show current distribution
    rows = conn.execute(
        "SELECT team, COUNT(*) AS n FROM daily_picks GROUP BY team ORDER BY n DESC"
    ).fetchall()
    print(f"Current daily_picks.team distribution ({len(rows)} unique values):")
    full_count = abbrev_count = unknown_count = 0
    unknowns = []
    for r in rows:
        t = r["team"]
        if t in VALID_ABBREVS:
            abbrev_count += r["n"]
        elif t in FULL_TO_ABBREV:
            full_count += r["n"]
        else:
            unknown_count += r["n"]
            unknowns.append((t, r["n"]))

    print(f"  Already abbreviated: {abbrev_count} rows")
    print(f"  Full name (will convert): {full_count} rows")
    print(f"  Unknown (skipping): {unknown_count} rows")
    if unknowns:
        print("  Unknown values:")
        for t, n in unknowns:
            print(f"    {t!r:<40} {n} rows")

    if args.dry_run:
        print("\n--dry-run: no writes performed.")
        return

    # Migrate
    print("\nMigrating full names -> abbrevs...")
    total_changed = 0
    for full, abbrev in FULL_TO_ABBREV.items():
        cur = conn.execute(
            "UPDATE daily_picks SET team = ? WHERE team = ?",
            (abbrev, full),
        )
        if cur.rowcount > 0:
            print(f"  {full:<30} -> {abbrev}  ({cur.rowcount} rows)")
            total_changed += cur.rowcount
    conn.commit()
    print(f"\nTotal rows updated: {total_changed}")

    # Verify
    after = conn.execute(
        "SELECT COUNT(DISTINCT team) AS n_teams, COUNT(*) AS total FROM daily_picks"
    ).fetchone()
    print(f"After: {after['n_teams']} unique team values across {after['total']} rows.")

    conn.close()
    print("\nNext: re-export and deploy:")
    print("  python export_site_data.py")
    print("  git add mlb_hr_bet_site/data/*.json && git commit -m 'Update' && git push origin main")


if __name__ == "__main__":
    main()
