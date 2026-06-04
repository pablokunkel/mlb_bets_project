# Data-integrity audit — 2026-06-03

> **B31 — read-only diagnostic.** Column-by-column model-input coverage map across `pick_inputs` + the snapshot/support tables, every gap classified, and a ranked backfill plan. No data / scoring / ETL changes. Regenerate with `python -m diagnostics.data_integrity_audit --md docs\data_integrity_audit_<date>.md`.

- **DB audited:** `C:\dev\Claude\Projects\data\hr_bets.db`
- **recent-live window** (`2026_recent_14d`): **2026-05-20 → 2026-06-02** — the last 14d distinct `pick_inputs` dates; the only era that reflects today's live pipeline.
- **era row counts:** `2025_backfill`=55,638, `2026_pre_recent`=12,125, `2026_recent_14d`=4,198

## Gap-classification counts

| Class | `pick_inputs` columns | Meaning |
|---|---:|---|
| `BROKEN_BACKFILL` | 17 | ran-but-never-landed or starved by a code bug — **the work-list** |
| `LIVE_GAP` | 2 | thin on recent-live rows — affects today's picks |
| `INTENTIONAL` | 10 | NULL by design / known-dead — **not a bug** |
| `HEALTHY` | 32 | populated where it should be — no action |
| `METADATA` | 5 | provenance / bookkeeping (non-signal) |

Plus three snapshot tables, all `BROKEN_BACKFILL`: `batter_park_archetype` (all-NULL centroids), `batter_form_archetype` (empty), `batter_pitch_type_splits` (empty).

## Are today's picks scoring on complete data? (`2026_recent_14d`)

**Yes for the six scored factors.** In the recent-live window every input the composite actually consumes is well-covered: Power (barrel/exit_velo/hr_fb/iso/xwoba ~93–100%), Matchup (pitcher_* 100%, woba_vs_hand ~99%, vegas ~99%, slate_pitcher_vulnerability_pct 100%), Park (hr_park_factor ~97%), Weather (temp/wind 100%), Form (recent_hr_10g/iso_30g ~99%, season_hr ~91%), plus the B6a real-Statcast 14d inputs (~82%). The picks are **not** scoring on missing data.

**What is dark in the live window** is the set of *unwired/optional* signals — they don't degrade today's picks, but they're exactly the levers B27 (form rebuild) needs:

- **0% (broken-backfill):** `recent_barrel_real_21d`, `recent_xwoba_contact_21d`, `recent_iso_21d`, `recent_barrel_real_28d`, `recent_xwoba_contact_28d`, `recent_iso_28d`, `fb_slg`, `fb_pa`, `br_slg`, `br_pa`, `os_slg`, `os_pa`, `form_archetype_centroid_json`, `form_archetype_window`, `form_archetype_n_hrs`, `park_archetype_centroid_json`, `park_archetype_n_hrs` — plus `ev_trend` (intentional until A2).
- **partial (live-gap):** `humidity_pct`, `slate_weather_pct` — mostly domes + open-meteo humidity misses (B14/B10).

## Per-column coverage (`pick_inputs`)

