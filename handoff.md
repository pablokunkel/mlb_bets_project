# Daily Homerun Bet — Project Handoff

## What This Is

An MLB daily HR parlay skill that generates an 8-player round-robin HR parlay card each day. It uses a composite 0-100 scoring engine across 5 factors (Power, Matchup, Park, Form, Weather), with three-tier batter pools and per-tier optimized weight configurations validated through Monte Carlo backtesting.

## Architecture

### Scoring Engine
- **5 factors**: Power Profile, Matchup Quality, Park Factor, Recent Form, Weather
- **6 weight configs**: `default`, `matchup_heavy`, `power_heavy`, `park_heavy`, `form_heavy`, `no_weather`
- Each factor produces a 0-100 score; the composite is a weighted sum

### Three-Tier Batter Pools (150 players total)
- **Tier 1 (Chalk)**: Top 50 HR hitters (18-60 HR in 2025) — 1pt per HR hit
- **Tier 2 (Mid-Range)**: Ranked 51-100 (13-26 HR) — 3pts per HR hit
- **Tier 3 (Longshots)**: Ranked 101-150 (8-13 HR) — 9pts per HR hit

### Optimized Configs (from 2025 backtesting)
| Tier | Best Config | Hit Rate |
|------|-------------|----------|
| T1 | matchup_heavy | 47.9% |
| T2 | default | 32.9% |
| T3 | power_heavy | 21.2% |

### Recommended Card Blend: 3/2/3
- 3 T1 picks, 2 T2 picks, 3 T3 picks
- Best hit rate (37.1%), lowest blank rate (10%), solid points (11.0 avg)
- Max pts if all 8 hit: 36

## File Structure

```
daily-homerun-bet/
├── SKILL.md                    # Workflow docs
├── references/
│   └── scoring_model.md        # Detailed factor/weight documentation
├── scripts/
│   ├── generate_picks.py       # ★ MAIN ENTRY POINT — generates daily card
│   ├── fetch_daily_data.py     # MLB Stats API + Open-Meteo + pybaseball
│   ├── score_batters.py        # Composite scoring engine + WEIGHT_CONFIGS
│   ├── output_picks.py         # Parlay card formatter
│   ├── mlb_2025_tiers.py       # Offline 2025 dataset (150 batters + 33 pitchers)
│   ├── mlb_2024_tiers.py       # Offline 2024 dataset (legacy)
│   ├── mlb_2024_data.py        # Original 2024 50-batter dataset (legacy)
│   ├── backtest.py             # Original backtest engine
│   ├── backtest_tiered.py      # Three-tier backtest + blended card sim
│   ├── backtest_config_sweep.py # Per-tier config optimization
│   ├── run_2025_phase1.py      # 2025 config sweep runner
│   ├── run_2025_phase2.py      # 2025 blended card comparison runner
│   └── run_2025_sweep.py       # Combined sweep (too slow for sandbox)
└── results/
    ├── config_sweep_2025.json  # ★ Final 2025 results (tier sweep + blend comparison)
    ├── phase1_2025.json        # 2025 per-tier config sweep
    ├── picks_2026-03-26.json   # Sample picks output
    ├── config_sweep_2024.json  # 2024 per-tier results (legacy comparison)
    ├── backtest_2024_all_configs.json
    ├── final_ranking.json
    └── tiered_backtest_2024.json
```

## How generate_picks.py Works

**Live mode** (default — needs internet):
```bash
python scripts/generate_picks.py --date 2026-03-26
```
1. Calls MLB Stats API (free, no key) for schedule, lineups, probable pitchers
2. Calls Open-Meteo (free, no key) for venue weather at game time
3. Tries pybaseball/FanGraphs for recent Statcast form data
4. Scores all batters in each tier with per-tier optimized configs
5. Selects top picks per tier with max-2-per-game diversification
6. Outputs formatted card + JSON

**Offline mode** (no internet needed):
```bash
python scripts/generate_picks.py --date 2026-03-26 --offline
```
Uses hardcoded 2025 data with simulated matchups. Deterministic by date (MD5 seed).

