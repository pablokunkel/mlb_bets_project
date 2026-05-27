# Audit Findings — 2026-05-26

## Summary
- **6 HIGH** findings, **11 MEDIUM**, **8 LOW**
- **Confidence: high** for Sections 1, 2, 3, 6, 9; **medium** for Sections 4, 5, 7, 8, 10, 11 (sampled rather than exhaustive)
- **Estimated effort to address HIGH findings: 8–14 hours** (one is data-only repair; one is a non-trivial backfill rewrite; four are 1–3 hour code fixes)

The dominant pattern is the one the brief warned about: **backfill code reads live-only tables and silently produces no-op output**. The park-archetype Phase 2 backfill is the clearest case — 114,495 rows written, **100% with NULL centroid**, because the JOIN target (`daily_slate`) only has 366 of the 4,881 `game_pk`s in the historical HR-event window. The pitch-type backfill was already patched (PR #96) for the same shape, but the park-archetype fix has not landed.

The second pattern is **the backtest is silently divergent from production scoring**: 23 columns are written to `pick_inputs` that `backtest_factors.rescore_row` never reads. Today those columns are all NULL in the 2025 backfill anyway, so the divergence is latent — but as soon as the Phase 2 sub-signals are populated, the harness's ship/hold rule will be graded on a different scoring function than production runs.

## How to triage

1. Ship the **Quick-fix candidates** section first — those are 1-line surface-level fixes that don't require additional context.
2. Triage HIGH findings in this order: **HIGH-1** (park-archetype data integrity) is the only HIGH that affects live picks today; **HIGH-2 / HIGH-3** matter only if/when the relevant flags flip on; **HIGH-4 / HIGH-5 / HIGH-6** are data-integrity probes the smoke suite should pin.
3. MEDIUM findings cluster around anchors and silent error swallowing — batch them by file rather than by severity.
4. The "What you didn't audit" section names follow-up scopes I left intentionally on the table.

## HIGH severity findings

### HIGH-1 — `etl/backfill_park_archetype.py` + `features_v2.compute_batter_park_archetype` produces 100% NULL centroids
- **What's wrong:** The 2026-05-26 backfill wrote **114,495 rows** to `batter_park_archetype` across the 188-date 2025 season — every single one has `feature_centroid_json IS NULL`. Root cause: `features_v2.compute_batter_park_archetype` at `features_v2.py:768-775` does `LEFT JOIN daily_slate ds ON ds.game_pk = bhe.game_pk` to resolve each HR's venue. But `daily_slate` is a live-only artifact — it has only 366 distinct `game_pk`s (April–May 2026), while `batter_hr_events` covers 4,881 distinct `game_pk`s. Only **120 of 4,881 (2.5%)** `game_pk`s from `batter_hr_events` exist in `daily_slate`, so the venue resolves to empty for the rest, the HR is dropped at `features_v2.py:788-789`, and every batter ends up below `PARK_ARCHETYPE_MIN_HRS=10` with no centroid. This is **the exact pattern the brief warned about** — and it's the same shape as the pitch-type bug PR #96 already patched.
- **Why it matters:** `pick_inputs.park_archetype_centroid_json` is **100% NULL** for all 69,567 rows, so `backtest_park_archetype.py` has no data to backtest. Phase 3 of the park-archetype rollout is blocked. The `hr_events` table has a `venue` column directly (Statcast pitch-level), and `pick_inputs` could also join through `daily_picks.game_pk → hr_events.venue` for historical dates — neither is currently used.
- **Proposed fix:** Change the venue resolution in `features_v2.compute_batter_park_archetype` to UNION the venue from `hr_events.venue` (which has 658 game_pks, 295 in daily_slate but the rest have venue inline) and `daily_slate.venue`. If a HR is in `hr_events` it has venue at the row level; otherwise it can fall through to `daily_slate`. For HRs in neither, the existing skip-on-missing absorbs them cleanly. Re-run `python -m etl.backfill_park_archetype` after.
- **Suggested smoke pin:** Add `pin_park_archetype_table_coverage_not_zero` — assert `SELECT COUNT(*) FROM batter_park_archetype WHERE feature_centroid_json IS NOT NULL > 0` after a backfill run.

### HIGH-2 — `backtest_factors.rescore_row` ignores 9 production-scored signals
- **What's wrong:** `backtest_factors.rescore_row` (`backtest_factors.py:130-233`) reconstructs a batter dict for re-scoring with the current `score_*` functions but **does NOT read** the following persisted `pick_inputs` columns:
  - `fb_slg / fb_pa / br_slg / br_pa / os_slg / os_pa` — pitch-type sub-signal inputs for `score_matchup`'s `_compute_xslg_vs_arsenal`.
  - `park_archetype_centroid_json / park_archetype_n_hrs` — park-archetype centroid for `score_park`'s archetype path.
  - The 21d/28d real-Statcast variants (`recent_*_21d`, `recent_*_28d`) — written for B12 backtest experiments.
- **Why it matters:** Today's USE flags are all False, so the divergence is invisible. But the harness drives the ship/hold decision for those flags; if `USE_ARSENAL_SUBSIGNAL` or `USE_PARK_ARCHETYPE` flips on without first reading them in `rescore_row`, **the backtest will silently underestimate the signal** by scoring re-runs with `None` inputs while production scores with real values. The form-archetype path is already wired in (lines 170-174) — pitch-type and park-archetype were missed.
- **Proposed fix:** Extend `rescore_row` to read all 8 columns and attach them to the `batter` dict. Add `park_archetype_centroid` parsed from JSON via the same `_parse_centroid_json` helper used for form. Update the `load_history` SQL at line 88-120 to SELECT the missing columns. Audit whether `bats` / `throws` should also be persisted (they're hardcoded to "R" on lines 156, 207 — comment at 184-190 acknowledges this).
- **Suggested smoke pin:** `pin_pick_inputs_columns_all_rescore_readable` — for every column in `pick_inputs` schema with a corresponding scoring input on the `batter` dict, assert `rescore_row` reads it.

### HIGH-3 — `daily_lineup.batting_order > 9` (428 rows) — sentinel values polluting score paths
- **What's wrong:** 428 rows in `daily_lineup` have `batting_order` set to 10/11/12/13/14 (4 dates across April–May 2026), with `lineup_source = NULL`. These are clearly sentinel/fallback values escaping the lineup-fetcher. The smoke probe at `tests/smoke.py:4163` already flags this case as a known issue ("428 rows have batting_order > 9"), but **the fetch logic that produces them has not been patched** — every day the morning ETL adds more.
- **Why it matters:** `score_lineup_position` at `score_batters.py:1228` only honors `1 <= batting_order <= 9`; values 10+ fall through to return `35.0` (the fallback for unknown values). Functionally the score is the right magnitude, but rows with sentinel values may also escape the `is_likely_out` / IL filter and the daily picks board.
- **Proposed fix:** Trace the lineup-source pipeline in `fetch_daily_data.fetch_lineups_for_date` — find where sentinel 10+ batting orders enter and convert them to `None` (or sentinel `lineup_source`). Backfill the 428 existing rows to set `batting_order=NULL` and `lineup_source='roster_fallback'`. The smoke probe should then flip green.
- **Suggested smoke pin:** Already exists in `tests/smoke.py:4163`. The pin should become an **HALT** (not WARN) once the fix lands.

### HIGH-4 — `pick_inputs.pitcher_fb_pct_allowed` has 23 rows with values > 100 (mathematically impossible)
- **What's wrong:** 23 `pick_inputs` rows on dates 2025-09-21 (12 rows) and 2025-09-27 (11 rows) have `pitcher_fb_pct_allowed = 102.5`. All trace back to Robert Gasser with `opp_pitcher_id = 0`. Likely cause: the Savant `fbld` field was parsed with `pct < 1` rescaling (`features_v2.py:340-341`), but `fbld = 1.025` (a 102.5% sum of fly_ball + line_drive, almost certainly a Savant data glitch for a tiny sample) passed the `pct < 1` guard and was multiplied by 100.
- **Why it matters:** When `USE_ARSENAL_SUBSIGNAL=False` (current default) this affects only `compute_slate_context`'s vulnerability percentile rank (which is robust to outliers). When the flag flips on, a 102.5% FB-allowed value would feed directly into `score_matchup`'s within-slate ranking and inflate vulnerability rank for every Gasser-matchup batter on those days.
- **Proposed fix:** In `features_v2.fetch_pitcher_fb_bulk` (line 338-342), guard against `pct > 100` after the `pct < 1` rescale — clip to 100 or drop the row. Backfill the 23 historical rows. Add a smoke probe asserting `MAX(pitcher_fb_pct_allowed) <= 100` across `pick_inputs`.
- **Suggested smoke pin:** `pin_pick_inputs_pitcher_fb_pct_allowed_in_range` — assert 0 rows with `pitcher_fb_pct_allowed > 100 OR pitcher_fb_pct_allowed < 0`.

### HIGH-5 — `score_batters.score_power` anchors compress 75%+ of empirical `hr_fb_pct` into score=0
- **What's wrong:** `score_power` at `score_batters.py:683` uses `min_max_scale(hr_fb_pct, 8, 20)`. The empirical 2025 distribution from 54,339 `pick_inputs` rows has p50 = 6.0 and p75 = 7.9 — **75%+ of all observations score 0** on this metric. The anchor min of 8 was claimed in the docstring (lines 660-666) to be "league avg 12 / elite 18+" but real league average is 6.0. Per the brief's HIGH-trigger (line 141): "Anchor that compresses 90%+ of empirical distribution into one tail."
- **Why it matters:** `score_power` returns `np.mean(scores)` so this term contributes 0 for 75% of batters, dragging power scores down disproportionately for anyone whose other power inputs aren't elite. The 2026-05-03 anchor re-tune commit was intended to fix this by tightening — it went too tight on `hr_fb_pct`.
- **Proposed fix:** Re-tune to `min_max_scale(hr_fb_pct, 4, 12)` based on actual p25=4.6 / p95=11.9 distribution. Document the empirical-distribution reference in the WEIGHT_REFIT_LOG. Verify the change doesn't tank power's lift in `backtest_power_inputs.py` before flipping it in production.
- **Suggested smoke pin:** `pin_power_anchors_match_empirical_p25_p95` — for each anchor on `score_power`, assert that the [lo, hi] band brackets the [p25, p95] of the 2025 backfill.

### HIGH-6 — Park archetype design doc + backfill doc are wrong about Phase 2 completion
- **What's wrong:** `docs/park_archetype_design.md:366-369` documents the Phase 2 reviewer check as "table is densely populated for active batters; the new column on pick_inputs is populated." Reality: 100% NULL centroids in `batter_park_archetype`, 0% non-NULL in `pick_inputs.park_archetype_centroid_json`. Phase 2 carry-forward (PR #92) reports success but the table is empty. The PARK_ARCHETYPE Phase 2 milestone is effectively unfinished.
- **Why it matters:** The next agent reading the design doc will assume Phase 2 is done and try to run Phase 3 against empty data. Doc + code disagree on whether the rollout phase is complete.
- **Proposed fix:** Once HIGH-1 is fixed and the backfill re-run produces non-zero centroids, update the design doc to indicate when Phase 2 was actually completed. Add a smoke probe linking the design-doc claim to a DB assertion (so the doc can't go stale silently).
- **Suggested smoke pin:** Same as HIGH-1 — the table-coverage pin doubles as the doc claim's enforcement.

---

## MEDIUM severity findings

### MED-1 — `pitcher_hh_pct` clipping at anchor edges (10% of rows at exactly 25.0)
- **Where:** `pick_inputs.pitcher_hh_pct`, 6222 rows (out of 54,339 in 2025 backfill) at exactly 25.0; 432 at exactly 50.0.
- **Issue:** `score_matchup` uses `min_max_scale(hh, 25, 50)` at `score_batters.py:851`. The clustering at the anchor boundaries suggests something upstream (probably `LEAGUE_AVG_PITCHER = 35` fallback for missing-data pitchers, then a clip) is producing literal 25.0/50.0 values that pin the score to 0/100. Inspect the pitcher-stat fetcher and provenance flag the source.
- **Proposed fix:** Investigate `_fetch_pitcher_stats_mlb`'s default-handling. If it's returning 25 as a sentinel for missing data, replace with `None` so `score_matchup`'s `if hh is not None and hh > 0` guard skips the term instead of scoring it as 0.

### MED-2 — `daily_picks` without matching `pick_inputs` row (2,941 rows; 19 of them SELECTED)
- **Where:** 2,941 `daily_picks` rows lack a corresponding `pick_inputs` row; 19 of those have `selected=1` (in the 8-pick card).
- **Issue:** `load_picks_to_db.py:251` skips persisting pick_inputs when `pid == 0` OR all power inputs are missing/zero. The 19 SELECTED rows are picks that ended up on the card despite having no input audit data — diagnostics, backfills, and refits silently ignore them.
- **Proposed fix:** Audit whether the 19 selected-orphan rows are real bench-pick artifacts (T4 untiered, no Statcast data) or bugs in the upstream fetcher. Persist a thin pick_inputs row even for these so backtest replay is consistent.

### MED-3 — `outcomes` with no matching `daily_picks` (16,712 rows, all 2025-03-15 → 2025-04-21)
- **Where:** 16,712 outcomes rows reference batters with no matching `daily_picks` row.
- **Issue:** Early-2025 outcomes were ingested before `daily_picks` were generated for those dates. This is a known historical artifact — but the `INNER JOIN` in `backtest_factors.load_history` quietly filters them out without surfacing the count. The harness's effective sample size is lower than the row count suggests.
- **Proposed fix:** Add a print in `load_history` reporting `len(outcomes_without_daily_picks)` so the divergence is visible. Or backfill `daily_picks` for the early-March 2025 dates via `etl/backfill_2025.py --start 2025-03-15`.

### MED-4 — `weather_source = 'api_failed_default'` count crossed threshold (>5/day) on 10+ May 2026 dates
- **Where:** 1,391 May 2026 rows with `weather_source='api_failed_default'`. Peak: 2026-05-16 had 237 failures.
- **Issue:** Either Open-Meteo had widespread outages or the rate-limit handling failed silently — `etl_morning.py:512-516` catches the exception and emits a neutral dome-style weather row (`weather_source='api_failed_default'`). Score_weather treats those as 50.0 baseline. The probe at brief Section 9 #9 says >5/day is a real issue; we're 5–50x over.
- **Proposed fix:** Investigate the weather-fetcher's rate limiting. Add a `WARN` to the morning ETL status table when failures > 5% of slate. Backfill May 2026 dates with archived weather (`etl/historical_calibration.fetch_historical_weather`) so backtest replay isn't dominated by neutral 50.0 weather scores.

### MED-5 — `recent_iso_30g = 3.0` outlier (impossible value)
- **Where:** Max value in `pick_inputs.recent_iso_30g` is 3.0 (ISO physically cannot exceed 1.0). At least one row exists.
- **Issue:** ISO is `(TB - H) / AB`; max possible is 3.0 (all 4-base hits → 3 extra bases per AB) which is only achievable with a 1-AB sample. Suggests a denominator-of-1 row or division shortcut needs sample-size gating.
- **Proposed fix:** In the recent-stats fetcher, require `AB >= 10` before reporting `recent_iso_30g`; otherwise return `None`.

### MED-6 — `pick_inputs.ev_trend` is 100% NULL despite being added 2026-05-19
- **Where:** `ev_trend` column added in PR #56 but no ETL has ever populated it. 0 of 69,567 rows are non-NULL.
- **Issue:** `score_form` reads `ev_trend` at `score_batters.py:1187`. The input is wired into scoring but no producer exists. Documented in `How_The_HR_Model_Works.md:163` as "Currently always NULL — gated on the A2 nightly Statcast ETL." Phantom column per the brief's HIGH criteria for Section 1.
- **Proposed fix:** Either implement A2 (nightly Statcast EV trend) or strip the `ev_trend` term from `score_form` until A2 ships. Don't keep an always-None input in production scoring — it's documentation drift.

### MED-7 — Silent error swallowing in `load_picks_to_db.py:343`
- **Where:** `load_picks_to_db.py:343` — `except Exception: pass` inside the `pick_inputs` insert loop.
- **Issue:** If a row's pick_inputs insert fails (e.g. column mismatch after schema migration), the exception is swallowed and the row silently doesn't get persisted. This is exactly how 2941 daily_picks rows have no matching pick_inputs (MED-2 above).
- **Proposed fix:** Replace with `except Exception as e: print(f"[WARN] pick_inputs insert failed for {date_str}/{pid}: {type(e).__name__}: {e}")`. Don't swallow without diagnostic.

### MED-8 — `compute_batter_park_archetype` and `compute_batter_form_archetype` don't log traceback on failure
- **Where:** `features_v2.py:777-779` (park archetype), `features_v2.py:1499-1501` (form archetype).
- **Issue:** Catches a bare `Exception` and prints only `f"[features_v2] ... failed: {e}"`. The form-archetype NA-boolean bug was opaque for hours because of this pattern.
- **Proposed fix:** Add `traceback.print_exc()` in both locations (and in `etl/backfill_*.py` orchestrators that catch and continue at `backfill_park_archetype.py:259-261` and `backfill_pitch_type_splits.py:297-300` — only `backfill_2025.py:443` and `backfill_form_archetype.py` do this correctly).

### MED-9 — `LEAGUE_AVG_PITCHER` defaults mismatch documented 2026 league averages
- **Where:** `score_batters.py:465-474`. Comment at line 413-415 acknowledges: "Real 2026 HR/9 is closer to 1.27, hard-hit% closer to 39%."
- **Issue:** Default values (`hr_per_9=1.2, hard_hit_pct_allowed=35`) match the older 2024-era league averages, not 2026. The drift is documented but not fixed.
- **Proposed fix:** Update to current league averages from Savant aggregates (`hr_per_9=1.27, hard_hit_pct_allowed=39, fb_pct_allowed=33, k_per_9=8.5`). The comment says "bump after a refit cycle when we want to update" — A1 just ran, so this is the moment.

### MED-10 — Doc drift: `How_The_HR_Model_Works.md` says `score_form` has 4 inputs, B11 dropped one
- **Where:** `How_The_HR_Model_Works.md:156` says "Four inputs"; lines 158-163 list four. But B11 (2026-05-26, code at `score_batters.py:1156-1162`) dropped `recent_avg_30g`. Current form is 3 inputs: `recent_hr_10g`, `recent_iso_30g`, `ev_trend`.
- **Proposed fix:** Update `How_The_HR_Model_Works.md` to say three inputs; remove the `recent_avg_30g` row; reference B11 in the change history.

### MED-11 — Form-archetype + pitch-type-splits backfill orchestrators print "no batters in daily_lineup" for 2025 dates
- **Where:** `etl/backfill_pitch_type_splits.py:191` returns reason="no batters in daily_lineup" for any 2025 date even though the UNION fix routes around it. Pre-fix the message was accurate; post-fix it's a stale message in the no-batters branch (which can still trigger for legitimately empty dates).
- **Proposed fix:** Update the reason string to reflect the UNION semantics — e.g., "no batters in daily_lineup OR daily_picks" — so debugging is clearer.

---

## LOW severity findings

### LOW-1 — `PARK_FEATURE_STATS` computed once at module import time
- **Where:** `features_v2.py:604` — `PARK_FEATURE_STATS = _compute_park_feature_stats()` runs at import.
- **Issue:** If `etl/park_factors_seed.py` changes, the in-memory stats stay stale until the Python process restarts. Documented at lines 602-604 but worth a comment in the function calling it.
- **Proposed fix:** Add a refresh helper / comment on cache invalidation policy.

### LOW-2 — `score_lineup_position(None)` returns 35.0, not 50.0 (silent "no opinion")
- **Where:** `score_batters.py:1227`. Convention elsewhere is "no inputs → 50.0".
- **Issue:** Returning 35.0 for None feels like a deliberate penalty for bench-style rows but isn't documented as such.
- **Proposed fix:** Either return 50.0 (per the convention) or document why None gets penalized.

### LOW-3 — `_fmt(pd.NA)` returns `"<NA>"` not "n/a" in diagnostic tables
- **Where:** All `diagnostics/backtest_*.py` `_fmt` helpers (e.g., `backtest_form_archetype.py:417`).
- **Issue:** `_fmt(pd.NA)` falls through the `isinstance(x, float)` guard and reaches the `f"{x:.{prec}f}"` formatter, which returns `"<NA>"` (a pandas type formatter) rather than the intended "n/a".
- **Proposed fix:** Add `pd.isna(x)` check before the isinstance guard.

### LOW-4 — Stale archived `_archive_docs_2026-05-02_DO_NOT_USE/AUDIT_REPORT.md` says `USE_PER_PLAYER_STATCAST=False`
- **Where:** Multiple archived docs claim `USE_PER_PLAYER_STATCAST=False` (e.g., `AUDIT_REPORT.md:92`).
- **Issue:** Current code at `generate_picks.py:88` has it as `True`. The archive folder is named DO_NOT_USE but is still grep-hit-able.
- **Proposed fix:** Move to a `legacy_archive/` directory outside the project root, or add a top-of-file disclaimer to each archived doc.

### LOW-5 — `Sutter Health Park` CF bearing has a "verify" comment
- **Where:** `score_batters.py:90` — `"Sutter Health Park": 340,  # Sacramento A's temp home (2025-26); CF roughly NNW — verify`.
- **Issue:** Comment flags it as unverified. Affects wind-alignment scoring for any A's-at-Sacramento game. Low-frequency game but score_wind uses this bearing.
- **Proposed fix:** Confirm CF bearing against Google Maps; remove the "verify" comment.

### LOW-6 — `PARK_ARCHETYPE_DIST_NEAR = 0.0` and resulting `(PARK_ARCHETYPE_DIST_FAR - PARK_ARCHETYPE_DIST_FAR)` arithmetic
- **Where:** `score_batters.py:948-950` — `min_max_scale(PARK_ARCHETYPE_DIST_FAR - dist, PARK_ARCHETYPE_DIST_FAR - PARK_ARCHETYPE_DIST_FAR, ...)`.
- **Issue:** The expression `PARK_ARCHETYPE_DIST_FAR - PARK_ARCHETYPE_DIST_FAR = 0` is a deliberate inverse-mapping trick but reads as a typo. Add a comment to make the intent obvious or use literal `0.0`.
- **Proposed fix:** Replace `PARK_ARCHETYPE_DIST_FAR - PARK_ARCHETYPE_DIST_FAR` with `0.0` and a `# inverse mapping: 0 distance = max score` comment.

### LOW-7 — `min_max_scale(0.350, 0.500)` pitch-type anchor lo > xSLG floor
- **Where:** `score_batters.py:893` — pitch-type sub-signal scaling.
- **Issue:** League-avg pitch-type SLG is in the 0.380-0.420 range. An anchor lo of 0.350 means most batters score above 0 but the tail compression isn't validated against 2025 data (since the column is 100% NULL).
- **Proposed fix:** Once HIGH-1 + pitch-type backfill produce real data, re-tune to actual p25/p95 of the empirical distribution.

### LOW-8 — `score_wind` for switch-hitters averages RF/LF alignment, not max
- **Where:** `score_batters.py:1322-1325`.
- **Issue:** A switch hitter facing a LHP bats from the right side, vice versa for RHP. Averaging both targets is a reasonable proxy when handedness isn't known, but the batter's actual side IS known — and `bats="S"` callers could pass `pitcher_throws` to pick the correct target.
- **Proposed fix:** If pitcher throws is available, set `target = (cf_bearing - 45)` for S vs LHP and `+ 45` for S vs RHP. Cosmetic — wind weight is small.

---

## What you DIDN'T audit

- **`refit_weights.py`** (32 KB, ~750 lines). The A1 refit logic is documented in WEIGHT_REFIT_LOG.md and the candidates match WEIGHT_CONFIGS, but I did not validate the implementation against the published numbers. A follow-up audit should re-run `refit_weights.py --update` against the current DB and verify the printed coefficients match the doc's table.
- **`mlb_hr_bet_site` (the Cloudflare site)**. Static HTML/JSON, but the contracts (`mlb_hr_bet_site/data/*.json`) are populated by `export_site_data.py`. I confirmed the column-set the site reads agrees with `daily_picks`+`pick_inputs` schema but did not audit the export logic itself for stale fields or silent NULL handling.
- **`fetch_daily_data.py`** (71 KB, ~1700 lines). I sampled the API-fetcher patterns but didn't read end-to-end. The morning ETL hot path is in this file; a focused audit could surface more silent-swallow patterns and confirm the IL/scratch filter's contract.
- **`pitcher_profile.py`** beyond the audit-relevant entry points. The `_aggregate_victim_profile` / `score_matchup_v2` helpers are well-documented but I didn't validate every helper for None propagation.
- **Cross-platform Section 11**: I confirmed argparse % escaping is consistent (PR #95 fix is the only site) and pathlib usage is uniform. I did not test on actual Python 3.14 or audit `subprocess.Popen` calls.
- **R2 sync / `infra/r2_sync.py`**. Touches DB upload/download paths; I noticed it's in the silent-swallow file list but didn't analyze.
- **The `etl/historical_calibration.py` table-builder**. Build path for the `historical_calibration` materialized join. Out of scope for this pass.
- **Test coverage gaps for HIGH-1 fix.** When HIGH-1 lands, `tests/smoke.py` should grow a real assertion that backfill produces non-zero centroids; I did not draft the pin itself.

---

## Quick-fix candidates

These are 1- to 3-line fixes the user can batch into a single small PR:

1. **`load_picks_to_db.py:343`** — replace `except Exception: pass` with logged catch (MED-7).
2. **`How_The_HR_Model_Works.md:156-163`** — change "Four inputs" → "Three inputs", remove the `recent_avg_30g` table row (MED-10).
3. **`score_batters.py:465-474`** — bump `LEAGUE_AVG_PITCHER` HR/9 to 1.27 and hard_hit% to 39 (MED-9).
4. **`score_batters.py:90`** — verify the Sutter Health Park CF bearing and drop the "verify" comment (LOW-5).
5. **`score_batters.py:948-950`** — replace `PARK_ARCHETYPE_DIST_FAR - PARK_ARCHETYPE_DIST_FAR` with `0.0` + comment (LOW-6).
6. **`features_v2.py:340-342`** — add `pct > 100` guard after the `pct < 1` rescale (HIGH-4 — only 3 lines).
7. **`docs/park_archetype_design.md:366-369`** — strike the "table densely populated" claim until HIGH-1 lands (HIGH-6).
8. **`etl/backfill_park_archetype.py:259-261`** + **`etl/backfill_pitch_type_splits.py:297-300`** — add `traceback.print_exc()` (MED-8).

The remaining HIGH findings (HIGH-1, HIGH-2, HIGH-3, HIGH-5) need either non-trivial backfill rework or anchor re-tuning — those should each get their own PR with backtest validation.
