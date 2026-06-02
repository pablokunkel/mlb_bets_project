"""
check_woba_today.py — quick sanity check on TODAY's matchup scores by woba quintile.
After tightening woba anchors (2026-05-01: 0.290-0.395), the avg_matchup
column should climb steeply across bins instead of staying flat.
Run: python check_woba_today.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from etl.db import get_db

# Canonical DB (HR_BETS_DB / repo-sibling fallback); fail-loud if absent so a
# missing DB is never silently created as a stray (B26).
conn = get_db()

latest = conn.execute("SELECT MAX(date) FROM daily_picks").fetchone()[0]
print(f"Latest date in daily_picks: {latest}\n")

rows = conn.execute("""
    WITH binned AS (
        SELECT
            NTILE(5) OVER (ORDER BY i.woba_vs_hand) AS bin,
            i.woba_vs_hand AS w,
            p.matchup_score AS m,
            p.matchup_version AS v
        FROM pick_inputs i
        JOIN daily_picks p
          ON p.date = i.date AND p.batter_id = i.batter_id
        WHERE i.date = ? AND i.woba_vs_hand IS NOT NULL
    )
    SELECT
        bin,
        ROUND(MIN(w), 3) AS lo,
        ROUND(MAX(w), 3) AS hi,
        COUNT(*)         AS n,
        ROUND(AVG(m), 1) AS avg_matchup,
        SUM(CASE WHEN v = 'v2' THEN 1 ELSE 0 END) AS n_v2
    FROM binned
    GROUP BY bin
    ORDER BY bin
""", (latest,)).fetchall()

print(f"{'BIN':<5} {'WOBA RANGE':<18} {'N':>4} {'AVG MATCHUP':>12} {'V2 ROWS':>9}")
print("-" * 55)
for r in rows:
    print(f"{r['bin']:<5} {r['lo']:>5}-{r['hi']:<10} {r['n']:>4} {r['avg_matchup']:>12} {r['n_v2']:>9}")

conn.close()
