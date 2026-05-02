# Handoff — Netlify Pipeline & DB-Backed Daily Flow

**Date paused:** 2026-04-29
**Status:** Code changes done and parsed-clean. Nothing has been run on Pablo's actual Windows machine yet. Backfill + scheduled-task setup + smoke test are pending.

## What this work was solving

Two related problems surfaced when reviewing the live Netlify dashboard:

1. **`picks_history.json` was empty** despite the daily run existing. Root cause: `export_site_data.py` reads from the SQLite DB (`daily_picks` JOIN `outcomes`), but **nothing in the daily flow was writing to those tables**. `generate_picks.py` only writes JSON. The DB itself was just stood up the night of 2026-04-28, so the gap is fresh — it's not a regression, it's missing wiring.

2. **20 days of historical pick data existed only in `raw_data.csv`** (2026-03-27 → 2026-04-15) and had never been ingested into the DB. The dashboard was effectively running with no historical data.

## Architecture (what was decided)

The daily pipeline is being split into **three Windows scheduled tasks** instead of one:

| Task | Time | Script | What it writes |
|---|---|---|---|
| `MLB_HR_Daily_Picks` | 12:00 PM | `run_daily.bat` | `daily_slate`, `daily_lineup`, `daily_picks` → deploys |
| `MLB_HR_Outcomes` | 1:00 AM | `run_outcomes.bat` | `outcomes` → re-deploys |
| `MLB_HR_Nightly` | 2:00 AM | `run_nightly.bat` | Statcast / arsenals / season stats |

Reasoning is in `DEPLOYMENT.md`. Short version: morning ETL needs lineups (only ready late morning), outcomes need games to be over (~1 AM ET earliest), nightly Statcast is heavy and shouldn't block the noon deploy.

**Deploy is via Netlify CLI direct, NOT git push.** Site ID `0fade6bd-ae06-43a8-aaef-22ee692ecbba`. Don't propose GitHub auto-deploy as a fix.

## Files created/changed in this session

**New:**
- `backfill_from_csv.py` — one-shot ingest of `raw_data.csv` into `daily_picks` + `outcomes`. Idempotent (delete-then-insert per date). Joins `mlb_2025_tiers` for tier assignments. Already validated end-to-end in a sandbox copy of the DB — produced 158 selected picks across 20 days, 36.1% hit rate (vs. 37.1% backtest target).
- `load_picks_to_db.py` — the bridge. Reads `results/picks_<DATE>.json` and inserts the full board into `daily_picks`, with `selected=1` flagged on the 8 in the card. Idempotent. Smoke-tested with synthetic JSON.
- `run_outcomes.bat` — 1 AM scheduled task wrapper.
- `run_nightly.bat` — 2 AM scheduled task wrapper.

**Modified:**
- `generate_picks.py` — JSON output now includes `player_id`, `opp_pitcher_id`, `matchup_version`, `bats`, and `game_pk` on both `picks[]` and `full_board[]`. Strictly additive change. **⚠️ See "Coordination with the other agent" below — this file is being rewritten in parallel.**
- `run_daily.bat` — now 5 steps: `etl_morning` → `generate_picks` → `load_picks_to_db` → `export_site_data` → `netlify deploy`. Each step aborts on error.
- `DEPLOYMENT.md` — rewritten to describe the three-task architecture and document the schtasks setup commands.

**Memory:** `project_mlb_hr_bets_deployment.md` was added to Claude's memory documenting the CLI-direct-deploy choice and the three-task flow.

## ⚠️ Coordination with the other agent

Pablo paused this work because another agent is simultaneously rewriting `generate_picks.py` to clean up its scoring logic. Two things matter for the bridge to keep working when that rewrite lands:

**`load_picks_to_db.py` reads these fields from the picks JSON. The new `generate_picks.py` MUST include them in its output:**

In `picks[]` array items:
- `player_id` (int) — required, becomes `daily_picks.batter_id`
- `name` (str)
- `team`, `tier`, `tier_label`
- `opp_pitcher`, `opp_pitcher_id`
- `composite`, `power_score`, `matchup_score`, `park_score`, `form_score`, `weather_score`, `lineup_score`
- `matchup_version` ("v1" or "v2")
- `batting_order` (1-9 or string like "bench")
- `game_pk`

In `full_board[]` array items: same as above, plus `selected: bool`. (The bridge will derive `selected` from card membership if the field is missing, but having it is more reliable.)

Top-level: `date`, `scoring_config`.

**If the rewrite changes those field names**, the bridge needs the new names. The bridge has a name-matching fallback when `player_id` is missing, but that loses the outcomes JOIN (which is keyed on `batter_id`), so player_id specifically must persist.

## What's still pending (the real work)

