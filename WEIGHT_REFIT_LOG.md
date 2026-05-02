# Weight refit log

A running log of monthly weight-refit decisions for `WEIGHT_CONFIGS["default"]` in `score_batters.py`. Each entry captures what was tried, what was learned, and whether anything actually shipped.

Refit driver: `refit_weights.py` (run monthly via Windows scheduled task `mlb-hr-refit-weights-monthly`). Training data feeder: `backfill_features_v2_bulk.py --season 2026` (refreshes Savant feature columns in `raw_data.csv` / `raw_data_v2.csv`).

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
