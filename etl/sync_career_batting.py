#!/usr/bin/env python3
"""
sync_career_batting.py — Populate the `career_batting` table.

Fetches career hitting totals from the MLB Stats API for every player
present in `season_batting` or `daily_picks`. One row per player.

This is the data feed for the USE_CAREER_PRIOR Bayesian shrinkage path
in score_batters.score_power (see commit [3/4] of the prototype). The
shrinkage formula pulls each current-season rate toward the player's
career rate with weight inversely proportional to current-season PA:

    shrunk = (current_pa * current_rate + k * career_rate) / (current_pa + k)

So a slow-start veteran's barrel% / HR-per-PA / ISO etc. get pulled
toward their career mean early in the season, then drift back to
purely current as the sample grows.

## Coverage

- Stats API gives counting stats (PA/AB/HR/H) + rate stats
  (AVG/SLG/OBP/ISO) for every player back to ~1900s. Career-wide.
- Statcast metrics (barrel_pct / exit_velo / hr_fb_pct) are 2015+ only
  and the Stats API does NOT expose them at career-aggregate. For
  Statcast-era players we'd need a separate pybaseball pull. For the
  prototype, those columns stay NULL and the shrinkage falls back to
  current-only on those metrics.

## Usage

    # Sync all players who appear in season_batting or daily_picks
    python -m etl.sync_career_batting

    # Sync a specific player_id (debugging)
    python -m etl.sync_career_batting --player-id 660271

    # Force re-fetch even if we have a recent row (default: skip if <30d old)
    python -m etl.sync_career_batting --force

## Cadence

Run quarterly (or once per season). Career stats don't change daily.
For active players, totals tick up but the SHRINKAGE effect of
career_pa=6500+1 vs 6500 is negligible — refresh on a slow cadence.

## Idempotent

INSERT OR REPLACE on player_id. Safe to re-run; the latest row wins.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from etl.db import get_db, create_tables, log_etl_start, log_etl_complete, log_etl_fail

MLB_API = "https://statsapi.mlb.com/api/v1"
SKIP_IF_FRESH_DAYS = 30


def _safe_float(v) -> float | None:
    """Convert MLB-style stat strings ('.268' / '0.268' / '—') to float, or None."""
    if v is None or v == "" or v == "-.---":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _safe_int(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def fetch_career_for_player(player_id: int) -> dict | None:
    """
    Fetch career hitting stats for one player from the MLB Stats API.

    Returns a dict ready for `INSERT OR REPLACE INTO career_batting`,
    or None on any error. Stat keys aligned to the table column names.
    """
    # /people/{id}?hydrate=stats(group=[hitting],type=[career]) returns
    # bio fields + the career-aggregate stat block in a single round-trip.
    # Multi-type hydrate (career + yearByYear together) returns 500, so
    # we use single-type and derive seasons_played from mlbDebutDate.
    url = f"{MLB_API}/people/{player_id}"
    params = {"hydrate": "stats(group=[hitting],type=[career])"}
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [career] fetch failed for player_id={player_id}: {e}")
        return None

    people = data.get("people", [])
    if not people:
        return None
    person = people[0]
    name = person.get("fullName", "")

    career_stat = None
    for stat_block in person.get("stats", []) or []:
        for split in stat_block.get("splits", []) or []:
            stat = split.get("stat", {})
            if stat.get("plateAppearances") is not None:
                career_stat = stat
                break
        if career_stat:
            break

    if not career_stat:
        return None

    # Derive seasons_played + first_season from mlbDebutDate. Last season
    # is the current year (we ASSUME the player is active enough to be in
    # season_batting / daily_picks). Approximation; refresh quarterly so
    # this drifts at most ~3 months.
    debut_date = person.get("mlbDebutDate", "")
    debut_year = None
    if debut_date and len(debut_date) >= 4:
        try:
            debut_year = int(debut_date[:4])
        except ValueError:
            pass
    current_year = datetime.now().year
    seasons_played = (current_year - debut_year + 1) if debut_year else None

    pa = _safe_int(career_stat.get("plateAppearances")) or 0
    ab = _safe_int(career_stat.get("atBats")) or 0
    hr = _safe_int(career_stat.get("homeRuns")) or 0
    hits = _safe_int(career_stat.get("hits")) or 0
    avg = _safe_float(career_stat.get("avg"))
    slg = _safe_float(career_stat.get("slg"))
    obp = _safe_float(career_stat.get("obp"))
    iso = (slg - avg) if (slg is not None and avg is not None) else None
    woba_proxy = (obp * 0.7 + slg * 0.3) if (obp is not None and slg is not None) else None
    hr_per_pa = (hr / pa) if pa > 0 else None

    return {
        "player_id":          player_id,
        "player_name":        name,
        "career_pa":          pa,
        "career_ab":          ab,
        "career_hr":          hr,
        "career_hits":        hits,
        "career_avg":         avg,
        "career_slg":         slg,
        "career_obp":         obp,
        "career_iso":         iso,
        "career_woba":        woba_proxy,
        "career_hr_per_pa":   hr_per_pa,
        # Statcast columns left NULL for the prototype — see module docstring
        "career_barrel_pct":  None,
        "career_exit_velo":   None,
        "career_hr_fb_pct":   None,
        "seasons_played":     seasons_played,
        "first_season":       debut_year,
        "last_season":        current_year if debut_year else None,
    }


def upsert_career_row(conn, row: dict) -> None:
    conn.execute("""
        INSERT OR REPLACE INTO career_batting
        (player_id, player_name,
         career_pa, career_ab, career_hr, career_hits,
         career_avg, career_slg, career_obp, career_iso, career_woba, career_hr_per_pa,
         career_barrel_pct, career_exit_velo, career_hr_fb_pct,
         seasons_played, first_season, last_season,
         fetched_at)
        VALUES (?, ?,  ?, ?, ?, ?,  ?, ?, ?, ?, ?, ?,  ?, ?, ?,  ?, ?, ?,
                datetime('now'))
    """, (
        row["player_id"], row["player_name"],
        row["career_pa"], row["career_ab"], row["career_hr"], row["career_hits"],
        row["career_avg"], row["career_slg"], row["career_obp"],
        row["career_iso"], row["career_woba"], row["career_hr_per_pa"],
        row["career_barrel_pct"], row["career_exit_velo"], row["career_hr_fb_pct"],
        row["seasons_played"], row["first_season"], row["last_season"],
    ))


def find_target_player_ids(conn, force: bool = False) -> list[int]:
    """All player_ids in season_batting + daily_picks, optionally filtered to
    those we don't have a recent career_batting row for.
    """
    season_ids = {r[0] for r in conn.execute(
        "SELECT DISTINCT player_id FROM season_batting WHERE player_id IS NOT NULL"
    ).fetchall()}
    pick_ids = {r[0] for r in conn.execute(
        "SELECT DISTINCT batter_id FROM daily_picks WHERE batter_id IS NOT NULL"
    ).fetchall()}
    all_ids = season_ids | pick_ids

    if force:
        return sorted(all_ids)

    cutoff = (datetime.now() - timedelta(days=SKIP_IF_FRESH_DAYS)).isoformat()
    fresh = {r[0] for r in conn.execute(
        "SELECT player_id FROM career_batting WHERE fetched_at >= ?",
        (cutoff,),
    ).fetchall()}
    return sorted(all_ids - fresh)


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync career_batting from MLB Stats API")
    parser.add_argument("--player-id", type=int, default=None,
                        help="Single player_id to refresh (debugging)")
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch even if a row is < 30 days old")
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after N players (smoke testing)")
    args = parser.parse_args()

    conn = get_db()
    create_tables(conn)
    log_id = log_etl_start(conn, "sync_career_batting", "career")

    try:
        if args.player_id:
            ids = [args.player_id]
        else:
            ids = find_target_player_ids(conn, force=args.force)

        if args.limit:
            ids = ids[:args.limit]

        print(f"[career] {len(ids)} player(s) to sync"
              f"{' (--force)' if args.force else ' (skip <30d old)'}")
        if not ids:
            print("  Nothing to do.")
            log_etl_complete(conn, log_id, rows=0, detail="no targets")
            conn.close()
            return 0

        ok = 0
        fail = 0
        for i, pid in enumerate(ids, 1):
            row = fetch_career_for_player(pid)
            if row:
                upsert_career_row(conn, row)
                ok += 1
                if i % 25 == 0:
                    conn.commit()
                    print(f"  [{i}/{len(ids)}] ok={ok} fail={fail} (last: {row['player_name']} "
                          f"{row['career_hr']} HR / {row['career_pa']} PA)")
            else:
                fail += 1
            # Polite pacing — 200ms between calls = ~5 req/s
            time.sleep(0.2)

        conn.commit()
        print(f"[career] DONE — ok={ok} fail={fail}")
        log_etl_complete(conn, log_id, rows=ok,
                         detail=f"ok={ok} fail={fail}")
        conn.close()
        return 0 if ok > 0 else 2
    except Exception as e:
        log_etl_fail(conn, log_id, str(e))
        print(f"[career] FAILED: {e}")
        conn.close()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
