#!/usr/bin/env python3
"""
backfill_from_csv.py — One-shot backfill of daily_picks + outcomes from raw_data.csv.

raw_data.csv contains per-player factor scores and outcomes for every batter on
each day's slate (the full board, not just the 8 picks). This script:

  1. Assigns tier (1/2/3) to each row using mlb_2025_tiers.py player_id lookups.
  2. Inserts every row into daily_picks with selected=0.
  3. For each date, picks the top 8 by composite (max 2 per game) and flips
     selected=1 on those — mirroring score_batters.select_top_picks behavior.
  4. Inserts one outcomes row per (date, batter, game) using ab + hr_count
     from the CSV.

After running this, run:
    python export_site_data.py --out mlb_hr_bet_site/data --days 60
to refresh the Netlify JSON.

Usage:
    python backfill_from_csv.py                  # default CSV + DB paths
    python backfill_from_csv.py --csv path.csv
    python backfill_from_csv.py --db path.db
    python backfill_from_csv.py --dry-run        # no writes, just summary

Re-running is safe: it deletes existing rows for the dates present in the CSV
before re-inserting, so you can iterate.
"""

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from etl.db import get_db, create_tables
from mlb_2025_tiers import TIER_1_BATTERS, TIER_2_BATTERS, TIER_3_BATTERS


# Build {player_id: (tier, tier_label)} from the tier modules.
TIER_LOOKUP: dict[int, tuple[int, str]] = {}
for p in TIER_1_BATTERS:
    TIER_LOOKUP[p["player_id"]] = (1, "T1-Chalk")
for p in TIER_2_BATTERS:
    TIER_LOOKUP[p["player_id"]] = (2, "T2-MidRange")
for p in TIER_3_BATTERS:
    TIER_LOOKUP[p["player_id"]] = (3, "T3-Longshot")


def parse_float(v: str) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def parse_int(v: str) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def select_top8_by_date(rows: list[dict], n: int = 8, max_per_game: int = 2) -> set[int]:
    """
    Return the set of row indices that should have selected=1.

    Mirrors score_batters.select_top_picks: sort by composite desc, walk the list,
    take a row if (a) we haven't taken this player_id before AND (b) the game_pk
    has fewer than max_per_game entries already.
    """
    indexed = sorted(
        enumerate(rows),
        key=lambda t: (t[1]["composite"] if t[1]["composite"] is not None else -1),
        reverse=True,
    )
    selected: set[int] = set()
    seen_players: set[int] = set()
    game_counts: dict[int, int] = defaultdict(int)
    for idx, row in indexed:
        if len(selected) >= n:
            break
        pid = row["player_id"]
        gpk = row["game_pk"]
        if pid is None or pid in seen_players:
            continue
        if gpk is not None and game_counts[gpk] >= max_per_game:
            continue
        selected.add(idx)
        seen_players.add(pid)
        if gpk is not None:
            game_counts[gpk] += 1
    return selected


def load_csv(csv_path: Path) -> list[dict]:
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            row = {
                "date": raw["date"],
                "game_pk": parse_int(raw.get("game_pk", "")),
                "player_id": parse_int(raw.get("player_id", "")),
                "name": raw.get("name", "").strip(),
                "team": raw.get("team", "").strip(),
                "bats": raw.get("bats", "").strip() or None,
                "venue": raw.get("venue", "").strip() or None,
                "opp_pitcher": raw.get("opp_pitcher", "").strip() or None,
                "opp_pitcher_id": parse_int(raw.get("opp_pitcher_id", "")),
                "ab": parse_int(raw.get("ab", "")),
                "hr_count": parse_int(raw.get("hr_count", "")) or 0,
                "hit_hr": parse_int(raw.get("hit_hr", "")) or 0,
                "power_score": parse_float(raw.get("power_score", "")),
                "matchup_score": parse_float(raw.get("matchup_score", "")),
                "park_score": parse_float(raw.get("park_score", "")),
                "form_score": parse_float(raw.get("form_score", "")),
                "weather_score": parse_float(raw.get("weather_score", "")),
                "composite": parse_float(raw.get("composite", "")),
            }
            tier_info = TIER_LOOKUP.get(row["player_id"])
            row["tier"] = tier_info[0] if tier_info else None
            row["tier_label"] = tier_info[1] if tier_info else None
            rows.append(row)
    return rows