**Options:**
- `--date YYYY-MM-DD` or `--date today`
- `--combo 3,2,3` (must sum to 8)
- `--offline` (skip API calls)
- `--output path.json`

**Requirements:** `pip install pybaseball pandas numpy requests`

## Data Sources (Live Mode)
- **MLB Stats API** (`statsapi.mlb.com/api/v1`) — schedule, lineups, rosters, probable pitchers. Free, no auth.
- **Open-Meteo** (`api.open-meteo.com`) — hourly weather by venue coordinates. Free, no auth.
- **pybaseball** — FanGraphs batting/pitching stats, Statcast batted-ball data. Free, no auth.
- **Park factors** — hardcoded 30-venue HR park factors (2022-2024 rolling avg) in `fetch_daily_data.py`

## Key Design Decisions
- `select_top_picks()` in `score_batters.py` deduplicates by player name AND enforces max 2 per game
- Live mode degrades gracefully: if FanGraphs fails, uses offline pitcher data; if MLB API fails entirely, falls back to offline sim
- The `DOME_STADIUMS` set handles weather neutralization for retractable/fixed roof venues
- Tier points (1/3/9) are calibrated so one T3 longshot HR = three T1 chalk HRs in value

## Backtesting Summary (2025 Data)

**Per-tier config sweep** (6 configs × 3 tiers × 2 seeds × 15 days):
- T1: matchup_heavy wins (47.9%) — pitcher matchup most predictive for chalk
- T2: default wins (32.9%) — balanced weights for mid-range
- T3: power_heavy wins (21.2%) — raw power best signal for longshots

**Blended card comparison** (uniform power_heavy vs per-tier optimized):
- Optimized improves all-tiers-covered rate by +3-4% on key combos
- 3/2/3 optimized: 37.1% hit rate, 10% blank rate, 11.0 avg pts
- 2/3/3 optimized: best all-tiers coverage (16.7%) + highest points (11.0)

---

## ⚠️ Code Review — Bugs to Fix Before Going Live

A code review was completed on 2026-03-26. Fix these in order of priority before testing live mode.

### Priority 1 — Blockers (will break live mode)

**[fetch_daily_data.py] Lazy-load pybaseball imports**
The top-level `import pybaseball` block calls `sys.exit(1)` on ImportError. This kills the entire import chain — `generate_picks.py` can't even load `fetch_daily_data` in offline mode if pybaseball isn't installed. Move all pybaseball imports inside the functions that use them and remove the top-level block:
```python
def get_statcast_batter_stats(season, min_pa=100):
    try:
        from pybaseball import batting_stats
        # ... rest of function
    except ImportError:
        return pd.DataFrame()
```

**[generate_picks.py] `fetched_fg` flag is inverted**
In `fetch_live_slate()`, the flag is set to `True` after the first FanGraphs attempt regardless of success or failure — meaning pitcher stats only fetch for the very first pitcher and all others fall back to offline data. Fix: only set `fetched_fg = True` on failure:
```python
live_stats = try_fetch_pitcher_season_stats(pname, season)
if live_stats:
    pitcher_lookup[pname] = live_stats
    # do NOT set fetched_fg here
else:
    fetched_fg = True  # first failure → skip FanGraphs for remaining
```

### Priority 2 — Scoring Accuracy

**[generate_picks.py] Barrel detection uses `>= 6` instead of `== 6`**
In `try_fetch_statcast_recent()`, `launch_speed_angle >= 6` also captures Statcast codes 7 (Flare/Burner) and 8 (Solid Contact). Fix:
```python
barrels = df[df["launch_speed_angle"] == 6] if "launch_speed_angle" in df.columns else df.head(0)
```

**[fetch_daily_data.py] Weather hour index uses UTC instead of local time**
`get_weather()` uses `dt.hour` on the UTC game timestamp. A 7pm ET game returns hour 23 UTC, indexing the wrong forecast slot. Quick fix:
```python
et_offset = 4  # EDT; use 5 for EST
game_hour = max(0, dt.hour - et_offset)
idx = min(game_hour, len(hourly.get("temperature_2m", [])) - 1)
```

