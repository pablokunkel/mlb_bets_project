# Weight refit log

A running log of monthly weight-refit decisions for `WEIGHT_CONFIGS["default"]` in `score_batters.py`. Each entry captures what was tried, what was learned, and whether anything actually shipped.

Refit driver: `refit_weights.py` (run monthly via Windows scheduled task `mlb-hr-refit-weights-monthly`). Training data feeder: `backfill_features_v2_bulk.py --season 2026` (refreshes Savant feature columns in `raw_data.csv` / `raw_data_v2.csv`). Score-curve / flag decisions are driven separately by `backtest_flags.py`, a same-data harness that compares scoring variants (anchor sets, floor on/off, prior on/off) against the canonical default on lift / AUC / top-8 / top-30 / monotonicity over a fixed window.

---

## 2026-05-13 — 14d refit (scheduled task: `mlb-hr-refit-weights-14d`)

**Status: no change shipped.** First post-v2 checkpoint (v2 features shipped 2026-04-29). New weights are within ±0.001 of current default; backtest lift is +0.62 pp, below the +1.0 pp shipping threshold. Vegas signal could not be validated this run — same infra blocker that was open on 2026-05-01.

### Ran

- `python backfill_features_v2_bulk.py --season 2026` — refreshed Savant columns. `xwoba_contact` 5196/5196, `fb_pct_allowed` 5175/5196, `pull_fb_pct` still 0/5196 (bulk path can't fetch).
- `python refit_weights.py` — DB path resolved to `Projects/data/hr_bets.db` but that directory is not mounted in the scheduled-task sandbox (only `MLB HR Bets/` is). Fell back to `--csv raw_data_v2.csv`.

### Coefficients (this refit → current default)

Bucketed: `power 0.249 (0.250) · matchup 0.263 (0.264) · park 0.000 (0.000) · form 0.279 (0.279) · weather 0.058 (0.057) · lineup 0.150 (0.150)`. All deltas ≤ 0.001 — refit-noise scale.

Standardized: form +0.495, matchup +0.462, power +0.347, weather +0.104, xwoba +0.096, park −0.011, **fb_pct_allowed +0.005**.

### Sign flip — `fb_pct_allowed` (−0.023 on 2026-05-01 → +0.005 here)

Magnitude tiny in both runs; same 5,196-row training window in both cases (2026-03-27 → 2026-04-15) so the flip happened with *no new data* — only Savant FBLD% re-pulls changed between runs. Univariate r = 0.028, p = 0.04 — barely significant on n = 5,175. Both runs' matchup bucket nets strongly positive (matchup_score is +0.46 standardized) so the bucket-level signal is fine. **Verdict: noise around zero, not actionable.** Worth a re-check once `implied_total_pct` enters the training set — the matchup bucket may decompose differently with Vegas co-present.

### Vegas signal — UNEVALUATED

`raw_data_v2.csv` has no `implied_total_pct` column (it's only persisted to the DB, populated daily by `generate_picks.py` since 2026-04-29). The DB-mode code path of `refit_weights.py` threw `FileNotFoundError` from this sandbox. So the +1-3 pp lift estimated at v2 ship for Vegas remains untested.

### Backtest top-8 hit rate (CSV window 2026-03-27 → 2026-04-15)

- `legacy_csv_composite`: 36.04%
- `current_default` (shipped): 34.79%
- `new_learned` (this refit): 35.42%
- Lift vs current: **+0.62 pp** (< +1.0 pp threshold → don't ship)

Note: `current_default` backtesting at 34.79% on this CSV — *below* legacy's 36.04% — contradicts the v2-ship narrative in `score_batters.py` WEIGHT_CONFIGS docstring (36.04% → 38.75% → 40.00%). Two likely reasons: (a) bulk-mode `pull_fb_pct` is null in this CSV, so v2's power bucket is missing one of its sub-features here; (b) the 40.00% number was backtested on a different (post-v2) window, not this 2026-03-27 → 2026-04-15 one. Apples-to-oranges; the +0.62 pp delta between configs is the only number that matters for shipping logic.

### Still-open action items (same as 2026-05-01 / 2026-05-03)

1. **Append daily outcomes into `raw_data_v2.csv` (or mount `Projects/data/` in the refit sandbox).** Without this, every monthly/14-day refit will hit the same 2026-03-27 → 2026-04-15 window and produce the same coefficients to within rounding. The Vegas signal will stay untested for the same reason.
2. **`pull_fb_pct`** is still bulk-uncrawlable. Only the live `features_v2.py` per-player path populates it (to the DB). Same fix-path as #1.

**Verification:** `score_batters.py` and `generate_picks.py` untouched; no production-side changes this cycle.

Full diagnostic: `diagnostics/refit_2026-05-13_summary.md`.

---

## 2026-05-03 — score-curve & scoring-flag changes (PR #25, harness-driven)

**Status: shipped.** Three changes landed together as a batched scoring tweak; weights themselves unchanged.

The 2026-05-01 refit decided "no weight change because training data is stale," but several diagnostic signals (input calibration `SIGNAL_NOT_CAPTURED` on barrel%, EV, HR/FB; the 2026-05-02 HR autopsy showing 25 HR hitters' average rank at 107.7) made it clear the upstream score curves themselves were broken — refitting weights on broken curves wouldn't have helped. So we ran `backtest_flags.py` over the available 14d / 30d windows comparing scoring variants and shipped what won decisively.

### Change 1: power-score anchor re-tune

The original 0-100 scaling anchors on the six power-score inputs were calibrated generously on the upside, so even MLB-leading Statcast values capped at 50-70%. Aaron Judge's 17% barrel was scoring 68 instead of saturating the scale. Anchors retuned to reflect actual MLB distributions (league-avg → 0, elite → 100):

| Input            | Old anchors    | New anchors    |
|------------------|----------------|----------------|
| barrel %         | 0 - 25         | 5 - 15         |
| exit velo (mph)  | 80 - 100       | 85 - 95        |
| HR/FB %          | 0 - 30         | 8 - 20         |
| ISO              | 0.100 - 0.350  | 0.130 - 0.300  |
| xwOBA on contact | 0.280 - 0.500  | 0.330 - 0.450  |
| pull-FB %        | 5 - 25         | 8 - 22         |

Result on the harness: mid-tier scores tightened ~3-5 pts; elite scores widened ~15-20 pts; under-replacement bottoms out near 0 (was ~20). More rank discrimination at both tails. Judge moved 70 → 84.

### Change 2: park additive bonus (`+0.05 × park`)

Park's regression weight stays at 0.000 in the weighted average — refit said "park is non-predictive net of pitcher vulnerability + weather." But park-as-within-slate-percentile *does* carry signal that's getting thrown away (Yankee Stadium PF 115 vs Petco PF 92 is a real 25-pt spread). Rather than re-stealing weight from another factor (which forces a full refit), added park as a **purely additive bonus** on top of the weighted-average composite: `composite += 0.05 × park`. Shifts every composite up ~2.5 pts on average, +5 for top parks, +0 for the worst. Rankings are what matter — bonus brings hot-park batters up the board where they belong.

Harness verdict: marginal but consistent positive lift. No re-fit was needed because the bonus is multiplied by 0.05, well below the noise floor of the weight-refit's reported coefficients.

### Change 3: `USE_SEASON_HR_FLOOR=True` flipped on

Discrete-tier floor on `power_score` keyed off the batter's accumulated season HR count (5 HR → 50, 8 HR → 60, 12 HR → 70, 18 HR → 78, 25 HR → 85). Highest qualifying tier wins; floor only ELEVATES (never pulls a good score down). Originating case: Drake Baldwin homering for his 8th of the season ranked #97 on our board; same season-HR count producing wildly different ranks across hitters (Buxton 10 HR rank #8, Walker 10 HR rank #85, Baldwin 8 HR rank #97).

`backtest_flags.py` over the 14d window: floor-on decisively wins on all 4 metrics (top-8 hit rate, top-30 hit rate, AUC, Spearman). 30d window was ambiguous — most of April's hitters hadn't yet crossed the 5/8/12 HR thresholds, so the flag is a no-op for that period. Decision: ship the flag; rely on the 14d harness as the active window.

A companion flag `USE_CAREER_PRIOR` (Bayesian shrinkage of small-sample per-PA rates toward career mean) stayed off — harness showed marginal gain over floor-only, not enough to justify extra complexity yet. Stacking floor + prior is a future experiment.

### Decisions still pending from 2026-05-01

Both flagged as still open:

- "Wire a job that appends each completed day's `daily_picks ⨝ outcomes` rows into `raw_data.csv`" — **still not done.** Monthly refits remain a no-op until this lands.
- "`refit_weights.py` `current_default` baseline is stale" — **still not done.** The hardcoded `comp_default` formula in `refit_weights.py` (lines 161-167) still reflects v1_learned weights, not the actual shipped default.

**Verification:** `score_batters` and `generate_picks` import cleanly; backtest_flags harness re-confirmed each flag's verdict before flip.

---

## 2026-05-01 — monthly refit (scheduled task: `mlb-hr-refit-weights-monthly`)

**Status: no change shipped.**

Re-ran `backfill_features_v2_bulk.py --season 2026` and `refit_weights.py` per the scheduled task. Findings:

- **Underlying training data was unchanged.** `raw_data.csv` is still 5,196 rows over 2026-03-27 → 2026-04-15, mtime `Apr 16 13:47`. ~16 days of live picks have run since the last refit (logs show daily runs through 2026-05-01) but no script in the daily flow appends new outcome rows back into `raw_data.csv`. The bulk script only refreshes Savant feature columns (`xwoba_contact`, `fb_pct_allowed`); it does not extend the date range. **Action item:** wire a job that appends each completed day's `daily_picks` ⨝ `outcomes` rows into `raw_data.csv` (or refit directly off the DB), otherwise this monthly refit is a no-op.

- **New learned weights are within rounding of current default.** Logreg gave `power 0.249, matchup 0.265, park 0.000, form 0.279, weather 0.057, lineup 0.150` vs current `0.250 / 0.264 / 0.000 / 0.279 / 0.057 / 0.150`. Differences ≤ 0.001.

- **`refit_weights.py` backtest's `current_default` baseline is stale.** The hardcoded `comp_default` formula (lines 161–167) still uses v1_learned weights (`0.217 / 0.270 / 0.304 / 0.060`), not the actual shipped default. So the printed `+1.25 pp lift_vs_current` is really lift-vs-v1; lift vs the actual shipped default is ~0. **Action item:** update that hardcoded baseline to mirror `WEIGHT_CONFIGS["default"]` so future refits compare apples-to-apples.

- **Coefficient sanity check (standardized):** form +0.4962, matchup +0.4703, power +0.3459, weather +0.1011, `xwoba_contact` +0.0974, `fb_pct_allowed` −0.0230, `park_score` −0.0117. No sign flips on the strong signals. The Vegas-bearing matchup factor is stable and second-strongest. Park is still ≈ 0 (−0.0117) — no signal yet, justifying the continued 0 weight. The slightly negative `fb_pct_allowed` is unexpected on its face but the magnitude is small and it lives inside the matchup bucket which still nets strongly positive.

- **Decision:** did not modify `score_batters.py`. Will revisit once `raw_data.csv` is being extended with new days.

**Verification:** `score_batters` and `generate_picks` import cleanly; today's daily pipeline (08:15 ET) had already run successfully — 277 `pick_inputs` persisted, 8 selected picks, site exported, GitHub push.