def backfill(csv_path: Path, db_path: Path | None, dry_run: bool = False) -> None:
    print(f"Reading {csv_path} ...")
    rows = load_csv(csv_path)
    print(f"  Loaded {len(rows)} rows")

    by_date: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_date[r["date"]].append(r)
    dates = sorted(by_date.keys())
    print(f"  {len(dates)} unique dates: {dates[0]} → {dates[-1]}")

    # Compute selection per date
    selection_by_row: list[bool] = [False] * len(rows)
    base_idx_by_date: dict[str, int] = {}
    cursor = 0
    for d in dates:
        date_rows = by_date[d]
        # Find the original indices of these rows in the master rows list
        # (We rebuild a per-date selection then map back via composite.)
        # Simpler: just call select on date_rows and mark by date_rows objects.
        sel_indices = select_top8_by_date(date_rows)
        for i, dr in enumerate(date_rows):
            dr["_selected"] = i in sel_indices
        base_idx_by_date[d] = cursor
        cursor += len(date_rows)

    n_selected = sum(1 for r in rows if r.get("_selected"))
    n_tiered = sum(1 for r in rows if r["tier"] is not None)
    print(f"  Tiered rows: {n_tiered}/{len(rows)}")
    print(f"  Selected (top-8 per day): {n_selected} (~{n_selected/len(dates):.1f} per day)")

    if dry_run:
        print("\nDRY RUN — no DB writes")
        # Show a sample
        sample_date = dates[0]
        print(f"\nSample picks for {sample_date}:")
        for r in by_date[sample_date]:
            if r.get("_selected"):
                print(f"  {r['composite']:5.1f}  {r['name']:<22} {r['team']:<22} "
                      f"{'T'+str(r['tier']) if r['tier'] else 'T-':<3}  hr={r['hr_count']}")
        return

    print(f"\nWriting to DB ({db_path or 'default path'}) ...")
    conn = get_db(db_path)
    create_tables(conn)

    # Wipe existing rows for any date we're re-importing (idempotent re-run).
    placeholders = ",".join("?" * len(dates))
    conn.execute(f"DELETE FROM daily_picks WHERE date IN ({placeholders})", dates)
    conn.execute(f"DELETE FROM outcomes    WHERE date IN ({placeholders})", dates)
    conn.commit()

    # Insert daily_picks
    pick_sql = """
        INSERT INTO daily_picks (
            date, batter_id, batter_name, team, tier, tier_label,
            game_pk, opp_pitcher, opp_pitcher_id,
            composite, power_score, matchup_score, matchup_version,
            park_score, form_score, weather_score, lineup_score,
            batting_order, weight_config, selected, rank_in_board
        ) VALUES (?, ?, ?, ?, ?, ?,  ?, ?, ?,  ?, ?, ?, ?,  ?, ?, ?, ?,  ?, ?, ?, ?)
    """
    outcome_sql = """
        INSERT OR IGNORE INTO outcomes (
            date, batter_id, batter_name, game_pk, ab, hits, hr_count, rbi
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """

    n_picks_ins = 0
    n_outcomes_ins = 0
    for d in dates:
        date_rows = by_date[d]
        # rank_in_board: rank by composite desc within the date
        ranked = sorted(
            date_rows,
            key=lambda r: r["composite"] if r["composite"] is not None else -1,
            reverse=True,
        )
        rank_by_id = {id(r): i + 1 for i, r in enumerate(ranked)}

        for r in date_rows:
            if r["player_id"] is None:
                continue
            conn.execute(pick_sql, (
                r["date"], r["player_id"], r["name"], r["team"],
                r["tier"], r["tier_label"],
                r["game_pk"], r["opp_pitcher"], r["opp_pitcher_id"],
                r["composite"], r["power_score"], r["matchup_score"], "v1",
                r["park_score"], r["form_score"], r["weather_score"], None,
                None, "default",
                1 if r.get("_selected") else 0,
                rank_by_id[id(r)],
            ))
            n_picks_ins += 1

            if r["game_pk"] is not None:
                conn.execute(outcome_sql, (
                    r["date"], r["player_id"], r["name"], r["game_pk"],
                    r["ab"], None, r["hr_count"], None,
                ))
                n_outcomes_ins += 1

    conn.commit()
    print(f"  Inserted {n_picks_ins} daily_picks rows")
    print(f"  Inserted {n_outcomes_ins} outcomes rows")
    conn.close()
    print("Done.")


def main():
    ap = argparse.ArgumentParser(description="Backfill daily_picks + outcomes from raw_data.csv")
    ap.add_argument("--csv", default=str(Path(__file__).parent / "raw_data.csv"))
    ap.add_argument("--db", default=None, help="DB path (default: from etl.db)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    backfill(Path(args.csv), Path(args.db) if args.db else None, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
