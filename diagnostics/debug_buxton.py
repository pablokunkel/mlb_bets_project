"""
debug_buxton.py — show the THREE places Buxton's power inputs come from
to find why live scoring computed power=13 vs displayed estimates of ~48.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from etl.db import get_db

PICKS = Path(__file__).parent / "mlb_hr_bet_site" / "data" / "picks_latest.json"

print("=" * 70)
print("1. WHAT season_batting TABLE HAS (used by export_site_data → modal)")
print("=" * 70)
# Canonical DB (HR_BETS_DB / repo-sibling fallback); fail-loud if absent (B26).
conn = get_db()
rows = conn.execute("""
    SELECT player_name, season, hr, ab, pa, avg, slg, iso,
           barrel_pct, exit_velo, hr_fb_pct, fetched_at
    FROM season_batting
    WHERE player_name LIKE '%Buxton%'
    ORDER BY season DESC
""").fetchall()
for r in rows:
    print(f"  {dict(r)}")
print()

print("=" * 70)
print("2. WHAT pick_inputs RECORDED FOR TODAY (snapshot of what scored)")
print("=" * 70)
rows = conn.execute("""
    SELECT i.date, i.batter_id, i.barrel_pct, i.exit_velo, i.hr_fb_pct,
           i.iso, i.xwoba_contact, i.pull_fb_pct,
           p.power_score, p.composite, p.batter_name
    FROM pick_inputs i
    JOIN daily_picks p ON p.date = i.date AND p.batter_id = i.batter_id
    WHERE p.batter_name LIKE '%Buxton%'
    ORDER BY i.date DESC
    LIMIT 5
""").fetchall()
for r in rows:
    print(f"  {dict(r)}")
print()

print("=" * 70)
print("3. WHAT TODAY'S PICKS_LATEST.JSON HAS")
print("=" * 70)
if PICKS.exists():
    data = json.loads(PICKS.read_text())
    board = data.get("full_board", [])
    bux = [p for p in board if "Buxton" in (p.get("batter_name") or "")]
    for p in bux:
        print(f"  composite={p.get('composite')} power_score={p.get('power_score')}")
        print(f"  season_stats={p.get('season_stats')}")
else:
    print("  picks_latest.json not found")

print()
print("=" * 70)
print("DIAGNOSIS")
print("=" * 70)
print("If pick_inputs row 2 shows LOW barrel/EV/hr_fb (e.g., 3, 83, 4) but")
print("season_batting row 1 shows HIGH (11.9, 89.6, 10.7), then live scoring")
print("used stale/estimated data while the export used current stats.")
print()
print("If pick_inputs shows the HIGH values too, then score_power has a bug")
print("(not the data source). Re-check score_batters.py:262.")

conn.close()
