#!/usr/bin/env python3
"""
prewarm_cache.py — Warm the archetype caches overnight so noon runs are fast.

Runs as part of run_nightly.bat (2 AM). Pre-fetches the slow per-player
Statcast data that USE_PER_PLAYER_STATCAST=True needs:
  - Pitcher arsenals (avg fb velo, spin, pitch mix) — ~30 starters
  - Batter HR events + victim profiles — full active roster

Both caches are 24h TTL, so warmth at 2 AM means the noon daily generate
hits cache for everything (~ms each) instead of per-player Statcast pulls.

Targets:
  - Tomorrow's probable starters from get_schedule()
  - Today's roster of batters from build_live_tiers() (a much smaller set
    than scanning all 750+ MLB hitters; only the players likely to be
    scored tomorrow)

Logs:
  - prints progress to stdout (run_nightly.bat tees to log file)
  - exits 0 even if some entries fail; partial warmth is still useful
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fetch_daily_data import get_schedule, build_live_tiers
from pitcher_profile import (
    build_pitcher_profiles_batch,
    build_victim_profiles_batch,
)
# Pre-warm the bulk Savant caches too, since fetch_live_slate uses them.
from features_v2 import (
    fetch_batter_xwoba_bulk,
    fetch_pitcher_fb_bulk,
    fetch_batter_recent_statcast_14d,
)


def main():
    season = datetime.now().year
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"\n[PREWARM] season={season}, today={today}, tomorrow={tomorrow}")

    # ---- Bulk Savant CSVs (covers xwOBA + FB% allowed for everyone) -----
    print("\n[PREWARM] Bulk Savant fetches...")
    t = time.time()
    try:
        b = fetch_batter_xwoba_bulk(season)
        p = fetch_pitcher_fb_bulk(season)
        print(f"  bulk batter xwOBA: {len(b)} entries ({time.time()-t:.1f}s)")
        print(f"  bulk pitcher FB%:  {len(p)} entries")
    except Exception as e:
        print(f"  WARN: bulk fetch failed: {e}")

    # ---- B6a: 14d quality-contact rolling Statcast (one bulk call) ------
    # Cache key uses tomorrow's date so noon's fetch_live_slate hits it.
    # 24h TTL; the daily run will overwrite this anyway, but pre-warming
    # means the noon hit costs ~ms instead of the ~30-60s bulk pitch pull.
    t = time.time()
    try:
        r = fetch_batter_recent_statcast_14d(as_of_date=tomorrow)
        print(f"  bulk recent 14d Statcast: {len(r)} batters ({time.time()-t:.1f}s)")
    except Exception as e:
        print(f"  WARN: recent 14d Statcast fetch failed: {e}")

    # ---- Tomorrow's probable starters (pitcher archetype warmup) --------
    print(f"\n[PREWARM] Pitcher archetypes for {tomorrow}...")
    pitcher_id_map: dict[str, int] = {}
    try:
        games = get_schedule(tomorrow)
        for g in games:
            for side in ("home", "away"):
                pname = g.get(f"{side}_pitcher_name", "TBD")
                pid = g.get(f"{side}_pitcher_id")
                if pname != "TBD" and pid:
                    pitcher_id_map[pname] = pid
        print(f"  {len(pitcher_id_map)} probable starters discovered")
    except Exception as e:
        print(f"  WARN: schedule fetch failed: {e}")

    if pitcher_id_map:
        t = time.time()
        try:
            profiles = build_pitcher_profiles_batch(pitcher_id_map, season)
            statcast_n = sum(1 for v in profiles.values() if v.get("source") == "statcast")
            print(f"  warmed {statcast_n}/{len(profiles)} via Statcast ({time.time()-t:.1f}s)")
        except Exception as e:
            print(f"  WARN: pitcher profile build failed: {e}")

    # ---- Active batter roster (victim profile warmup) --------------------
    print(f"\n[PREWARM] Batter victim profiles...")
    batter_ids: list[tuple[str, int]] = []
    try:
        live_tiers = build_live_tiers(today)
        if live_tiers:
            for tier in (1, 2, 3):
                for b in live_tiers.get(tier, []):
                    if b.get("player_id"):
                        batter_ids.append((b["name"], b["player_id"]))
        print(f"  {len(batter_ids)} active batters from live tiers")
    except Exception as e:
        print(f"  WARN: live tiers fetch failed: {e}")

    if batter_ids:
        t = time.time()
        try:
            vp = build_victim_profiles_batch(batter_ids, season)
            print(f"  warmed {len(vp)}/{len(batter_ids)} victim profiles ({time.time()-t:.1f}s)")
        except Exception as e:
            print(f"  WARN: victim profile build failed: {e}")

    print("\n[PREWARM] Done. Daily run at noon will hit cache.")


if __name__ == "__main__":
    main()