| column | 2025_backfill | 2026_pre_recent | 2026_recent_14d | class |
|---|---:|---:|---:|---|
| `barrel_pct` | 100.0% | 96.2% | 92.7% | HEALTHY |
| `exit_velo` | 100.0% | 99.8% | 100.0% | HEALTHY |
| `hr_fb_pct` | 100.0% | 96.2% | 92.7% | HEALTHY |
| `iso` | 100.0% | 98.6% | 97.7% | HEALTHY |
| `xwoba_contact` | 0.0% | 95.9% | 99.4% | INTENTIONAL |
| `pull_fb_pct` | 0.0% | 0.0% | 0.0% | INTENTIONAL |
| `recent_hr_14d` | 0.0% | 93.5% | 0.0% | INTENTIONAL |
| `recent_barrel_pct_14d` | 0.0% | 48.8% | 0.0% | INTENTIONAL |
| `ev_trend_14d` | 0.0% | 48.8% | 0.0% | INTENTIONAL |
| `pitcher_hr_per_9` | 100.0% | 97.3% | 100.0% | HEALTHY |
| `pitcher_era` | 100.0% | 97.0% | 100.0% | HEALTHY |
| `pitcher_hh_pct` | 100.0% | 97.3% | 100.0% | HEALTHY |
| `pitcher_k_per_9` | 100.0% | 97.0% | 100.0% | HEALTHY |
| `pitcher_fb_pct_allowed` | 98.7% | 96.5% | 99.5% | HEALTHY |
| `woba_vs_hand` | 100.0% | 99.5% | 99.5% | HEALTHY |
| `archetype_similarity` | 67.4% | 85.9% | 67.4% | INTENTIONAL |
| `vegas_team_total_pct` | 0.0% | 52.4% | 99.4% | INTENTIONAL |
| `platoon_advantage` | 100.0% | 97.3% | 100.0% | HEALTHY |
| `hr_park_factor` | 94.0% | 55.8% | 97.0% | HEALTHY |
| `temperature_f` | 100.0% | 57.3% | 100.0% | HEALTHY |
| `wind_mph` | 100.0% | 57.3% | 100.0% | HEALTHY |
| `wind_direction_deg` | 100.0% | 57.3% | 100.0% | HEALTHY |
| `humidity_pct` | 70.8% | 29.9% | 57.3% | LIVE_GAP |
| `is_dome` | 100.0% | 57.4% | 100.0% | HEALTHY |
| `batting_order` | 72.7% | 37.5% | 76.6% | INTENTIONAL |
| `fetched_at` | 100.0% | 100.0% | 100.0% | METADATA |
| `source` | 100.0% | 100.0% | 100.0% | METADATA |
| `bats` | 100.0% | 49.4% | 100.0% | HEALTHY |
| `throws` | 100.0% | 49.4% | 100.0% | HEALTHY |
| `weather_source` | 98.7% | 49.4% | 100.0% | METADATA |
| `barrel_pct_source` | 98.7% | 45.8% | 92.7% | METADATA |
| `vegas_team_total_raw` | 0.0% | 46.4% | 99.4% | INTENTIONAL |
| `lineup_source` | 98.7% | 43.6% | 100.0% | METADATA |
| `pitcher_recent_hr9_21d` | 92.3% | 20.3% | 96.1% | HEALTHY |
| `pitcher_recent_starts_21d` | 98.7% | 20.3% | 97.1% | HEALTHY |
| `recent_hr_10g` | 99.0% | 3.0% | 99.4% | HEALTHY |
| `recent_iso_30g` | 99.0% | 3.0% | 99.4% | HEALTHY |
| `recent_avg_30g` | 99.0% | 3.0% | 99.4% | HEALTHY |
| `recent_window_days` | 98.0% | 3.0% | 99.0% | HEALTHY |
| `ev_trend` | 0.0% | 0.0% | 0.0% | INTENTIONAL |
| `pitcher_recent_era_21d` | 92.3% | 0.0% | 87.5% | HEALTHY |
| `pitcher_recent_k9_21d` | 92.3% | 0.0% | 87.5% | HEALTHY |
| `season_hr` | 100.0% | 0.0% | 90.7% | HEALTHY |
| `recent_barrel_real_14d` | 86.9% | 0.0% | 81.7% | HEALTHY |
| `recent_xwoba_contact_14d` | 84.8% | 0.0% | 81.7% | HEALTHY |
| `recent_iso_14d` | 86.9% | 0.0% | 81.7% | HEALTHY |
| `recent_barrel_real_21d` | 0.0% | 0.0% | 0.0% | BROKEN_BACKFILL |
| `recent_xwoba_contact_21d` | 0.0% | 0.0% | 0.0% | BROKEN_BACKFILL |
| `recent_iso_21d` | 0.0% | 0.0% | 0.0% | BROKEN_BACKFILL |
| `recent_barrel_real_28d` | 0.0% | 0.0% | 0.0% | BROKEN_BACKFILL |
| `recent_xwoba_contact_28d` | 0.0% | 0.0% | 0.0% | BROKEN_BACKFILL |
| `recent_iso_28d` | 0.0% | 0.0% | 0.0% | BROKEN_BACKFILL |
| `fb_slg` | 0.0% | 0.0% | 0.0% | BROKEN_BACKFILL |
| `fb_pa` | 0.0% | 0.0% | 0.0% | BROKEN_BACKFILL |
| `br_slg` | 0.0% | 0.0% | 0.0% | BROKEN_BACKFILL |
| `br_pa` | 0.0% | 0.0% | 0.0% | BROKEN_BACKFILL |
| `os_slg` | 0.0% | 0.0% | 0.0% | BROKEN_BACKFILL |
| `os_pa` | 0.0% | 0.0% | 0.0% | BROKEN_BACKFILL |
| `form_archetype_centroid_json` | 0.0% | 0.0% | 0.0% | BROKEN_BACKFILL |
| `form_archetype_window` | 0.0% | 0.0% | 0.0% | BROKEN_BACKFILL |
| `form_archetype_n_hrs` | 0.0% | 0.0% | 0.0% | BROKEN_BACKFILL |
| `park_archetype_centroid_json` | 0.0% | 0.0% | 0.0% | BROKEN_BACKFILL |
| `park_archetype_n_hrs` | 0.0% | 0.0% | 0.0% | BROKEN_BACKFILL |
| `slate_park_pct` | 94.0% | 55.8% | 97.0% | HEALTHY |
| `slate_weather_pct` | 70.4% | 29.9% | 57.3% | LIVE_GAP |
| `slate_pitcher_vulnerability_pct` | 100.0% | 97.4% | 100.0% | HEALTHY |

