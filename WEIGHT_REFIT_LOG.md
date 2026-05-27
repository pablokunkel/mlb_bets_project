# Weight refit log

A running log of monthly weight-refit decisions for `WEIGHT_CONFIGS["default"]` in `score_batters.py`. Each entry captures what was tried, what was learned, and whether anything actually shipped.

Refit driver: `refit_weights.py` (run monthly via Windows scheduled task `mlb-hr-refit-weights-monthly`). As of A1 (2026-05-26) refit reads directly from `pick_inputs ⨝ outcomes` in `data/hr_bets.db` — the 2025-season backfill closed the prior CSV-extension data-source gate. Score-curve / flag decisions are driven separately by `backtest_flags.py`, a same-data harness that compares scoring variants (anchor sets, floor on/off, prior on/off) against the canonical default on lift / AUC / top-8 / top-30 / monotonicity over a fixed window.

---

## 2026-05-27 — B17 power input anchor recalibration

**Status: shipped.** Five `score_power` anchors retuned against empirical `pick_inputs` distribution. No weight refit; this is an input-curve change ahead of the next A1 cycle so the refit fits weights against well-calibrated factor scores (per the audit recommendation in PR #100 section A and BACKLOG B17).

### Sample

- **2025 backfill** — `daily_picks.mode='backfill_2025'`. Used for setting anchors on 4 of 5 inputs (n=55,527 for `barrel_pct` / `iso` / `hr_fb_pct`; n=47,373 for `recent_xwoba_contact_14d`).
- **2026 live** — `pick_inputs.date >= '2026-05-03'`. Used for setting `xwoba_contact` only — the 2025 backfill is NULL for this column by design (Savant bulk endpoint returns season-final aggregates → would be look-ahead; backfill substitutes `recent_xwoba_contact_14d` via `USE_RECENT_STATCAST_BLEND=True`). n=7,711 for `xwoba_contact`. Cross-reference numbers reported for the other inputs (n=7,549–7,939).

### Method

Calibration rule: **empirical p10 → score 0, empirical p90 → score 100**. Anchors rounded to one decimal of precision toward cleaner numbers (e.g., p10=3.1 rounds to 3.0; p90=10.2 rounds to 10.0). Acceptance bounds per the brief: post-recal `p50_score` must land in `[40, 60]`, and fewer than 15% of populated rows clamp to score=0 or score=100.

Verification script: [`_review/b17_anchor_verification.py`](_review/b17_anchor_verification.py) (kept for reproducibility; embeds OLD anchors and prints summary table).

### Per-input results

Anchor-setting sample (2025 backfill where populated; 2026 live for `xwoba_contact`):

| Input | OLD anchor | NEW anchor | sample n | p10 | p50 | p90 | p50_score (NEW) | %@0 (NEW) | %@100 (NEW) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `xwoba_contact` | (0.330, 0.450) | **(0.260, 0.390)** | 7,711 (live) | 0.2570 | 0.3160 | 0.3880 | 43.1 | 11.2% | 9.5% |
| `barrel_pct` | (5, 15) | **(3.0, 11.0)** | 55,527 | 3.10 | 6.60 | 11.30 | 45.0 | 9.3% | 11.3% |
| `iso` | (0.130, 0.300) | **(0.100, 0.250)** | 55,527 | 0.1020 | 0.1670 | 0.2500 | 44.7 | 9.7% | 10.3% |
| `hr_fb_pct` | (8, 20) | **(3.0, 10.0)** | 55,527 | 2.80 | 6.00 | 10.20 | 42.9 | 11.0% | 11.0% |
| `recent_xwoba_contact_14d` | (0.330, 0.450) | **(0.225, 0.410)** | 47,373 | 0.2250 | 0.3140 | 0.4090 | 48.1 | 10.1% | 9.8% |

All five inputs pass the acceptance bounds on the anchor-setting sample. Live-sample cross-check (where populated) also passes for all four inputs that have both samples — `barrel_pct` 9.2% / 13.3%; `iso` 11.9% / 12.1%; `hr_fb_pct` 11.3% / 12.9%; `recent_xwoba_contact_14d` 10.4% / 8.9%.

OLD anchor effect for context (2025 backfill clamp at 0): `barrel_pct` 23.7%, `iso` 21.7%, `hr_fb_pct` **75.8%**, `recent_xwoba_contact_14d` 58.9%. Live xwoba_contact at OLD anchor: **60.8%** at 0. The hr_fb_pct case was the most severe — p50_score was literally 0 under (8, 20).

### Notable deviation from brief: `recent_xwoba_contact_14d`

Brief suggested anchor for the B6a-gated `recent_xwoba_contact_14d` input as "same as live xwoba_contact." Empirical p10/p90 on the 2025 backfill rows for this column are 0.2250 / 0.4090 — wider than the 2026 live `xwoba_contact` distribution (0.2570 / 0.3880). Forcing the live anchor onto the 14d distribution failed the verification (21.8% clamped at 0). Per the brief's iteration instruction ("If any input fails either bound, adjust that input's anchors and re-verify. Document the iteration."), the 14d anchor was set against the 14d empirical distribution: `(0.225, 0.410)`. Resulting acceptance: %@0=10.1%, %@100=9.8%, p50_score=48.1. Documented in `score_power`'s anchor comment.

### Smoke pins

Added 5 anchor-literal pins to `tests/smoke.py` (per the brief):

- `pin_score_power_barrel_anchors`
- `pin_score_power_hr_fb_anchors`
- `pin_score_power_iso_anchors`
- `pin_score_power_xwoba_anchors`
- `pin_score_power_recent_xwoba_anchors`

Each pin scores anchor-low, anchor-high, empirical p50, and out-of-range values, confirming the curve matches the documented anchor. All five pass. The existing `pin_score_power_elite` (Buxton-class profile) still passes — elite score rises from ~88 to 94.0 under the new (tighter) anchors; existing floor pins unchanged (the floor mechanism is independent of the anchor curve).

### What this change does NOT do

- Does NOT change `score_power`'s other inputs (`exit_velo` anchor stays (85, 95) — already well-calibrated per audit section A; `pull_fb_pct` left at (8, 22) pending B20 drop/wire decision).
- Does NOT touch the season-HR floor curve, the `USE_*` flags, or any other scoring factor.
- Does NOT modify weights — this is curve calibration, not weight refit. Next A1 refit will fit against the new curves.
- Does NOT modify `score_form` anchors, `score_matchup` v1/v2 paths, `score_park`, `score_weather`, `score_lineup_position` — those have their own backlog items (B15 lineup table, etc.).

### Files touched

- `score_batters.py::score_power` — five anchor constants + comment block update.
- `tests/smoke.py` — five new pins + helper `_check_anchor_score`.
- `WEIGHT_REFIT_LOG.md` — this entry.
- `_review/b17_anchor_verification.py` — verification script (kept; embeds OLD anchors and prints summary table).

---

## 2026-05-26 — A1 refit on the 188-date 2025 backfill (post-B11 score_form)

**Status: candidate weights presented; user to flip the switch in a follow-up.** First true post-v2 / post-B11 refit. The 2025-season backfill (PR #69) and B11 (PR #78, score_form drops recent_avg_30g) jointly unblocked A1. `refit_weights.py` was rebuilt this cycle (see "Refit tool changes" below).

### Sample

- **Source.** `pick_inputs ⨝ outcomes`, 2025-03-27 → 2025-09-30. 41,590 rows over 183 dates (the 188-date headline drops 5 dates where outcomes are missing). Overall HR rate 11.90%. `as_of_date` semantics inherited from the backfill — every row was scored using only data available before its own morning, matching the production flow.
- **Why pick_inputs, not daily_picks.** Pre-A1 `refit_weights.py` joined `daily_picks ⨝ pick_inputs ⨝ outcomes`. `daily_picks` is the model's already-selected top-N per day (≤8 selected + ~50 unselected ranked board), so refitting there biased the regression toward "what production already thinks is a HR candidate" — circular. Pulling from `pick_inputs` exposes the full ~220-row daily slate.
- **Re-scoring.** Persisted `form_score` in `daily_picks` was computed pre-B11 (still includes `recent_avg_30g`). The refit recomputes every factor row-by-row using the CURRENT `score_*` functions so B11's drop-AVG change is honored. Sanity print confirmed 38,486/41,590 (92.5%) of rows have `new_form ≠ persisted_form`, as expected.

### Method

Logistic regression `hit_hr ~ {power, matchup, park, form, weather, lineup}` on factor scores normalized to [0, 1] (raw 0-100 / 100). L2 regularization at C=1.0 (mild shrinkage; not enough to swamp signal). Two candidate variants emitted:

- **FREE.** Raw logreg coefficients, negatives clipped to 0, normalized to sum to 1.0. No structural priors.
- **PINNED.** Same logreg, then `lineup = 0.15` floor (opportunity-arithmetic anchor; lineup_score is a 9-step function whose logreg coefficient is noisy across refits) and `park = 0` pin (rationale: batters play their home park ~50% of games, so a season-aggregate park weight averages out; the +0.05*park additive bonus added 2026-05-03 handles within-slate park signal outside the weighted average).

Verdict driven by **out-of-sample** holdout: chronological 70/30 split (train 2025-03-27 → 08-05, holdout 2025-08-06 → 09-30). Refitting on the full 188 dates and grading on the same 188 dates is in-sample — the lift it shows is partly overfit. The OOS numbers are what should drive the ship decision.

### Coefficients (full-sample fit, on [0, 1] factor inputs)

```
power      +0.99
matchup    +2.10
park       -1.23   (negative — gets clipped to 0)
form       +0.14
weather    +0.44
lineup     -0.15   (negative — gets clipped to 0 under FREE; pinned 0.15 under PINNED)
intercept  -2.85
```

### Candidate weights — both variants

| factor   | current_default | FREE  | PINNED |
|----------|----------------:|------:|-------:|
| power    | 0.250           | 0.271 | 0.230  |
| matchup  | 0.264           | 0.572 | 0.486  |
| park     | 0.000           | 0.000 | 0.000  |
| form     | 0.279           | 0.038 | 0.033  |
| weather  | 0.057           | 0.119 | 0.101  |
| lineup   | 0.150           | 0.000 | 0.150  |
| **sum**  | 1.000           | 1.000 | 1.000  |

### OOS backtest (holdout: 12,382 rows, 55 dates 2025-08-06 → 09-30, HR rate 12.30%)

| Variant                      | AUC    | Top-decile rate | Top-decile lift pp | Avg HR rank | Top-8 hit rate | Quintile mono |
|------------------------------|-------:|----------------:|-------------------:|------------:|---------------:|--------------:|
| persisted (live, pre-B11)    | 0.5965 | 0.1955          | +7.25 pp           | 99.7        | 83.64%         | 4/4 strict    |
| current_default (re-scored)  | 0.6085 | 0.1971          | +7.41 pp           | 97.2        | 81.82%         | 4/4 strict    |
| **candidate FREE**           | 0.6232 | 0.2246          | **+10.16 pp**      | 94.3        | **90.91%**     | 4/4 strict    |
| candidate PINNED             | 0.6143 | 0.2019          | +7.89 pp           | 96.3        | 80.00%         | 4/4 strict    |

Deltas vs `current_default` on OOS:

- **FREE.** Top-decile lift +2.75 pp (threshold > +1.00 pp). AUC +0.0148 (must be > -0.005). **SHIP-eligible.** Top-8 hit rate +9.1 pp.
- **PINNED.** Top-decile lift +0.48 pp (below threshold). AUC +0.0059. **HOLD per the rule.**

Sensitivity check: 80/20 and 60/40 chronological splits both rate FREE as SHIP (+2.55 pp / +3.06 pp) and PINNED as HOLD (+0.97 pp / +0.54 pp). Robust to split choice.

### Why each weight moved (FREE variant)

- **Matchup 0.264 → 0.572.** Strongest standalone factor (Pearson r=0.121, top-quintile lift 1.53x). The regression assigns it the largest coefficient (+2.10) because matchup_score already absorbs four signals — pitcher vulnerability (slate-percentile), woba_vs_hand, Vegas team total (the v2 Vegas signal that landed 2026-04-29), and platoon/rookie bonuses. That's why doubling matchup's weight doesn't double-count the same input multiple times: matchup IS the multi-input bucket.
- **Power 0.250 → 0.271.** Small bump. Power's Pearson r=0.115 is close to matchup's; the coefficient (+0.99) is roughly half matchup's. Synthetic-input power (barrel%/exit_velo/HR_FB% derived from season SLG-encoded inputs) was already strong (see 2026-05-25 backtest_power_inputs results) — the refit didn't unlock new signal here, just re-weighted slightly upward.
- **Form 0.279 → 0.038.** This is the biggest swing and the one to scrutinize. **Multicollinearity drives it.** On the re-scored data the factor-to-factor correlations are: power×form r=0.49, matchup×form r=0.30, power×matchup r=0.34. Power and matchup together already absorb most of the "good hitter quality" signal that form contributed before B6+B11; once they're in the model, the marginal info form adds drops near zero. The 2026-05-25 standalone form backtest still showed form's own AUC 0.564 — that's not invalidated; it just means form's *unique* contribution above power+matchup is tiny. Form's Pearson r=0.080 stays the same; what changed is the joint regression's accounting.
- **Park 0.000 → 0.000.** Consistent with every refit since v1. Park's logreg coefficient is -1.23 (negative — clipped to 0). The score_park rescore returns 50.0 for all rows in the harness fallback (no slate_ctx, empty park_factors), so this number is structurally about score_park's variance rather than predictive value — but the prior reasoning (the +0.05*park additive bonus handles real park signal outside the weighted average) still holds. Park stays at 0.
- **Weather 0.057 → 0.119.** Doubled. Weather's Pearson r is small (0.023) but positive; coefficient (+0.44) is positive in the joint fit. This is plausible — temperature + wind alignment carries some real HR signal that nothing else captures. But +6.2 pp on a factor with r=0.023 is partly a "you have to put weight somewhere" effect of the form drop.
- **Lineup 0.150 → 0.000.** Lineup_score has *negative* Pearson r with HR (-0.020) on this sample. Mechanism: top-of-order hitters (#1/#2) are typically contact-oriented (high lineup_score, low HR-per-PA); power hitters cluster in #3/#4/#5 (mid lineup_score) and HR more. The opportunity-arithmetic rationale for the carve-out (more PAs at the top) is real but the contact-vs-power composition effect outweighs it on a per-batter level. PINNED preserves the carve-out at 0.15 anyway; OOS shows PINNED fails the +1.0 pp threshold, which is the empirical evidence that the carve-out is net-hurting.

### Skepticism / artifacts the user should consider before flipping

1. **Form 0.279 → 0.038 is a wild swing.** The user prompt explicitly flagged this kind of redistribution as "likely an artifact of the small per-batter HR sample." It IS large. The defense is OOS validation: the FREE candidate still beats current_default on 55 untouched dates. But the candidate places ~93% of its weight on power + matchup. If matchup is mis-specified for any season-on-season reason (rule changes, Vegas line drift, etc.), the candidate has less ballast.
2. **Multicollinearity inflates matchup's apparent contribution.** Coefficients in a correlated-input regression are not stable across slightly different training sets; refit again in 6 weeks and the matchup/power split could shift by ±0.05 to ±0.10. The PINNED variant is more robust to that drift (15% lineup keeps the composite from rotating entirely on power+matchup) but pays for that robustness with worse OOS lift.
3. **The 188-date sample is one season.** 2025 had specific rule + ball + roster characteristics. 2024 weights may have looked different; 2026 weights may look different again. The +2.75 pp OOS lift is genuine on THIS data, not necessarily a permanent improvement.
4. **Pre-B11 vs post-B11 form_score on the SAME training rows.** Re-scoring every row with the post-B11 score_form is the correct apples-to-apples comparison for the refit. But the live production data over the next 6 weeks will be POST-B11 scored — so the re-scored numbers here ARE the relevant baseline for the candidate.

### Decision

**Recommendation: present both variants; defer the final flip to the user.** Per the A1 plan in the original instruction, this PR is the analysis + recommendation, not the actual weight change. The FREE candidate ships under the +1.0 pp / -0.005 AUC rule; PINNED holds. User to choose:

- **Ship FREE** if comfortable with form ≈ 0 and lineup ≈ 0 (the OOS lift is empirically supported; multicollinearity concerns are real but the data is on-side).
- **Ship PINNED** if structural priors (lineup carve-out, form-floor) outweigh the marginal lift (PINNED missed the threshold by 0.52 pp on 70/30 OOS — not by much; could ship as a "less risky" variant if appetite is low).
- **Hold** if the form/lineup swings feel uncomfortable enough that another 2-4 weeks of post-B11 live data is preferred before committing.

**This PR does NOT modify `score_batters.py`.** The candidate lines are in `refit_weights.py --update` output; flipping the switch is a one-line follow-up.

### Refit tool changes (this cycle)

- `refit_weights.py` rebuilt:
  - Reads `pick_inputs ⨝ outcomes` (full slate) instead of `daily_picks ⨝ pick_inputs ⨝ outcomes` (selected picks only — was biased / circular).
  - Re-scores every row with current `score_*` functions so B11 (and any future score-curve changes) are reflected without re-running production.
  - Chronological train/test split (`--holdout-frac`, default 0.3) prevents in-sample overfitting from driving the ship-or-hold call.
  - Emits two variants per fit (FREE / PINNED with lineup-carve-out + park-pin).
  - `current_default` baseline is now imported live from `WEIGHT_CONFIGS["default"]` (was a stale hardcoded v1_learned line; this finally closes the 2026-05-01 / 2026-05-13 action item).
- Decision rule embedded: ship IF (OOS top-decile lift improvement > +1.0 pp) AND (OOS AUC regression ≤ 0.005).

### Verification

`score_batters` and `generate_picks` import cleanly (no changes to either). Sanity check inside `refit_weights.py`: 38,486/41,590 rows differ between `new_form` (post-B11) and `persisted_form` (pre-B11), confirming the rescore path actually exercises score_form's new code.

---

## 2026-05-25 — backtest-harness decision phase (B6 + Form anchors)

> See 2026-05-26 entry above. The B11 score_form change (drop recent_avg_30g) that this entry pre-committed has now shipped (PR #78); the A1 refit that this entry was gating has been run.

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

## 2026-05-13 — 14d refit (scheduled task: `mlb-hr-refit-weights-14d`)

> See 2026-05-26 entry above. The "still-open action items" called out here (CSV extension, `pull_fb_pct` bulk crawl, stale `current_default` baseline) are all resolved by the A1 rebuild: refit now reads `pick_inputs` directly from the DB, and `SHIPPED_DEFAULT_W` is imported live from `WEIGHT_CONFIGS["default"]`. The 2026-05-13 coefficients themselves (within ±0.001 of then-current default) reflect a stale, no-new-data window — superseded by the 188-date 2025 backfill.

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

1. **Append daily outcomes into `raw_data_v2.csv` (or mount `Projects/data/` in the refit sandbox).** Without this, every monthly/14-day refit will hit the same 2026-03-27 → 2026-04-15 window and produce the same coefficients to within rounding. The Vegas signal will stay untested for the same reason. **Update (2026-05-25):** effectively addressed — see the 2026-05-25 entry. The 2025-season backfill puts the full season in `pick_inputs`; `refit_weights.py` can now read directly from the DB.
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

- "Wire a job that appends each completed day's `daily_picks ⨝ outcomes` rows into `raw_data.csv`" — **see 2026-05-25 entry.** Effectively addressed by the 2025-season backfill: `pick_inputs` now carries the full season as training data, and `refit_weights.py` can be re-pointed at the DB directly (the cleaner of the two options the original action item proposed).
- "`refit_weights.py` `current_default` baseline is stale" — **still not done.** The hardcoded `comp_default` formula in `refit_weights.py` (lines 161-167) still reflects v1_learned weights, not the actual shipped default.

**Verification:** `score_batters` and `generate_picks` import cleanly; backtest_flags harness re-confirmed each flag's verdict before flip.

---

## 2026-05-01 — monthly refit (scheduled task: `mlb-hr-refit-weights-monthly`)

> See 2026-05-26 entry above. Both action items called out here (CSV-extension job and `current_default` baseline staleness) are resolved by the A1 rebuild — refit reads `pick_inputs` directly from the DB, and `SHIPPED_DEFAULT_W` is now imported live from `WEIGHT_CONFIGS["default"]`. The 2026-05-01 coefficients themselves (effectively unchanged from current default on a stale 20-day window) are superseded by the 188-date 2025 backfill.

**Status: no change shipped.**

Re-ran `backfill_features_v2_bulk.py --season 2026` and `refit_weights.py` per the scheduled task. Findings:

- **Underlying training data was unchanged.** `raw_data.csv` is still 5,196 rows over 2026-03-27 → 2026-04-15, mtime `Apr 16 13:47`. ~16 days of live picks have run since the last refit (logs show daily runs through 2026-05-01) but no script in the daily flow appends new outcome rows back into `raw_data.csv`. The bulk script only refreshes Savant feature columns (`xwoba_contact`, `fb_pct_allowed`); it does not extend the date range. **Action item:** wire a job that appends each completed day's `daily_picks` ⨝ `outcomes` rows into `raw_data.csv` (or refit directly off the DB), otherwise this monthly refit is a no-op. **Update (2026-05-25):** effectively addressed — see the 2026-05-25 entry. The 2025-season backfill puts the full season in `pick_inputs`; `refit_weights.py` can now read directly from the DB instead of needing the CSV extension.

- **New learned weights are within rounding of current default.** Logreg gave `power 0.249, matchup 0.265, park 0.000, form 0.279, weather 0.057, lineup 0.150` vs current `0.250 / 0.264 / 0.000 / 0.279 / 0.057 / 0.150`. Differences ≤ 0.001.

- **`refit_weights.py` backtest's `current_default` baseline is stale.** The hardcoded `comp_default` formula (lines 161–167) still uses v1_learned weights (`0.217 / 0.270 / 0.304 / 0.060`), not the actual shipped default. So the printed `+1.25 pp lift_vs_current` is really lift-vs-v1; lift vs the actual shipped default is ~0. **Action item:** update that hardcoded baseline to mirror `WEIGHT_CONFIGS["default"]` so future refits compare apples-to-apples.

- **Coefficient sanity check (standardized):** form +0.4962, matchup +0.4703, power +0.3459, weather +0.1011, `xwoba_contact` +0.0974, `fb_pct_allowed` −0.0230, `park_score` −0.0117. No sign flips on the strong signals. The Vegas-bearing matchup factor is stable and second-strongest. Park is still ≈ 0 (−0.0117) — no signal yet, justifying the continued 0 weight. The slightly negative `fb_pct_allowed` is unexpected on its face but the magnitude is small and it lives inside the matchup bucket which still nets strongly positive.

- **Decision:** did not modify `score_batters.py`. Will revisit once `raw_data.csv` is being extended with new days.

**Verification:** `score_batters` and `generate_picks` import cleanly; today's daily pipeline (08:15 ET) had already run successfully — 277 `pick_inputs` persisted, 8 selected picks, site exported, GitHub push.
