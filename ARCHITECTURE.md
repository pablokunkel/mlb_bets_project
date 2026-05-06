# Architecture

System overview for MLB HR Bets. Companion to `How_The_HR_Model_Works.md` (model details) and `DEPLOY.md` (release process).

## Component map

```
                  ┌─ MLB Stats API ─┐
                  ├─ Open-Meteo ────┤
                  ├─ Baseball Savant┤        ┌──────────────┐
                  ├─ the-odds-api  ─┼──────► │ ETL scripts  │
                  └─ pybaseball ────┘        │ (etl/*.py)   │
                                             └──────┬───────┘
                                                    ▼
                                          ┌────────────────────┐
                                          │ SQLite DB          │
                                          │ hr_bets.db (WAL)   │
                                          └────────┬───────────┘
                                                   │
                                ┌──────────────────┴──────────────────┐
                                ▼                                     ▼
                  ┌──────────────────────┐              ┌──────────────────────┐
                  │ generate_picks.py    │              │ export_site_data.py  │
                  │ (noon, slate scoring)│              │ (noon + 1AM)         │
                  └──────────┬───────────┘              └──────────┬───────────┘
                             ▼                                     ▼
                  ┌──────────────────────┐              ┌──────────────────────┐
                  │ load_picks_to_db.py  │              │ mlb_hr_bet_site/     │
                  │ (writes daily_picks  │              │   data/*.json        │
                  │  + pick_inputs)      │              │   index.html         │
                  └──────────────────────┘              └──────────┬───────────┘
                                                                   │
                                                          git push ▼
                                                       ┌──────────────────┐
                                                       │ Cloudflare Worker│
                                                       │ dingersonlybot   │
                                                       │ (apex domain)    │
                                                       └────────┬─────────┘
                                                                ▼
                                                        dingersonly.cc

  Separate path:                                        api.dingersonly.cc
  workers/live-hr/ ──── wrangler deploy ────►  Cloudflare Worker
                                               dingersonly-live-hr
                                               (1-min cron, KV-cached)
```

## Data flow per day

1. **2 AM** — `run_nightly.bat` refreshes Statcast / arsenals / season stats into the DB. Pre-warms next-day cache.
2. **12 PM** — `run_daily.bat` runs as 8 steps (see `DEPLOY.md`):
   - `[0a]` Kill stale `python.exe` zombies from a prior Ctrl-Cd run
   - `[0b]` `git pull --rebase --autostash origin main`
   - `[1]` Morning ETL: schedule + lineups + weather → DB
   - `[2]` Score every batter (3 tier passes + 1 untiered pass)
   - `[3]` Persist top-8 picks + full board → `daily_picks` + `pick_inputs`
   - `[4]` Self-heal yesterday's outcomes + HR events (idempotent re-run of `etl_outcomes` to recover from a failed/missed 1 AM run)
   - `[5]` Export JSON → `mlb_hr_bet_site/data/*.json`
   - `[6]` Commit + push. Cloudflare auto-deploys.
3. **(games happen)** — During games, `dingersonly-live-hr` polls MLB Stats API every minute and serves live HR data via `api.dingersonly.cc/api/live-hrs`. The dashboard's HR Recap tab polls this endpoint at 30s while the tab is visible.
4. **1 AM next day** — `run_outcomes.bat` pulls yesterday's box scores, computes outcomes, re-runs backtest_factors, re-exports JSON, commits, pushes. CF auto-deploys.

## Two scoring code paths

`score_batters.compute_composite()` chooses one of two matchup-scoring paths:

- **v2 path** (preferred when archetype data is available): `score_matchup_v2()` in `pitcher_profile.py`. Uses 4 signals (vulnerability, archetype similarity, Vegas implied total, woba_vs_hand). Gated behind `USE_PER_PLAYER_STATCAST=True` AND a successful Statcast profile fetch for the pitcher.
- **v1 path** (fallback): `score_matchup()` in `score_batters.py`. Uses 3 signals (vulnerability, woba_vs_hand, Vegas). No archetype matching.

Both paths share Power, Form, Park, Weather, Lineup scoring. Composite weights are the same (`WEIGHT_CONFIGS["default"]`).

## Lineup data source (rebuilt 2026-05-04)