_(numbers are non-NULL %; raw counts available from the console run.)_

## Snapshot / archetype tables

Counting **non-NULL centroid/SLG**, not rows (the handoff's warning: park had 114k rows with all-NULL centroids).

| table | rows | real (non-NULL) | players | span | class |
|---|---:|---|---:|---|---|
| `batter_park_archetype` | 5,003 | 0 (0.0%) of `feature_centroid_json` | 717 | 2026-05-27 -> 2026-06-02 | BROKEN_BACKFILL |
| `batter_form_archetype` | 0 | 0 (0.0%) of `feature_centroid_json` | 0 | (empty) | BROKEN_BACKFILL |
| `batter_pitch_type_splits` | 0 | 0 (0.0%) of `fb_slg` | 0 | (empty) | BROKEN_BACKFILL |

- **`batter_park_archetype`** — Rows present but ALL-NULL centroids -- venue lookup starved by live-only daily_slate (~3% of HRs resolve). backfill_park_archetype.py needs a venue-resolution fix first.
- **`batter_form_archetype`** — EMPTY (0 rows). backfill_form_archetype.py crashes on the NA-ambiguous boolean bug every iteration; never landed. Fix the NA bug first.
- **`batter_pitch_type_splits`** — EMPTY (0 rows). backfill_pitch_type_splits.py never ran on canonical (no etl_log). Clean re-run; code is sound post-2026-05-26 daily_picks-branch fix.

## Support tables

| table | rows | detail | class |
|---|---:|---|---|
| `season_batting` | 1,561 | 2024: 500 rows (last 2026-04-29 07:36:35); 2025: 500 rows (last 2026-04-29 07:36:36); 2026: 561 rows (last 2026-06-02 12:18:54) | HEALTHY |
| `career_batting` | 685 | career_hr_per_pa 98% \| career_barrel_pct 0% \| refreshed 2026-05-02 23:56:52 | LIVE_GAP |
| `pitcher_arsenals` | 3,040 | 2024: 1053 (last 2026-04-29 04:17:15); 2025: 1047 (last 2026-04-29 05:55:47); 2026: 940 (last 2026-06-02 12:17:10) \| avg_fb_velo 99% \| pitcher_name 0% | HEALTHY |

- **`season_batting`** — 2026 current; 2024/2025 frozen (static history). barrel/exit_velo/hr_fb 100% but SYNTHETIC (B1). tier col unused here.
- **`career_batting`** — career Statcast cols (barrel/exit_velo/hr_fb) 0% populated -- USE_CAREER_PRIOR Statcast shrinkage runs on nothing. Flag-gated, low priority. Quarterly refresh (last 2026-05-03) is fine.
- **`pitcher_arsenals`** — 2026 current. pitcher_name 0% (live lookup keys on pitcher_id, so harmless) -- B22 hygiene, not a model gap.

## Gap classification (detail)

### Broken-backfill (the work-list)

- **`recent_barrel_real_21d`** — 0% all eras. backfill_statcast_windows.py never landed on canonical (no etl_log row). B12's negative 21d/28d finding is therefore untrustworthy (scored on no data) -- B27 needs these.
- **`recent_xwoba_contact_21d`** — 0% all eras. Same as recent_barrel_real_21d.
- **`recent_iso_21d`** — 0% all eras. Same as recent_barrel_real_21d.
- **`recent_barrel_real_28d`** — 0% all eras. backfill_statcast_windows.py never landed (28d window).
- **`recent_xwoba_contact_28d`** — 0% all eras. Same as recent_barrel_real_28d.
- **`recent_iso_28d`** — 0% all eras. Same as recent_barrel_real_28d.
- **`fb_slg`** — 0% all eras. batter_pitch_type_splits is EMPTY (0 rows); backfill_pitch_type_splits.py never ran on canonical (no etl_log).
- **`fb_pa`** — 0% all eras. Same family as fb_slg.
- **`br_slg`** — 0% all eras. Same family as fb_slg.
- **`br_pa`** — 0% all eras. Same family as fb_slg.
- **`os_slg`** — 0% all eras. Same family as fb_slg.
- **`os_pa`** — 0% all eras. Same family as fb_slg.
- **`form_archetype_centroid_json`** — 0% all eras. batter_form_archetype is EMPTY (0 rows). CODE BUG: NA-ambiguous boolean in the form-archetype builder crashes every (date,window) iteration; the orchestrator's except swallows it. Fix before re-run.
- **`form_archetype_window`** — 0% all eras. Same family as form_archetype_centroid_json.
- **`form_archetype_n_hrs`** — 0% all eras. Same family as form_archetype_centroid_json.
- **`park_archetype_centroid_json`** — 0% all eras. batter_park_archetype has rows but ALL-NULL centroids. CODE/DATA BUG: venue lookup JOINs batter_hr_events->daily_slate, but daily_slate is live-only so only ~3% of HRs resolve a venue. Fix venue resolution before re-run.
- **`park_archetype_n_hrs`** — 0% all eras. Same family as park_archetype_centroid_json.

### Live-pipeline-gap

- **`humidity_pct`** — Only ~57% live. ~33% is domes (intentional NULL); the rest is non-dome open-meteo misses (B14 weather failures + B10 partial-weather). Low priority -- weather weight 0.08.
- **`slate_weather_pct`** — ~57% live -- tracks humidity_pct (requires temp+wind+humidity all non-NULL). Same root cause as humidity_pct.

### Intentional / known-dead (do NOT 'fix')

- **`xwoba_contact`** — 0% in 2025 by design -- the 2025 backfill substitutes recent_xwoba_contact_14d. ~99% live.
- **`pull_fb_pct`** — 0% all eras -- dead branch (adv dict never carries it). B20: drop, do not wire.
- **`recent_hr_14d`** — Legacy proxy, retired by the 2026-05-19 Form rebuild (replaced by recent_hr_10g). Kept for historical replay.
- **`recent_barrel_pct_14d`** — Legacy proxy = min(25, recent_ISO*100); replaced by recent_iso_30g. Historical-only.
- **`ev_trend_14d`** — Legacy proxy = (recent_SLG-season_SLG)*30; replaced. Historical-only.
- **`archetype_similarity`** — ~67% by structure -- NULL on the v1 matchup path / when no Statcast pitcher profile is available (skip-on-missing). Stable across eras.
- **`vegas_team_total_pct`** — 0% in 2025 (Vegas odds not persisted in the backfill). ~99% live.
- **`batting_order`** — ~77% live -- NULL for bench / roster_fallback / non-starters; only 1-9 starters carry a value (by design).
- **`vegas_team_total_raw`** — 0% in 2025 (not persisted). ~99% live.
- **`ev_trend`** — 0% all eras -- the real EV-trend slot is wired skip-on-missing and stays NULL until A2 builds the nightly EV ETL (CLAUDE.md false-alarm; handoff). NOT a re-run -- A2 is a build.

## Ranked backfill plan (what B32 executes)

Ordered by HR-prediction value (recent-window quality + `ev_trend` rank high for the form goal; the archetypes are bigger lifts). `status` is **live** — re-run this audit after a backfill and it flips to `PASS` once coverage clears 70% (this is the verification step missing every prior cycle).

| # | family | value | script | suspected failure | current | status |
|---|---|---|---|---|---:|---|
| 1 | Recent-window quality (21d / 28d) | HIGH | `etl/backfill_statcast_windows.py` | PATH BUG / never-run-on-canonical | 0.0% | FAIL (not landed) |
| 2 | ev_trend (real EV trend, A2) | HIGH | `(none -- this is a BUILD, not a re-run: A2 nightly EV ETL)` | INTENTIONAL NULL until A2 ships | — | N/A (build, not a re-run) |
| 3 | Pitch-type SLG splits (fb/br/os) | MEDIUM-HIGH | `etl/backfill_pitch_type_splits.py` | PATH BUG / never-run-on-canonical | 0.0% | FAIL (not landed) |
| 4 | Form-archetype centroid | MEDIUM (bigger lift) | `etl/backfill_form_archetype.py` | CODE BUG (NA-ambiguous boolean) -- crashes every (date,window) iteration; the orchestrator's line-491 except swallows it (prints message only, no traceback) | 0.0% | FAIL (not landed) |
| 5 | Park-archetype centroid | LOWER | `etl/backfill_park_archetype.py` | CODE/DATA BUG -- venue lookup JOINs batter_hr_events->daily_slate (live-only); only ~3% of HRs resolve a venue, so centroids are all-NULL even on canonical (proven by the live nightly rows) | 0.0% | FAIL (not landed) |

### #1 — Recent-window quality (21d / 28d)

- **Value:** HIGH -- directly unblocks B27's form window-sweep. B12's negative 21d/28d finding is untrustworthy (those columns are empty, so the variant scored on no data).
- **Script:** `etl/backfill_statcast_windows.py`
- **Suspected failure mode:** PATH BUG / never-run-on-canonical. Script is sound (--db -> DB_PATH). No etl_log row exists.
- **Fix / action:** Clean re-run to canonical (HR_BETS_DB set or --db). ~90-100 min cold cache. Decide whether to also wire nightly (currently backtest-only).
- **Current coverage** (`2025_backfill`, cols `recent_barrel_real_21d`, `recent_iso_21d`, `recent_barrel_real_28d`, `recent_iso_28d`): 0.0% → **FAIL (not landed)** (target 70%)

### #2 — ev_trend (real EV trend, A2)

- **Value:** HIGH -- the recent contact-quality signal B27/A2 want, decorrelated from season power.
- **Script:** `(none -- this is a BUILD, not a re-run: A2 nightly EV ETL)`
- **Suspected failure mode:** INTENTIONAL NULL until A2 ships. No existing backfill script -- the column is wired skip-on-missing.
- **Fix / action:** Build A2: rolling EV in nightly ETL (etl/etl_nightly.py), recent EV - season EV -> pick_inputs.ev_trend. Off the noon critical path.

### #3 — Pitch-type SLG splits (fb/br/os)

- **Value:** MEDIUM-HIGH -- the matchup pitch-type archetype sub-signal (score_matchup v2).
- **Script:** `etl/backfill_pitch_type_splits.py`
- **Suspected failure mode:** PATH BUG / never-run-on-canonical. batter_pitch_type_splits is EMPTY; no etl_log. Code is sound post-2026-05-26 daily_picks-branch fix.
- **Fix / action:** Clean re-run to canonical (~90-180 min Statcast pull), then load into pick_inputs (load_picks_to_db / backfill_2025 --force).
- **Current coverage** (`2025_backfill`, cols `fb_slg`, `br_slg`, `os_slg`): 0.0% → **FAIL (not landed)** (target 70%)

### #4 — Form-archetype centroid

- **Value:** MEDIUM (bigger lift) -- form-archetype matchup signal; conceptually aligned with B27 but a separate centroid calc.
- **Script:** `etl/backfill_form_archetype.py`
- **Suspected failure mode:** CODE BUG (NA-ambiguous boolean) -- crashes every (date,window) iteration; the orchestrator's line-491 except swallows it (prints message only, no traceback). batter_form_archetype is EMPTY.
- **Fix / action:** FIX FIRST: add traceback.print_exc() at the except to locate the NA, fix the unsafe boolean on a parquet-loaded nullable Float64/boolean in the features_v2 form-archetype builder. THEN re-run.
- **Current coverage** (`2025_backfill`, cols `form_archetype_centroid_json`): 0.0% → **FAIL (not landed)** (target 70%)

### #5 — Park-archetype centroid

- **Value:** LOWER -- park weight is only 0.04; bigger lift and needs a data-model change first.
- **Script:** `etl/backfill_park_archetype.py`
- **Suspected failure mode:** CODE/DATA BUG -- venue lookup JOINs batter_hr_events->daily_slate (live-only); only ~3% of HRs resolve a venue, so centroids are all-NULL even on canonical (proven by the live nightly rows).
- **Fix / action:** FIX FIRST: enrich batter_hr_events with home_team at Statcast ETL time + a static home_team->venue map (cheap), OR a game_pk->venue API lookup. THEN re-run.
- **Current coverage** (`2025_backfill`, cols `park_archetype_centroid_json`): 0.0% → **FAIL (not landed)** (target 70%)

## Re-running (for B32 verification)

```powershell
$env:HR_BETS_DB = "C:\dev\Claude\Projects\data\hr_bets.db"   # or pass --db
python -m diagnostics.data_integrity_audit --md docs\data_integrity_audit_<date>.md
```

After each B32 backfill, re-run: the broken family's `current` coverage rises and its `status` flips to `PASS (landed)`. That closes the loop the prior cycles never did.
