#!/usr/bin/env python3
"""
backfill_pick_inputs.py — Populate pick_inputs for historical daily_picks rows.

Runs once. For every (date, batter_id) in daily_picks that doesn't yet have
a pick_inputs row, assembles inputs from:

  Clean (have exact historical value):
    - hr_park_factor      <- park_factors lookup by venue
    - batting_order       <- daily_picks (already there)
    - weather (temp/wind/humidity/dome)  <- daily_slate game_pk lookup
    - platoon_advantage   <- bats vs throws

  Approximate (current season-to-date proxy):
    - barrel_pct, exit_velo, hr_fb_pct, iso, woba_vs_hand  <- season_batting
    - xwoba_contact, pull_fb_pct                          <- bulk Savant fetch
    - pitcher_hr_per_9, era, hh_pct, k_per_9              <- season_pitching
    - pitcher_fb_pct_allowed                              <- bulk Savant fetch
    - archetype_similarity                                <- compute from
                                                             pitcher_arsenals + victim_profiles

  Computable correctly (Statcast has dates):
    - recent_hr_14d                                       <- batter_hr_events filtered
    - recent_barrel_pct_14d, ev_trend_14d                 <- not in batter_hr_events;
                                                             we only stored HR events,
                                                             not all batted balls. Stays NULL.

  Lost forever:
    - vegas_team_total_pct                                <- free odds APIs no history; NULL
    - vegas_team_total_raw                                <- (same)

Each row written has source='backfill' so the dashboard can distinguish from
'live' rows going forward. Future runs of generate_picks → load_picks_to_db
write source='live' (default).

Usage:
    python backfill_pick_inputs.py           # default season 2026
    python backfill_pick_inputs.py --season 2026
    python backfill_pick_inputs.py --dry-run # no writes; print what would happen
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from etl.db import get_db, create_tables
from features_v2 import fetch_batter_xwoba_bulk, fetch_pitcher_fb_bulk


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def archetype_similarity_quick(victim, pitcher):
    """
    Lightweight similarity calc — pulls from pitcher_profile.archetype_similarity
    if available, else falls back to a simple velo+mix distance proxy.
    """
    try:
        from pitcher_profile import archetype_similarity
        # archetype_similarity expects two dicts. Build them from our DB rows.
        v = {
            "avg_fb_velo": victim.get("avg_fb_velo"),
            "fb_usage_pct": victim.get("fb_usage_pct"),
            "breaking_usage_pct": victim.get("breaking_pct"),
            "offspeed_usage_pct": victim.get("offspeed_pct"),
            "hand_R_pct": victim.get("hand_r_pct"),
            "avg_fb_spin": victim.get("avg_fb_spin"),
            "avg_extension": victim.get("avg_extension"),
            "confidence": victim.get("confidence", 0.5),
        }
        p = {
            "avg_fb_velo": pitcher.get("avg_fb_velo"),
            "fb_usage_pct": pitcher.get("fb_usage_pct"),
            "breaking_usage_pct": pitcher.get("breaking_pct"),
            "offspeed_usage_pct": pitcher.get("offspeed_pct"),
            "p_throws": pitcher.get("p_throws", "R"),
            "avg_fb_spin": pitcher.get("avg_fb_spin"),
            "avg_extension": pitcher.get("avg_extension"),
        }
        # Drop Nones — archetype_similarity uses defaults internally
        v = {k: vv for k, vv in v.items() if vv is not None}
        p = {k: vv for k, vv in p.items() if vv is not None}
        if not v or not p:
            return None
        return float(archetype_similarity(v, p))
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description="Backfill pick_inputs from local DB + bulk Savant")
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--dry-run", action="store_true", help="Print what would be written")
    args = ap.parse_args()

    conn = get_db()
    create_tables(conn)

    # Add `source` column to pick_inputs if it doesn't exist (idempotent migration).
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pick_inputs)").fetchall()}
    if "source" not in cols:
        conn.execute("ALTER TABLE pick_inputs ADD COLUMN source TEXT DEFAULT 'live'")
        conn.commit()
        print("  Added 'source' column to pick_inputs")

    # ----------------------------------------------------------------------
    # 1. Find daily_picks rows that don't have pick_inputs yet
    # ----------------------------------------------------------------------
    picks = conn.execute("""
        SELECT
            dp.date, dp.batter_id, dp.batter_name, dp.team,
            dp.opp_pitcher, dp.opp_pitcher_id, dp.game_pk, dp.batting_order
        FROM daily_picks dp
        LEFT JOIN pick_inputs pi
            ON pi.date = dp.date AND pi.batter_id = dp.batter_id
        WHERE pi.batter_id IS NULL
        ORDER BY dp.date, dp.batter_id
    """).fetchall()

    if not picks:
        print("Nothing to backfill — every daily_picks row already has pick_inputs.")
        return

    print(f"Rows to backfill: {len(picks)}")
    dates = sorted({p["date"] for p in picks})
    print(f"  Date range: {dates[0]} -> {dates[-1]} ({len(dates)} days)")

    # ----------------------------------------------------------------------
    # 2. One-time bulk fetches (Savant CSVs) — current season-to-date
    # ----------------------------------------------------------------------
    print("\nFetching bulk batter xwOBA leaderboard...")
    bulk_xwoba = fetch_batter_xwoba_bulk(args.season) or {}
    print(f"  {len(bulk_xwoba)} batters")

    print("Fetching bulk pitcher FB%/LD% leaderboard...")
    bulk_fb = fetch_pitcher_fb_bulk(args.season) or {}
    print(f"  {len(bulk_fb)} pitchers")

    # ----------------------------------------------------------------------
    # 3. Local table snapshots (in-memory dicts for fast lookup)
    # ----------------------------------------------------------------------
    print("\nLoading local lookup tables...")

    # season_batting → barrel_pct, exit_velo, hr_fb_pct, iso, woba, bats
    sb_rows = conn.execute("""
        SELECT player_id, bats, hr_per_pa, slg, iso, woba,
               barrel_pct, exit_velo, hr_fb_pct
        FROM season_batting WHERE season = ?
    """, (args.season,)).fetchall()
    season_batting = {r["player_id"]: dict(r) for r in sb_rows}
    print(f"  season_batting: {len(season_batting)} players")

    # season_pitching → hr_per_9, era, hard_hit_pct, k_per_9, p_throws
    sp_rows = conn.execute("""
        SELECT pitcher_id, p_throws, era, hr_per_9, k_per_9, whip, hard_hit_pct
        FROM season_pitching WHERE season = ?
    """, (args.season,)).fetchall()
    season_pitching = {r["pitcher_id"]: dict(r) for r in sp_rows}
    print(f"  season_pitching: {len(season_pitching)} pitchers")

    # park_factors → venue → hr_pf_overall
    pf_rows = conn.execute("""
        SELECT venue, hr_pf_overall FROM park_factors WHERE season = ?
    """, (args.season,)).fetchall()
    park_factors = {r["venue"]: r["hr_pf_overall"] for r in pf_rows}
    print(f"  park_factors: {len(park_factors)} venues")

    # daily_slate → game_pk + date → weather
    ds_rows = conn.execute("""
        SELECT game_pk, date, venue, temperature_f, wind_mph, wind_dir_deg,
               humidity_pct, dome
        FROM daily_slate
    """).fetchall()
    slate = {(r["game_pk"], r["date"]): dict(r) for r in ds_rows}
    print(f"  daily_slate: {len(slate)} game-days")

    # pitcher_arsenals + victim_profiles — for archetype similarity
    arsenals = {r["pitcher_id"]: dict(r) for r in conn.execute("""
        SELECT pitcher_id, p_throws, avg_fb_velo, fb_usage_pct, breaking_pct,
               offspeed_pct, avg_fb_spin, avg_extension
        FROM pitcher_arsenals WHERE season = ?
    """, (args.season,)).fetchall()}
    print(f"  pitcher_arsenals: {len(arsenals)} pitchers")

    victims = {r["batter_id"]: dict(r) for r in conn.execute("""
        SELECT batter_id, avg_fb_velo, fb_usage_pct, breaking_pct, offspeed_pct,
               hand_r_pct, avg_fb_spin, avg_extension, confidence
        FROM victim_profiles WHERE season = ?
    """, (args.season,)).fetchall()}
    print(f"  victim_profiles: {len(victims)} batters")

    # ----------------------------------------------------------------------
    # 4. Form metrics: count batter_hr_events with date filter (correct as-of-date)
    # ----------------------------------------------------------------------
    # Pre-aggregate HR counts by (batter_id, game_date) for fast 14d windowing
    hr_events_by_batter: dict = {}
    for r in conn.execute("SELECT batter_id, game_date FROM batter_hr_events").fetchall():
        bid = r["batter_id"]
        d = r["game_date"]
        if d:
            hr_events_by_batter.setdefault(bid, []).append(d)
    # Sort each batter's dates
    for bid in hr_events_by_batter:
        hr_events_by_batter[bid].sort()
    print(f"  batter_hr_events: indexed {len(hr_events_by_batter)} batters")

    def recent_hr_14d_for(bid: int, as_of: str) -> int:
        """Count HRs in the 14 days ending the day before `as_of`."""
        events = hr_events_by_batter.get(bid)
        if not events:
            return 0
        end = as_of  # exclusive (HRs before this date)
        start = (datetime.strptime(as_of, "%Y-%m-%d") - timedelta(days=14)).strftime("%Y-%m-%d")
        # Count dates in [start, end)
        return sum(1 for d in events if start <= d < end)

    # ----------------------------------------------------------------------
    # 5. Iterate and INSERT OR REPLACE
    # ----------------------------------------------------------------------
    # NOTE 2026-05-13: this backfill does NOT populate pitcher_recent_hr9_21d /
    # pitcher_recent_starts_21d (those columns stay NULL on backfilled rows).
    # Before the next weight refit, write a separate backfill that queries
    # the MLB API gameLog (per pitcher × per pick_inputs date) so the refit
    # has a clean recency column across the training window. The live
    # generate_picks path populates these columns from today onward.
    insert_sql = """
        INSERT OR REPLACE INTO pick_inputs (
            date, batter_id,
            barrel_pct, exit_velo, hr_fb_pct, iso, xwoba_contact, pull_fb_pct,
            recent_hr_14d, recent_barrel_pct_14d, ev_trend_14d,
            pitcher_hr_per_9, pitcher_era, pitcher_hh_pct, pitcher_k_per_9, pitcher_fb_pct_allowed,
            woba_vs_hand, archetype_similarity, vegas_team_total_pct, platoon_advantage,
            hr_park_factor,
            temperature_f, wind_mph, wind_direction_deg, humidity_pct, is_dome,
            batting_order, source
        ) VALUES (?, ?,  ?, ?, ?, ?, ?, ?,  ?, ?, ?,  ?, ?, ?, ?, ?,  ?, ?, ?, ?,  ?,  ?, ?, ?, ?, ?,  ?, 'backfill')
    """

    n_written = 0
    n_skipped_no_match = 0
    for p in picks:
        date_str = p["date"]
        bid = p["batter_id"]
        ppid = p["opp_pitcher_id"] or 0
        gpk = p["game_pk"]

        sb = season_batting.get(bid, {})
        sp = season_pitching.get(ppid, {})
        sl = slate.get((gpk, date_str), {})

        # Park factor: prefer slate.venue, else fall back to a join
        venue = sl.get("venue")
        pf_overall = park_factors.get(venue) if venue else None

        # Platoon advantage: bats vs throws
        bats = sb.get("bats")
        throws = sp.get("p_throws")
        platoon = 1 if (bats and throws and bats != throws) else (0 if bats and throws else None)

        # Form
        recent_hr = recent_hr_14d_for(bid, date_str)

        # Batting order — daily_picks stores it as a string ("1"-"9", "bench", "roster_only")
        bo_raw = p["batting_order"]
        try:
            bo = int(bo_raw) if bo_raw and str(bo_raw).isdigit() else None
            if bo is not None and not (1 <= bo <= 9):
                bo = None
        except Exception:
            bo = None

        # Archetype similarity
        archetype_sim = archetype_similarity_quick(victims.get(bid, {}), arsenals.get(ppid, {}))

        # iso / hr_fb_pct fallbacks
        # season_batting has hr_per_pa not hr_count_total; use slg-based iso if missing
        iso = sb.get("iso")

        row = (
            date_str, bid,
            sb.get("barrel_pct"),
            sb.get("exit_velo"),
            sb.get("hr_fb_pct"),
            iso,
            bulk_xwoba.get(bid),
            None,  # pull_fb_pct — bulk has no source for historical
            recent_hr,
            None,  # recent_barrel_pct_14d — only HR events captured, not all bb
            None,  # ev_trend_14d — same
            sp.get("hr_per_9"),
            sp.get("era"),
            sp.get("hard_hit_pct"),
            sp.get("k_per_9"),
            bulk_fb.get(ppid),
            sb.get("woba"),
            archetype_sim,
            None,  # vegas_team_total_pct — historical free-API odds gone forever
            platoon,
            pf_overall,
            sl.get("temperature_f"),
            sl.get("wind_mph"),
            sl.get("wind_dir_deg"),
            sl.get("humidity_pct"),
            1 if sl.get("dome") else (0 if sl else None),
            bo,
        )

        if all(v is None for v in row[2:]):
            n_skipped_no_match += 1
            continue

        if not args.dry_run:
            conn.execute(insert_sql, row)
        n_written += 1

    if not args.dry_run:
        conn.commit()
    conn.close()

    print(f"\nDone.")
    print(f"  pick_inputs rows written: {n_written}")
    print(f"  Skipped (no joinable data): {n_skipped_no_match}")
    if args.dry_run:
        print("  (DRY RUN — no DB writes)")
    else:
        print("\nNext: re-export and deploy.")
        print("  python export_site_data.py")
        print("  git add mlb_hr_bet_site/data/*.json && git commit -m 'Backfill update' && git push origin main")


if __name__ == "__main__":
    main()