**[score_batters.py] Platoon bonus fires for unknown pitcher handedness**
Both `bats` and `throws` default to `"R"` when missing. LHB batters always get the bonus against any pitcher with no `throws` data. Fix:
```python
batter_hand = batter.get("bats")
pitcher_hand = pitcher.get("throws")
platoon_bonus = 10 if (batter_hand and pitcher_hand and batter_hand != pitcher_hand) else 0
```

### Priority 3 — Reliability

**[generate_picks.py] Team name matching is fragile**
`score_live_slate()` uses substring matching on full team names from the API. Add a `TEAM_ABBREV_TO_FULL_NAME` dict with the exact strings returned by `statsapi.mlb.com` and use it for matching instead.

**[fetch_daily_data.py] Season start date is hardcoded to March 28**
Opening Day varies by year. Set the scan start to March 20 to avoid missing early games.

---

## Cowork Instructions — Make This Operational

**Goal: apply all bug fixes, get `generate_picks.py --date today` running cleanly in live mode, then set up a daily scheduled task that drops a picks file on the Desktop each morning.**

### Step 1 — Apply all bug fixes
Work through every fix in the Priority 1 and Priority 2 sections above, editing the four scripts in place. After each file is patched, run the offline test to confirm nothing is broken:
```bash
python scripts/generate_picks.py --date 2026-03-26 --offline
```
Expected: formatted 8-pick card prints without errors, all 8 players have names/venues/scores.

### Step 2 — Install dependencies
```bash
pip install pybaseball pandas numpy requests
```
If there are system conflicts:
```bash
pip install pybaseball pandas numpy requests --break-system-packages
```

### Step 3 — Test live mode against today's slate
```bash
python scripts/generate_picks.py --date 2026-03-26
```
Watch for:
- `[LIVE] Found N games` from the MLB Stats API — expect 10-15 on a full game day
- Weather pulling without errors for each venue
- Pitcher stats fetching from FanGraphs OR gracefully falling back to offline
- A formatted card with real player names, real venues, and real opposing pitchers

If `pool_size = 0` for any tier in the Tier Breakdown section, the team-name matching in `score_live_slate()` is silently dropping batters. Add debug prints to compare `batter["team"]` values against the game's `home_team`/`away_team` strings and fix the mismatch.

### Step 4 — Set up daily Desktop output
Create a wrapper script at the project root:

**Mac/Linux — `run_daily.sh`:**
```bash
#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT_DIR="$HOME/Desktop/HR-Picks"
mkdir -p "$OUTPUT_DIR"
DATE=$(date +%Y-%m-%d)
python "$SCRIPT_DIR/scripts/generate_picks.py" --date "$DATE" \
  --output "$OUTPUT_DIR/picks_$DATE.json" \
  > "$OUTPUT_DIR/picks_$DATE.txt" 2>&1
```
```bash
chmod +x run_daily.sh
```
Schedule via cron at 10am daily (after probable pitchers post, before lineups):
```bash
crontab -e
# Add this line:
0 10 * * * /full/path/to/daily-homerun-bet/run_daily.sh
```

**Windows — `run_daily.bat`:**
```bat
@echo off
set OUTPUT_DIR=%USERPROFILE%\Desktop\HR-Picks
if not exist "%OUTPUT_DIR%" mkdir "%OUTPUT_DIR%"
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value') do set DT=%%I
set DATE=%DT:~0,4%-%DT:~4,2%-%DT:~6,2%
python "%~dp0scripts\generate_picks.py" --date %DATE% --output "%OUTPUT_DIR%\picks_%DATE%.json" > "%OUTPUT_DIR%\picks_%DATE%.txt" 2>&1
```
Schedule via Task Scheduler: point to `run_daily.bat`, trigger at 10:00 AM daily.

### Step 5 — Verify end-to-end on a real game day
Confirm the `.txt` file on the Desktop contains a valid 8-pick card where:
- All players are from teams playing that day
- Venues and opposing pitchers are real (not "N/A" or "TBD")
- The mode line says `LIVE` not `OFFLINE`

If any picks show "N/A" for pitcher or venue, live data is partially failing — check the fallback logs and trace through `score_live_slate()` to find where data is dropping.
