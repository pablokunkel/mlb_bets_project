# Weight refit log

A running log of monthly weight-refit decisions for `WEIGHT_CONFIGS["default"]` in `score_batters.py`. Each entry captures what was tried, what was learned, and whether anything actually shipped.

Refit driver: `refit_weights.py` (run monthly via Windows scheduled task `mlb-hr-refit-weights-monthly`). Training data feeder: `backfill_features_v2_bulk.py --season 2026` (refreshes Savant feature columns in `raw_data.csv` / `raw_data_v2.csv`). Score-curve / flag decisions are driven separately by `backtest_flags.py`, a same-data harness that compares scoring variants (anchor sets, floor on/off, prior on/off) against the canonical default on lift / AUC / top-8 / top-30 / monotonicity over a fixed window.

---

## 2026-05-25 — backtest-harness decision phase (B6 + Form anchors)

**Status: harnesses shipped, weight changes pending.** Two new backtest tools landed against the 2025-season backfill; preliminary findings on the partial sample are clean enough to pre-commit two A1-prep directions, but the weight refit itself is still gated on (a) full-backfill re-run after a data-recovery incident, and (b) a wider-real-Statcast variant.

### Tools shipped

- `diagnostics/backtest_power_inputs.py` — sweeps 6 variants of `score_power` (synthetic-only / real-only / blended / real-tight-anchors / blended-tight-anchors / synthetic-no-hr-encoded), grades AUC + top-decile lift + quintile monotonicity on `pick_inputs ⨝ outcomes`. Skepticism-probe design — tests for both anchor-calibration bias and HR-rate auto-correlation in the synthetic inputs.
- `diagnostics/backtest_form_anchors.py` — sweeps 6 variants of `score_form` (current / avg_floor_180 / no_avg / 2x_hr / hr_iso_only / hr_only) with the same grading.

### Findings on the 90-date partial sample (2025-03-27 → 2025-06-24, 18,925 rows)

**Form**: dropping `recent_avg_30g` lifts AUC 0.546 → **0.564** (+0.018), top-decile lift 1.27 → 1.42. Consistent with an earlier 148-date result (+0.017). Mechanism: AVG is mostly singles + groundballs falling in; ISO already captures the power dimension. Feast-or-famine power hitters have lower AVG by definition, so the AVG term anti-correlates with the very signal we want. Lowering the floor (0.210 → 0.180) didn't help; weighting HR more didn't help. Dropping AVG is the lever.

**Power**: synthetic season inputs beat real 14d Statcast by **~0.10 AUC** (0.652 vs 0.550). Probed for confounds:
- `synthetic-no-hr-encoded` (drops `barrel_pct` + `hr_fb_pct`, leaving SLG-encoded `exit_velo` + `iso`) AUC 0.649 — **essentially tied with synthetic-only**. So the win is NOT past-HR-rate auto-correlation. The SLG-encoded subset alone carries the signal.
- `real-tight-anchors` (barrel 10–22, xwOBA 0.32–0.42, ISO 0.13–0.32) AUC 0.548 — **anchors aren't the problem either**. Tightening anchors against the 14d distribution doesn't unlock predictive signal.
- Quintile rates make the gap visible: synthetic Q1→Q5 spread 4x (0.050 → 0.206); real-only spread 1.6x (0.093 → 0.150). The 14d window genuinely under-discriminates.

### A1 pre-commits (pending full-backfill confirmation)

1. **Drop `recent_avg_30g` from `score_form`.** Small standalone PR. Re-confirm on 188-date sample first. Tracked as B11 in BACKLOG.md.
2. **Keep `USE_RECENT_STATCAST_BLEND=False`.** Don't flip the B6 blend; it hurts AUC under both default and tight anchors.

### Outstanding before final A1

- **Wider real-Statcast window (21d / 28d).** Last untested variant. Requires a new bulk-Statcast ETL pass to populate `recent_*_21d` / `recent_*_28d` columns. ~3–4 hours of work. If 14d is just too noisy at the per-row level, a longer window may unlock the signal B6 was built on. Tracked as B12.
- **Full-backfill re-run.** Partial sample lost ~98 dates of 7/1–9/30 in a tooling incident (R2 push exit code masked by `| tail` in a `&&` chain; subsequent pull overwrote local-ahead state). Re-running now via the wrapper. Lesson committed: never pipe a command whose exit code matters; always inventory R2 explicitly before any pull that could overwrite locally-ahead state.
- **`raw_data.csv` extension is now effectively obsolete** for the refit-data-source question — `pick_inputs` now has the full 2025 season and is the right source for the next refit. Action item from 2026-05-01 closes here (see pointer there).