### 1. Confirm `generate_picks.py` post-rewrite still emits the right JSON shape

After the other agent finishes, diff the new `picks_<DATE>.json` against the contract above. If field names changed, update `load_picks_to_db.py` to match — it's a small file, easy to retarget.

### 2. Initial one-time backfill on Pablo's actual machine

The DB I populated lives in a sandbox (cleaned up on session end). Pablo's real DB at `C:\Users\pablo\OneDrive\Documents\Claude\Projects\data\hr_bets.db` is still empty.

```cmd
cd "C:\Users\pablo\OneDrive\Documents\Claude\Projects\MLB HR Bets"
python etl\db.py --create
python backfill_from_csv.py
python export_site_data.py
netlify deploy --prod --dir=mlb_hr_bet_site --site=0fade6bd-ae06-43a8-aaef-22ee692ecbba
```

Expected output: `Inserted 5196 daily_picks rows / Inserted 5196 outcomes rows`, then export prints `picks_history.json (20 days)` and `performance.json (158 picks, 57 hits)`.

### 3. Register the two new scheduled tasks (admin cmd)

```cmd
schtasks /create /tn "MLB_HR_Outcomes" /tr "\"C:\Users\pablo\OneDrive\Documents\Claude\Projects\MLB HR Bets\run_outcomes.bat\"" /sc daily /st 01:00 /rl HIGHEST /f

schtasks /create /tn "MLB_HR_Nightly" /tr "\"C:\Users\pablo\OneDrive\Documents\Claude\Projects\MLB HR Bets\run_nightly.bat\"" /sc daily /st 02:00 /rl HIGHEST /f
```

Existing `MLB_HR_Daily_Picks` doesn't need re-registering — `run_daily.bat` is updated in place.

### 4. Manual end-to-end smoke test

After the backfill and before the noon scheduled task fires, run `run_daily.bat` manually once. This validates the full new chain (morning ETL → generate → bridge → export → deploy). Watch `logs/daily_<TODAY>.log` for any errors.

If `load_picks_to_db.py` errors with "No picks JSON found", check whether `generate_picks.py` is writing to `<project>/results/` or `<project parent>/results/` (the bridge checks both, plus `Desktop/HR-Picks/`). Easy fix is to pass `--json` explicitly.

## Known issues / gotchas

**Pre-existing bugs in `generate_picks.py`** — `handoff.md` documents Priority 1 / 2 / 3 bugs from a 2026-03-26 code review. These weren't in scope for this session and may be exactly what the other agent is fixing. The Priority 1 one (top-level `pybaseball` import that calls `sys.exit(1)`) bit me when I tried to import-check the file in the sandbox — it's not blocking on Pablo's machine because pybaseball is installed there, but it'd kill any CI or fresh-env run.

**Tier coverage** — `mlb_2025_tiers.py` covers ~150 players from the 2025 season. Several top 2026 picks (Jordan Walker, CJ Abrams, Sal Stewart) aren't tiered. So `daily_picks.tier_label` is NULL for ~half the rows from the backfill, and the dashboard's per-tier breakdown only covers 84 of 158 picks. Not a bug — just worth knowing when reviewing the dashboard.

**The DB path is awkward** — `etl/db.py` resolves to `<project parent>/data/hr_bets.db`, which is `C:\Users\pablo\OneDrive\Documents\Claude\Projects\data\hr_bets.db` (a sibling of the project dir, not inside it). Don't try to "fix" this without checking the ETL scripts — they all use the same `get_db()` helper, so the path is consistent across the codebase.

**File line endings** — the project uses CRLF. If using bash to manipulate files, write CRLF explicitly. (One Edit during this session inadvertently truncated `generate_picks.py`; restoring required a bash write with explicit `\r\n`. Not a recurring problem unless you're appending to .py files via raw bash.)

**Composite signal is weak at the top of the board** — backfill showed `avg_hit_composite` (69.8) vs. `avg_miss_composite` (67.7), only ~2 points of separation. Worth investigating once the dashboard is live, but not part of the deployment fix.

## Verification once everything is wired

After step 4 above succeeds, the live site at `mlb-hr-bets.netlify.app` should show:

- Today's card on the latest-picks view
- 20+ days of history (more if any new days have accumulated)
- ~36% hit rate, 20-day hit streak (will tick up to 21 with each successful day)
- Top hitters list (Jordan Walker, Sal Stewart, Yordan Alvarez at the top from the backfill)
- Factor trends chart populated

If `picks_history.json` still shows `total_days: 0` after step 2, the backfill didn't actually write to the DB — check that `python etl\db.py --create` ran and look at where it created the file. The export needs the same DB path, which it gets via `etl.db.get_db()`.
