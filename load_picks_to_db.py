#!/usr/bin/env python3
"""
load_picks_to_db.py — Persist a day's picks JSON to the SQLite DB.

generate_picks.py produces a JSON file (default: results/picks_<DATE>.json)
containing both the 8-pick card and the full scored board. This script reads
that JSON and inserts every board row into daily_picks, with selected=1 set
on the rows that match the 8 picks in the card.

Idempotent: deletes any existing daily_picks rows for the date before
re-inserting, so it's safe to re-run.

Usage:
    python load_picks_to_db.py                       # today, default JSON path
    python load_picks_to_db.py --date 2026-04-29
    python load_picks_to_db.py --json path/to/picks.json
    python load_picks_to_db.py --db custom.db
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from etl.db import get_db, create_tables


def resolve_json_path(date_str: str, explicit: str | None) -> Path:
    """Find the picks JSON for the given date.

    generate_picks.py writes to `<project>/../results/picks_<DATE>.json` by
    default (note: parent of the project dir). Fall back to a few sensible
    alternates so we work whether run_daily.bat passes --output or not.
    """
    if explicit:
        return Path(explicit)

    candidates = [
        Path(__file__).parent.parent / "results" / f"picks_{date_str}.json",
        Path(__file__).parent / "results" / f"picks_{date_str}.json",
        Path.home() / "Desktop" / "HR-Picks" / f"picks_{date_str}.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(
        f"No picks JSON found for {date_str}. Tried:\n  "
        + "\n  ".join(str(c) for c in candidates)
    )


def load_picks(json_path: Path, db_path: Path | None = None) -> tuple[int, int]:
    """Insert the day's full board into daily_picks. Returns (n_inserted, n_selected)."""

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    date_str = data["date"]
    card = data.get("picks", [])
    board = data.get("full_board", [])
    weight_config = data.get("scoring_config", "default")

    if not board:
        # Older JSON without full_board — treat the 8-pick card as the entire board.
        board = card
        print(f"  [warn] No full_board in JSON; loading card-only ({len(card)} rows).")

    # Build a set of (player_id, game_pk) keys identifying the selected card.
    # Falls back to name+game_pk if player_id is missing in older JSONs.
    selected_keys: set[tuple] = set()
    for p in card:
        pid = p.get("player_id") or 0
        gpk = p.get("game_pk")
        if pid:
            selected_keys.add(("id", pid, gpk))
        else:
            selected_keys.add(("name", p.get("name", ""), gpk))

    def is_selected(row: dict) -> bool:
        pid = row.get("player_id") or 0
        gpk = row.get("game_pk")
        if pid and ("id", pid, gpk) in selected_keys:
            return True
        if ("name", row.get("name", ""), gpk) in selected_keys:
            return True
        # Some generators flag selected=true on the board row itself.
        return bool(row.get("selected", False))

    conn = get_db(db_path)
    create_tables(conn)

    # Idempotent: clear and re-insert this date's picks.
    conn.execute("DELETE FROM daily_picks WHERE date = ?", (date_str,))
    conn.commit()

    pick_sql = """
        INSERT INTO daily_picks (
            date, batter_id, batter_name, team, tier, tier_label,
            game_pk, opp_pitcher, opp_pitcher_id,
            composite, power_score, matchup_score, matchup_version,
            park_score, form_score, weather_score, lineup_score,
            batting_order, weight_config, selected, rank_in_board
        ) VALUES (?, ?, ?, ?, ?, ?,  ?, ?, ?,  ?, ?, ?, ?,  ?, ?, ?, ?,  ?, ?, ?, ?)
    """

    # pick_inputs row insertion — captures the raw signals fed into each
    # factor, so the dashboard can decompose what drove each score.
    # Idempotent (INSERT OR REPLACE on PRIMARY KEY (date, batter_id)).
    pick_inputs_sql = """
        INSERT OR REPLACE INTO pick_inputs (
            date, batter_id,
            barrel_pct, exit_velo, hr_fb_pct, iso, xwoba_contact, pull_fb_pct,
            recent_hr_14d, recent_barrel_pct_14d, ev_trend_14d,
            pitcher_hr_per_9, pitcher_era, pitcher_hh_pct, pitcher_k_per_9, pitcher_fb_pct_allowed,
            woba_vs_hand, archetype_similarity, vegas_implied_total, platoon_advantage,
            hr_park_factor,
            temperature_f, wind_mph, wind_direction_deg, humidity_pct, is_dome,
            batting_order
        ) VALUES (?, ?,  ?, ?, ?, ?, ?, ?,  ?, ?, ?,  ?, ?, ?, ?, ?,  ?, ?, ?, ?,  ?,  ?, ?, ?, ?, ?,  ?)
    """

    # Clear pick_inputs for the date too — re-runs should start clean.
    try:
        conn.execute("DELETE FROM pick_inputs WHERE date = ?", (date_str,))
    except Exception:
        # Table may not exist yet on older DBs that haven't been migrated.
        # create_tables() above should have added it; if it failed, skip.
        pass

    n_inserted = 0
    n_selected = 0
    n_inputs = 0
    for i, row in enumerate(board, start=1):
        sel = 1 if is_selected(row) else 0
        if sel:
            n_selected += 1

        card_match = None
        for p in card:
            same_id = row.get("player_id") and p.get("player_id") == row.get("player_id")
            same_name = (not row.get("player_id")) and p.get("name") == row.get("name")
            if same_id or same_name:
                card_match = p
                break

        def field(key, default=None):
            if card_match and card_match.get(key) not in (None, ""):
                return card_match.get(key, default)
            return row.get(key, default)

        conn.execute(pick_sql, (
            date_str,
            row.get("player_id") or 0,
            row.get("name"),
            row.get("team"),
            row.get("tier"),
            row.get("tier_label"),
            row.get("game_pk") or field("game_pk"),
            row.get("opp_pitcher") or field("opp_pitcher"),
            row.get("opp_pitcher_id") or field("opp_pitcher_id") or 0,
            row.get("composite"),
            row.get("power_score"),
            row.get("matchup_score"),
            field("matchup_version", "v1"),
            row.get("park_score"),
            row.get("form_score"),
            row.get("weather_score"),
            row.get("lineup_score"),
            (str(row.get("batting_order")) if row.get("batting_order") is not None else None),
            weight_config,
            sel,
            i,
        ))
        n_inserted += 1

        # Persist raw factor inputs if generate_picks emitted them.
        inputs = row.get("inputs") or field("inputs", {}) or {}
        pid = row.get("player_id") or 0
        if inputs and pid:
            try:
                bo = inputs.get("batting_order")
                bo_int = bo if isinstance(bo, int) else None
                conn.execute(pick_inputs_sql, (
                    date_str, pid,
                    inputs.get("barrel_pct"),
                    inputs.get("exit_velo"),
                    inputs.get("hr_fb_pct"),
                    inputs.get("iso"),
                    inputs.get("xwoba_contact"),
                    inputs.get("pull_fb_pct"),
                    inputs.get("recent_hr_14d"),
                    inputs.get("recent_barrel_pct_14d"),
                    inputs.get("ev_trend_14d"),
                    inputs.get("pitcher_hr_per_9"),
                    inputs.get("pitcher_era"),
                    inputs.get("pitcher_hh_pct"),
                    inputs.get("pitcher_k_per_9"),
                    inputs.get("pitcher_fb_pct_allowed"),
                    inputs.get("woba_vs_hand"),
                    inputs.get("archetype_similarity"),
                    inputs.get("vegas_implied_total"),
                    inputs.get("platoon_advantage"),
                    inputs.get("hr_park_factor"),
                    inputs.get("temperature_f"),
                    inputs.get("wind_mph"),
                    inputs.get("wind_direction_deg"),
                    inputs.get("humidity_pct"),
                    inputs.get("is_dome"),
                    bo_int,
                ))
                n_inputs += 1
            except Exception:
                pass

    conn.commit()
    conn.close()
    if n_inputs > 0:
        print(f"  Persisted {n_inputs} pick_inputs rows")
    return n_inserted, n_selected


def main():
    ap = argparse.ArgumentParser(description="Load a day's picks JSON into daily_picks")
    ap.add_argument("--date", default="today", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--json", default=None, help="Explicit path to picks JSON")
    ap.add_argument("--db", default=None, help="Custom DB path")
    args = ap.parse_args()

    date_str = (
        datetime.now().strftime("%Y-%m-%d")
        if args.date == "today"
        else args.date
    )

    json_path = resolve_json_path(date_str, args.json)
    print(f"Loading picks for {date_str} from {json_path} ...")
    n_ins, n_sel = load_picks(json_path, Path(args.db) if args.db else None)
    print(f"  Inserted {n_ins} board rows ({n_sel} flagged selected=1)")


if __name__ == "__main__":
    main()