### Decisions still pending from earlier entries

- `refit_weights.py` `current_default` baseline still stale (2026-05-01 item) — **still not done**. The hardcoded `comp_default` formula in `refit_weights.py` (lines 161-167) still reflects v1_learned weights, not the actual shipped default. A1 refit prep should address this.

### Verification

`score_batters` and `generate_picks` import cleanly. 57/57 smoke pin tests pass (including the new `pin_backtest_power_inputs_isolates_variants`, `pin_backtest_form_anchors_variants_isolate`, `pin_weather_archive_cache_roundtrip`, `pin_weather_retry_config`, and the DB-backed archetype pins). End-to-end smoke: pre-warmed weather cache hits in 17ms; both backtest harnesses run cleanly on 90-date sample with stable findings.

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

- "Wire a job that appends each completed day's `daily_picks ⨝ outcomes` rows into `raw_data.csv`" — **see 2026-05-25 entry.** Effectively addressed by the 2025-season backfill: `pick_inputs` now carries the full season as training data, and `refit_weights.py` can be re-pointed at the DB directly (the cleaner of the two options the original action item proposed).
- "`refit_weights.py` `current_default` baseline is stale" — **still not done.** The hardcoded `comp_default` formula in `refit_weights.py` (lines 161-167) still reflects v1_learned weights, not the actual shipped default.

**Verification:** `score_batters` and `generate_picks` import cleanly; backtest_flags harness re-confirmed each flag's verdict before flip.

---

## 2026-05-01 — monthly refit (scheduled task: `mlb-hr-refit-weights-monthly`)

**Status: no change shipped.**

Re-ran `backfill_features_v2_bulk.py --season 2026` and `refit_weights.py` per the scheduled task. Findings:

- **Underlying training data was unchanged.** `raw_data.csv` is still 5,196 rows over 2026-03-27 → 2026-04-15, mtime `Apr 16 13:47`. ~16 days of live picks have run since the last refit (logs show daily runs through 2026-05-01) but no script in the daily flow appends new outcome rows back into `raw_data.csv`. The bulk script only refreshes Savant feature columns (`xwoba_contact`, `fb_pct_allowed`); it does not extend the date range. **Action item:** wire a job that appends each completed day's `daily_picks` ⨝ `outcomes` rows into `raw_data.csv` (or refit directly off the DB), otherwise this monthly refit is a no-op. **Update (2026-05-25):** effectively addressed — see the 2026-05-25 entry. The 2025-season backfill puts the full season in `pick_inputs`; `refit_weights.py` can now read directly from the DB instead of needing the CSV extension.

- **New learned weights are within rounding of current default.** Logreg gave `power 0.249, matchup 0.265, park 0.000, form 0.279, weather 0.057, lineup 0.150` vs current `0.250 / 0.264 / 0.000 / 0.279 / 0.057 / 0.150`. Differences ≤ 0.001.

- **`refit_weights.py` backtest's `current_default` baseline is stale.** The hardcoded `comp_default` formula (lines 161–167) still uses v1_learned weights (`0.217 / 0.270 / 0.304 / 0.060`), not the actual shipped default. So the printed `+1.25 pp lift_vs_current` is really lift-vs-v1; lift vs the actual shipped default is ~0. **Action item:** update that hardcoded baseline to mirror `WEIGHT_CONFIGS["default"]` so future refits compare apples-to-apples.

- **Coefficient sanity check (standardized):** form +0.4962, matchup +0.4703, power +0.3459, weather +0.1011, `xwoba_contact` +0.0974, `fb_pct_allowed` −0.0230, `park_score` −0.0117. No sign flips on the strong signals. The Vegas-bearing matchup factor is stable and second-strongest. Park is still ≈ 0 (−0.0117) — no signal yet, justifying the continued 0 weight. The slightly negative `fb_pct_allowed` is unexpected on its face but the magnitude is small and it lives inside the matchup bucket which still nets strongly positive.

- **Decision:** did not modify `score_batters.py`. Will revisit once `raw_data.csv` is being extended with new days.

**Verification:** `score_batters` and `generate_picks` import cleanly; today's daily pipeline (08:15 ET) had already run successfully — 277 `pick_inputs` persisted, 8 selected picks, site exported, GitHub push.
