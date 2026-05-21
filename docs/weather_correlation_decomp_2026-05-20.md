# Weather factor — empirical correlation decomposition (2026-05-20)

Read-only analysis. Does the Weather factor (weight 0.057) add HR-prediction signal? **Short answer: no — the temperature/wind/humidity blend is noise. The only piece of "weather" that retains marginal value is the `is_dome` indicator, and that's a venue-suppression effect that Park already partially captures.**

## 1. Data summary

| | value |
|---|---|
| Source | `pick_inputs ⋈ daily_picks ⋈ outcomes` (LEFT JOIN `daily_slate`) |
| Date range | 2026-03-27 → 2026-05-19 (41 distinct dates; 5/20 has no outcomes yet) |
| Total rows | 10,018 batter-game-AB |
| HR-hit rate | 9.87% |
| `is_dome` = 1 | 1,261 rows (HR rate 8.41%) |
| `is_dome` = 0 | 3,503 rows (HR rate 11.02%) |
| `is_dome` IS NULL | 5,254 rows (all 2026-03-27 → 2026-04-15; pre-noon path didn't stamp the column) |
| Apr 17–26 blackout | confirmed empty in DB (3 stray rows on 16/27/28) |
| `daily_slate` coverage | starts 2026-04-29 — all `venue` lookups limited to that window |

Outdoor weather completeness (n=3,503): `temperature_f`/`wind_mph`/`wind_direction_deg` non-null on 3,498. `humidity_pct` non-null on only **2,489** — ~29% of outdoor games hit the fallback path. This matters for §5.

## 2. Univariate correlation — each weather sub-input vs HR-hit binary

Outdoor frame (n=3,503; `is_dome=0`). Pearson r ≡ point-biserial for binary y. 95% CI via Fisher-z. Quintile binning by sub-input value; rate-by-quintile shows the actual lift.

| Sub-input | n | Pearson r | 95% CI | Q1 HR rate | Q5 HR rate | Q5/Q1 lift | AUC | Verdict |
|---|---:|---:|---|---:|---:|---:|---:|---|
| `temperature_f` | 3498 | +0.013 | [-0.020, +0.046] | 9.9% | 12.4% | 1.25x | 0.505 | flat — non-monotonic (Q3≈Q1) |
| `wind_mph` (raw) | 3498 | -0.006 | [-0.039, +0.027] | 11.2% | 11.0% | 0.99x | 0.496 | dead |
| `wind_direction_deg` (raw) | 3498 | +0.005 | [-0.028, +0.038] | 11.1% | — | 0.90x | — | meaningless raw (cyclic) |
| `wind_alignment` (CF±45° via `score_wind`) | 3498 | +0.022 | [-0.011, +0.055] | 9.1% | 11.6% | 1.27x | 0.529 | weakest "real" signal; CI crosses 0 |
| `humidity_pct` | 2489 | +0.016 | [-0.023, +0.056] | 11.4% | 12.6% | 1.11x | 0.518 | flat; CI crosses 0 |
| `is_dome` (binary; full known frame n=4764) | 4764 | -0.038 | [-0.066, -0.010] | — | — | — | — | only sub-input with CI not crossing 0 |

Every CI on the continuous sub-inputs crosses zero. The wind-alignment score (which is the one piece score_wind was carefully built around) has r=+0.022 with CI [-0.011, +0.055] — indistinguishable from noise at n=3,500. **`is_dome` is the only sub-input whose CI excludes zero**, and the effect is negative (dome games hit fewer HRs).

Dome-vs-outdoor HR-rate gap: 8.41% vs 11.02%, +2.61pp difference, SE=0.94pp, z=2.77 (p≈0.006 two-sided). Real but moderate. See §6 for whether this is independent of Park.

## 3. Sub-input multicollinearity

Outdoor frame (n=3,498). Pearson r:

|  | temp | wind_mph | wind_dir | wind_align | humidity |
|---|---:|---:|---:|---:|---:|
| `temperature_f` | 1.000 | 0.081 | -0.105 | 0.158 | **-0.356** |
| `wind_mph` | 0.081 | 1.000 | **0.504** | **0.366** | -0.188 |
| `wind_direction_deg` | -0.105 | 0.504 | 1.000 | 0.136 | -0.067 |
| `wind_alignment` | 0.158 | 0.366 | 0.136 | 1.000 | -0.004 |
| `humidity_pct` | -0.356 | -0.188 | -0.067 | -0.004 | 1.000 |

Pairs with |r| > 0.3 (bold):
- `wind_mph` × `wind_direction_deg`: r=0.504 — moderate; same physical phenomenon (wind regimes).
- `wind_mph` × `wind_alignment`: r=0.366 — alignment is built from speed and direction, so correlation is structural.
- `temperature_f` × `humidity_pct`: r=-0.356 — cooler = more humid in springtime sample; not a model bug, just the season's weather.

Including `is_dome` (n=4,764, all is_dome-known rows): `wind_mph × is_dome r=-0.745`, `wind_direction_deg × is_dome r=-0.561`. Domes report wind ≈ 0 mph, which is correct behavior but makes the outdoor-only wind features quasi-redundant with the dome flag in a full-sample regression.

## 4. Marginal contribution conditional on the rest of the model

Define `composite_minus_weather = daily_picks.composite - 0.057 × weather_score`. Then logit HR on the full composite minus weather, plus each weather variable individually. (Median |composite_minus_weather − linsum(power..lineup)| ≈ 2.5, accounted for by the +0.05·park additive bonus and platoon dampener — not material here.)

### Full sample (n=10,018, HR rate 9.87%)

| Model | Coef (weather) | p | AIC vs base |
|---|---:|---:|---:|
| HR ~ comp_no_w *(baseline)* | — | — | 6234.8 |
| HR ~ comp_no_w + **weather_score** | +0.0048 | 0.088 | -0.9 (worse-ish) |
| HR ~ comp_no_w + **is_dome** *(is_dome-known frame n=4764)* | -0.255 | **0.027** | -3.6 |
| HR ~ comp_no_w + is_dome + weather_score *(n=4764)* | weather=+0.0048 (p=0.267); is_dome=-0.22 (p=0.061) | — | -0.8 vs is_dome-only |

### Each individual weather sub-input, conditional on composite_minus_weather (outdoor only, n=3,498)

| Sub-input | Coef | p | Beats `weather_score`? |
|---|---:|---:|---|
| `weather_score` (outdoor) | +0.0048 | 0.260 | — |
| `temperature_f` | +0.0030 | 0.610 | no |
| `wind_mph` | -0.0039 | 0.787 | no |
| `wind_alignment` | +0.0071 | 0.191 | marginal — best of the continuous set |
| `humidity_pct` | +0.0036 | 0.232 | no |

### Subset by where weather actually matters for picks

| Sample | n | Weather coef | p |
|---|---:|---:|---:|
| All rows | 10,018 | +0.0048 | 0.088 |
| Tier 1–3 (real picks) | 5,093 | +0.0011 | 0.771 |
| `selected==1` (the actual recommended picks) | 321 | -0.0051 | 0.608 |
| Tier 1 only | 1,500 | +0.0023 | 0.720 |

**Key result:** the borderline-significant p=0.088 result on the full sample is driven by `is_dome` separating top-rated batters into "outdoor-feasible" vs "dome-suppressed" buckets. Once you condition on `is_dome`, weather_score's coefficient drops to p=0.267. On the actual recommended picks, weather has zero (slightly negative) marginal value.

## 5. Slate-ctx vs fallback path divergence

Per `score_weather` (`score_batters.py:966-998`): if all three of (temp, wind_mph, humidity) are non-null AND `compute_slate_context` has the game in `weather_pct`, the score is `0.60 × slate_pct + 0.40 × wind_alignment`. Otherwise the score is `0.45 × temp_anchor + 0.35 × wind + 0.20 × humidity_anchor`.

Empirically (outdoor only):

| Path | n | HR rate | r(weather_score, HR) | mean weather_score | std |
|---|---:|---:|---:|---:|---:|
| All 3 inputs present (slate-ctx eligible) | 2,489 | 11.33% | +0.024 [-0.016, +0.063] | 59.38 | 13.68 |
| Humidity NULL (fallback path forced) | 1,009 | 10.31% | -0.006 [-0.068, +0.056] | 48.60 | 2.58 |

Two observations:
1. **Fallback path has 5× narrower variance** (std 2.58 vs 13.68). When the fallback fires, every batter gets ~50 from weather — i.e., no information at all. So 29% of outdoor rows score weather as a near-constant.
2. **On the same 2,489 slate-ctx-eligible games**, recomputing both paths' scores and comparing:
   - `(live weather_score) - (fallback formula score)`: mean = **+9.91**, std = 10.26, median |diff| = **10.72**
   - 1,876 / 2,489 rows (75%) differ by > 5 points
   - 1,358 / 2,489 rows (55%) differ by > 10 points

   The two paths produce systematically different numbers on the same game. The slate-ctx path runs ~10 points higher on average (because slate-rank percentile is centered on the slate's median, not on a fixed 50 anchor).

This is finding #10 from `scoring_audit_2026-05-20.md` quantified. The 10-point divergence is large compared to weather's contribution to composite (`0.057 × 10 = 0.57` composite points) but small compared to composite std (≈8). Operationally it means: on a mixed-weather slate, ~75% of outdoor games' weather_scores aren't on the same scale, so backtest weight-refits using these scores are fitting partially-incomparable observations.

## 6. Park × Weather interaction

Pairwise (outdoor n=3,498):

| | corr(park_score, sub-input) |
|---|---:|
| `temperature_f` | +0.119 |
| `wind_mph` | +0.039 |
| `wind_alignment` | +0.031 |
| `humidity_pct` | -0.384 |

Humidity has the strongest park overlap (r=-0.38) — Florida/Texas humid parks are also generally hitter-friendly, so park and humidity are picking up the same regional signal.

Logit HR ~ park_score + weather variable (outdoor only):

| Model | park_score coef | park p | weather var coef | weather p |
|---|---:|---:|---:|---:|
| + `weather_score` | +0.0029 | 0.155 | +0.0054 | 0.210 |
| + `temperature_f` | +0.0030 | 0.140 | +0.0035 | 0.560 |
| + `wind_mph` | +0.0032 | 0.116 | -0.0058 | 0.686 |
| + `wind_alignment` | +0.0031 | 0.130 | +0.0068 | 0.210 |
| + `humidity_pct` | +0.0042 | 0.084 | +0.0046 | 0.158 |

None of the continuous weather sub-inputs improves on park alone (p > 0.15 across the board). park_score itself is marginal (p=0.084 at best, n=2,489).

**Is_dome regression on the is_dome-known frame (n=4,764):**

| Model | Coef | p | AIC |
|---|---:|---:|---:|
| HR ~ park_score | — | 0.058 | base |
| HR ~ park_score + **is_dome** | is_dome=-0.278 | **0.016** | -4.0 |

`is_dome` retains independent signal after park_score, because the park-factor seed (`get_hardcoded_park_factors`) doesn't fully express the dome HR-suppression. (Several dome parks — T-Mobile, Tropicana, loanDepot, Globe Life — are top-5 HR-suppressors by raw observation.) Park factor refresh (B3 backlog) would likely absorb most of this.

## 7. Recommendation

**Drop the weather factor as a standalone. Reweight the rest. Bake `is_dome` into Park (or keep as a 1-coefficient indicator) if anything.**

Defense:
- **Weather_score as a composite is noise on actual picks** (§4): p=0.608 on selected==1, p=0.771 on T1-T3. The +0.0048 coefficient on full-sample (n=10,018, p=0.088) is driven entirely by the dome split, not by temperature/wind/humidity.
- **No individual weather sub-input retains marginal value** vs the rest of the model (§4): all p > 0.15 once you condition on composite_no_weather.
- **The slate-ctx vs fallback paths disagree by ~10 points on 55% of games** (§5), so the current weather_score isn't even internally consistent on the same observation. Refitting weights against this is fitting on noise with structured measurement error.
- **`is_dome` is the only sub-input with a CI that excludes zero** (§2), and it adds 4 AIC points beyond park (§6). But it's a 1-bit signal that conceptually belongs to Park. Folding `is_dome` into the park-factor calculation as a refresh (B3) is the cleaner home for it.
- Weight reallocation suggestion: redistribute the 0.057 proportionally across `power`, `matchup`, `form` (current ratio 0.250 : 0.264 : 0.279 → bump each by ~+0.019). Final: `power 0.269, matchup 0.283, park 0.000+0.05_additive (with is_dome-aware refresh), form 0.298, lineup 0.150`. Will re-validate at the next A1 refit anyway.

Alternative — **"fold + drop"**: if Pablo wants to preserve the dome handling without a full Park refactor, change `score_weather` to return `score_park` for dome games (or apply a -2pp HR-prob shift) and zero out the rest. This is 5 lines in `score_batters.py` and captures the only real signal at zero weight cost.

What I'd NOT recommend: keep weather at 0.057 and recalibrate. The continuous sub-inputs are flat-out noise this season, and the slate-ctx/fallback divergence (§5) means any recalibration will fit different measurement scales as if they were the same. B6 (Power rebuild) → A1 (refit) is a better moment to address this — and at that point the recommendation is "weather weight = 0.000".

## 8. Caveats

| Risk | Severity |
|---|---|
| **Sample size.** 386 HR events outdoor; CIs on weather sub-inputs are still wide. A weak true effect (r=0.02–0.04) wouldn't be detected at this n. But the model can't act on a true effect that small either. | medium |
| **Season-to-date is April + half of May.** Summer extremes (95°F, 80% humidity, wind 20+ mph nights) under-represented. The temperature anchor table goes to 100°F but Q5 of observed temp is only 71.9–95.7°F. Effect may be different in July–August. | medium |
| **Pre-B8 floor-less rescore issue** (audit finding #3) does NOT apply here — I worked with **production-stored** scores from `daily_picks`, not re-scored backfill. So the comparisons are apples-to-apples vs how the model actually ran each day. | none |
| **Apr 17–26 blackout** (3 stray rows only) — handled cleanly by the inner-join filter. | none |
| **`is_dome` NULL on pre-2026-04-29 rows** (5,254 rows). Those rows are in §4's "all rows" full-sample regression but excluded from §6's is_dome conditioning. The full-sample weather_score coefficient (p=0.088) leans on these; once is_dome is observable, weather_score drops to p=0.267. | low — the better-data sub-analysis is the right one to trust |
| **Park factors are stale** (B3 backlog — `get_hardcoded_park_factors` is the 2024 seed). If park HR suppression for the dome venues is mis-anchored, the §6 is_dome conditioning is on a noisy park signal. Effect: probably under-states park's signal, slightly over-states is_dome's incremental value. | low for direction, possible for magnitude |
| **Single-season backtest.** Wind/temp regressions on small samples can be deceptive — there are well-established cross-decade Statcast results showing temperature does drive HR distance ~1ft/°F. The sample just doesn't extend to extremes where that gradient matters in this dataset. | medium — the prior says temp should matter; the data says not enough to ride at 0.057 |
| **Confidence on the headline.** ~80% that weather_score's continuous sub-inputs aren't adding value over the rest of the model in 2026-to-date data. ~70% that the same conclusion holds in summer. ~90% that `is_dome` is the only piece worth keeping, and ~60% that folding it into Park is preferable to keeping it standalone. | — |
