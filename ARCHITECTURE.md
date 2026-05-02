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
2. **12 PM** — `run_daily.bat` pulls today's schedule + lineups + weather, scores every batter (3 tier passes + 1 untiered pass), writes the top-8 picks + full board to the DB and to `mlb_hr_bet_site/data/*.json`, commits, pushes. Cloudflare auto-deploys.
3. **(games happen)** — During games, `dingersonly-live-hr` polls MLB Stats API every minute and serves live HR data via `api.dingersonly.cc/api/live-hrs`. The dashboard's HR Recap tab polls this endpoint at 30s while the tab is visible.
4. **1 AM next day** — `run_outcomes.bat` pulls yesterday's box scores, computes outcomes, re-runs backtest_factors, re-exports JSON, commits, pushes. CF auto-deploys.

## Two scoring code paths

`score_batters.compute_composite()` chooses one of two matchup-scoring paths:

- **v2 path** (preferred when archetype data is available): `score_matchup_v2()` in `pitcher_profile.py`. Uses 4 signals (vulnerability, archetype similarity, Vegas implied total, woba_vs_hand). Gated behind `USE_PER_PLAYER_STATCAST=True` AND a successful Statcast profile fetch for the pitcher.
- **v1 path** (fallback): `score_matchup()` in `score_batters.py`. Uses 3 signals (vulnerability, woba_vs_hand, Vegas). No archetype matching.

Both paths share Power, Form, Park, Weather, Lineup scoring. Composite weights are the same (`WEIGHT_CONFIGS["default"]`).

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
| `daily_picks` | `load_picks_to_db.py` | Today's full scored board (8 picks selected from top composites; full board persisted with `selected=0` for the rest) |
| `pick_inputs` | `load_picks_to_db.py` | Per-pick raw inputs (decomposes the composite for backtest/refit) |
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

(Current as of 2026-05-02 — see `_review/model_audit_2026-05-02.md` for full list.)

- **`raw_data.csv` is the refit training source but doesn't auto-extend.** Monthly refit reads a 5,196-row 2026-03-27→2026-04-15 window. New days from `daily_picks ⨝ outcomes` are not appended. Result: monthly refit is currently a no-op. Fix is to either append nightly or refit directly off the DB.
- **Multiple hardcoded "league-average pitcher" dicts.** 6+ sites construct `{"hr_per_9": 1.2, ...}` defaults independently. Drift over seasons; no provenance flag distinguishing measured vs default. Tracked for PR #4.
- **`pick_inputs.vegas_implied_total` stores a 0–100 percentile, not a Vegas total in runs.** Column name is misleading. Fix in PR #4.
- **Live tier estimates** (`barrel_pct`, `exit_velo`, `hr_fb_pct` synthesized from `hr_per_pa × constants`) populate `season_batting` via `etl_nightly.sync_season_batting`, so `enrich_with_season_batting`'s "fallback" is sometimes another synthetic estimate rather than real Statcast. Tracked for PR #5+.

## Diagrams of process flow

For the per-day picks generation flow at a finer grain, see `How_The_HR_Model_Works.md`.

For the deployment / release process, see `DEPLOY.md`.