Lineup ingestion in `fetch_daily_data.py` follows a 3-tier fallback chain. Each batter dict carries a `lineup_source` string that's persisted to `pick_inputs` and surfaced in the dashboard so we can see which tier each row came from.

1. **Posted lineup (preferred).** `statsapi.mlb.com/api/v1/schedule?hydrate=lineups`. One call returns every game on the date with confirmed batting orders 1-9 keyed by `homeBattingOrder` / `awayBattingOrder`. `lineup_source = "posted"`.

2. **Recent posted lineup (fallback when today's not yet up).** Walk back through the last 14 days of the team's `daily_lineup` rows; take the most recent date that has a posted lineup. Players keep their batting-order positions from that date. `lineup_source = "recent:YYYY-MM-DD"`.

3. **Bdfed roster (last resort).** `bdfed.stitch.mlbinfra.com/bdfed/matchup` returns the alphabetical 26-man roster. We mark every player `batting_order = NULL` and `lineup_source = "roster_fallback"` — the picks selection rule (`batting_order between 1 and 9`) excludes these from the final card.

**Critical bug history.** Prior to 2026-05-04, the scoring code took bdfed's roster output and assigned `batting_order = i + 1` based on the array index — but bdfed returns the roster *alphabetically*, not in batting order. The model was effectively scoring random hitters at "position 1-9" for any team without a posted lineup at noon, which during early May was most teams. Live hit rate dropped from 36-40% to ~17% during the bug window. PR #32 switched the primary path to statsapi/schedule, PR #33 added the recent-lineup fallback so a real-but-stale order is preferred over the alphabetical roster, and PR #34 added the `lineup_source` column for visibility.

## Tier scoring + the untiered fallback (added 2026-05-02)

`build_live_tiers()` in `fetch_daily_data.py` qualifies batters into 3 tiers based on `games >= 5 AND hr >= 1`. `generate_picks.py` runs `score_live_slate()` once per tier (T1-Chalk / T2-Mid / T3-Longshot), producing scored batters per tier.

After the 3 tier passes, `score_untiered_starters()` scoops up confirmed starters who didn't qualify for any tier — slow starters, recent IL returns, rookies — and scores them with stub data plus `season_batting` fallback. Tagged `tier=4` / `tier_label="T4-Untiered"` so the dashboard can distinguish.

This pass was added to fix the SEA/KC autopsy symptom (PR #3) where 5 of 9 SEA starters were silently dropped from `daily_picks` because they didn't meet the `games >= 5 AND hr >= 1` bar.

## What lives where in the repo

| Path | Purpose |
|---|---|
| `wrangler.jsonc` (root) | CF Workers Builds config for `dingersonlybot` static-assets Worker |
| `score_batters.py`, `generate_picks.py`, `fetch_daily_data.py`, `load_picks_to_db.py` | Daily scoring core |
| `features_v2.py`, `pitcher_profile.py` | Advanced features (Savant bulk, archetype matching) |
| `etl/` | ETL scripts (morning, nightly, outcomes, weather, calibration) + `db.py` schema |
| `mlb_hr_bet_site/` | **Canonical dashboard source.** Edit here for site changes. |
| `workers/live-hr/` | API worker for `api.dingersonly.cc` (live HR feed) |
| `diagnostics/` | Investigation tooling (autopsy, simulators, one-offs) — present after C4 lands |
| `backfill_*.py` | One-time and incremental backfill utilities |
| `refit_weights.py`, `backtest_factors.py` | Model evaluation (monthly refit, nightly backtest) |
| `mlb_2025_tiers.py` | Hardcoded 2025 tier data (offline-mode fallback) |
| `_archive_*` | Archived / abandoned directories. **Do not edit.** |
| `_review/` | Audit reports + scoping plans (untracked; agent outputs) |

## Key DB tables (sqlite, WAL mode)

| Table | Written by | Purpose |
|---|---|---|
| `daily_slate` | `etl_morning.fetch_schedule` + `fetch_weather` | Today's games + venue + probable pitchers + weather |
| `daily_lineup` | `etl_morning.fetch_lineups` | Confirmed lineups; `batting_order` capped at 9 (bench gets NULL) |
| `daily_picks` | `load_picks_to_db.py` | Today's full scored board (8 picks selected from top composites; full board persisted with `selected=0` for the rest). Carries a `mode` column (`'live'` / `'offline_simulation'`) added 2026-05-03. |
| `pick_inputs` | `load_picks_to_db.py` | Per-pick raw inputs (decomposes the composite for backtest/refit). Schema additions 2026-05-03 → 2026-05-04: `bats`/`throws` (batter + opposing pitcher handedness), `weather_source` / `barrel_pct_source` (provenance flags so the dashboard can distinguish real Statcast from synthetic estimates), `lineup_source` (posted / `recent:YYYY-MM-DD` / `roster_fallback`), and `vegas_team_total_raw` alongside the renamed `vegas_team_total_pct` (formerly mis-named `vegas_implied_total`, which actually stored a 0-100 percentile not a Vegas total in runs — PR #21). |
| `outcomes` | `etl_outcomes.fetch_outcomes_for_date` | Yesterday's HR-yes/no per batter from box scores |
| `season_batting` | `etl_nightly.sync_season_batting` | Season-to-date batting stats (Stats API splits + synthetic Statcast estimates) |
| `pitcher_arsenals` | `etl_nightly` | Pitcher pitch-mix from Savant (>7d staleness check) |
| `victim_profiles` | `etl_nightly.recompute_victim_profiles` | Per-batter weighted average of pitcher arsenals they've been HR'd by |
| `park_factors` | `etl_nightly.sync_park_factors` | Per-venue HR factor (current = hardcoded seed) |

DB path: `<project_parent>/data/hr_bets.db` — sibling of the repo, not inside it. All ETL scripts resolve via `etl/db.py:get_db()`.

## Why not Postgres / cloud DB?

The pipeline runs on a single machine, single concurrent writer, no remote consumers. SQLite + WAL is the right answer:

- Zero infra cost / setup
- Faster than network-Postgres for the read patterns (read-heavy on noon scoring)
- Backup is `cp hr_bets.db hr_bets.db.bak` (or OneDrive sync, which already happens)
- Migration to Postgres only makes sense if multiple machines start writing concurrently

## Known architectural debt

(Current as of 2026-05-06.)

- **`raw_data.csv` is the refit training source but doesn't auto-extend.** Monthly refit reads a 5,196-row 2026-03-27→2026-04-15 window. New days from `daily_picks ⨝ outcomes` are not appended. Result: monthly refit is currently a no-op. Fix is to either append nightly or refit directly off the DB.
- **Live tier estimates** (`barrel_pct`, `exit_velo`, `hr_fb_pct` synthesized from `hr_per_pa × constants`) populate `season_batting` via `etl_nightly.sync_season_batting`, so `enrich_with_season_batting`'s "fallback" is sometimes another synthetic estimate rather than real Statcast.
- **Live tracker session window.** The HR Recap "Live Today" panel rolls over at midnight ET, not at end-of-game. West-coast late games that finish past midnight ET still show up under "yesterday" while live status flips to "today" — minor visual mismatch. Planned fix: define a session window from first-pitch of the earliest game to last-out of the latest, instead of using calendar midnight.

### Resolved 2026-05

- **`pick_inputs.vegas_implied_total` mis-naming.** Renamed to `vegas_team_total_pct` and a separate `vegas_team_total_raw` (Vegas total in runs) added (PR #21, 2026-05-03). Schema migration handled in `etl/db.py` — old column auto-renamed at startup.
- **Multiple hardcoded "league-average pitcher" dicts.** Deduplicated 2026-05-02 — `LEAGUE_AVG_PITCHER` constant in `score_batters.py` is the single source.
- **Lineup data source was wrong.** Bdfed's alphabetical roster was being treated as a batting order; switched to `statsapi schedule?hydrate=lineups` (PR #32) with a recent-lineup fallback (PR #33) and a `lineup_source` provenance flag (PR #34) all 2026-05-04 → 2026-05-05. See "Lineup data source" section above for the full rebuild.
- **Untiered confirmed starters silently dropped.** Added the `score_untiered_starters` 4th pass (T4-Untiered) 2026-05-02. See "Tier scoring + the untiered fallback" above.

## Diagrams of process flow

For the per-day picks generation flow at a finer grain, see `How_The_HR_Model_Works.md`.

For the deployment / release process, see `DEPLOY.md`.
