# Inputs / anchors / paths audit — 2026-05-26

Second deeper-pass audit, per the user's PM brief. The previous audit (PR
#97) found ~6 HIGH but only ~2 net-new. This pass goes deeper into areas
the prior audit sampled rather than exhausted. Every HIGH carries an
empirical citation.

**Data sample:** `pick_inputs` (69,911 rows, 239 dates, 2025-03-27 →
2026-05-26). Two primary windows used:
- **2025 backfill** — `daily_picks.mode='backfill_2025'`, 55,527 rows.
  Largest clean sample; used wherever production semantics are also active.
- **2026 live** — `pick_inputs.date >= '2026-05-03'`, 8,135 rows. Smaller
  but exercises the production scoring path with the current code.

## Counts

- **HIGH (net-new): 7**
- **HIGH (restated from known): 1** (hr_fb_pct anchor — per handoff doc)
- **MEDIUM: 7**
- **LOW: 1**

## Most actionable

1. **`pull_fb_pct` is 100% NULL across all 69,911 rows of `pick_inputs`.**
   `score_power` reads it (one of six documented Power inputs); the
   producer at [generate_picks.py:1627-1628](generate_picks.py:1627) is
   dead code because the slate-level `batter_adv` only contains
   `xwoba_contact`. The doc-listed "6-input Power score" is empirically a
   5-input score, and the dashboard's input_calibration card has been
   showing a flat null row for months. Small surgical fix: drop the input
   from `score_power` and `compute_composite.inputs_snapshot`, OR wire a
   `bulk_pull_fb_pct` fetch into `fetch_live_slate`. **Est: 30-60 min**
   (drop) or **2-4h** (re-wire).

2. **`xwoba_contact` anchor `(0.330, 0.450)` clamps 60.8% of live 2026
   measurements to score=0.** Empirical p10/p25 of measured xwoba is
   0.270 / 0.284 — below the anchor low. Only 9.5% of populated live
   rows clear neutral on this input. Anchor was set assuming
   league-average ~0.380 (per the score_power docstring), but the
   `pick_inputs` population is a broader board (T1-T4 + bench) so the
   distribution sits much lower. Recommended re-tune to ~`(0.270, 0.420)`
   — re-tune analysis can crib from `diagnostics/backtest_power_inputs.py`.
   **Est: 1-2h** (anchors + smoke verify; refit happens later).

3. **`backtest_factors.rescore_row` scores 3 of 6 factors with formulas
   that differ from production.** Park always returns 50, weather uses a
   different blend, matchup takes the v1 fallback. Comment at
   [backtest_factors.py:218](backtest_factors.py:218) shows this is
   intentional ("slate-relative path = off") but it means the A1 refit
   trains on scores that don't match what production composites. **Est:
   3-5h** to thread a stored or reconstructed slate_ctx through
   load_history. Pairs naturally with B5/B12/Phase-2 flag flips.

## A. Anchor vs empirical distribution

Method: for each fixed-anchor scoring input, pull non-NULL values from
`pick_inputs`, compute empirical p10/p25/p50/p75/p90, score each value
with the actual `score_*` function's scaler, and report % clamped at 0
or 100. Flag HIGH per the spec rule (>25% clamp OR p50_score outside
[30, 70]). Source: `_review/audit_inputs_run.py` (kept for reproducibility).

### Power factor inputs

| Input | Anchor | Sample | p10 / p25 / p50 / p75 / p90 | %@0 | %@100 | p50_score | Verdict |
|---|---|---|---|---|---|---|---|
| `barrel_pct` | (5, 15) | 2025 BF (n=55,068) | 3.10 / 5.20 / **6.60** / 8.80 / 11.30 | 23.4 | 2.5 | **16** | **HIGH** (p50<30) |
| `barrel_pct` | (5, 15) | 2026 live (n=7,549) | — / 4.80 / **6.60** / 9.10 / — | **28.1** | 4.8 | **16** | **HIGH** (clamp+p50) |
| `exit_velo` | (85, 95) | 2025 BF (n=55,068) | 87.0 / 87.6 / **88.2** / 89.0 / 89.7 | 0.6 | 0.1 | 32 | OK |
| `hr_fb_pct` | (8, 20) | 2025 BF (n=55,068) | 2.80 / 4.60 / **6.00** / 7.90 / 10.20 | **75.1** | 0.9 | **0** | **HIGH** (restated; known issue per handoff doc) |
| `hr_fb_pct` | (8, 20) | 2026 live (n=7,549) | — / 4.40 / **5.90** / 8.20 / — | **74.0** | 0.7 | **0** | HIGH (restated) |
| `iso` | (0.130, 0.300) | 2025 BF (n=55,068) | 0.102 / 0.135 / **0.167** / 0.205 / 0.250 | 21.5 | 3.2 | **22** | **HIGH** (p50<30) |
| `iso` | (0.130, 0.300) | 2026 live (n=7,939) | — / 0.130 / **0.163** / 0.204 / — | **25.3** | 5.1 | **19** | **HIGH** (clamp+p50) |
| `xwoba_contact` | (0.330, 0.450) | 2025 BF | (NULL — by design) | — | — | — | (see note) |
| `xwoba_contact` | (0.330, 0.450) | 2026 live (n=7,711) | — / 0.284 / **0.316** / 0.352 / — | **60.8** | 1.6 | **0** | **HIGH** (severe — net-new) |
| `pull_fb_pct` | (8, 22) | all rows (n=69,911) | (100% NULL) | — | — | — | **HIGH — dead input (net-new)** |
| `recent_xwoba_contact_14d` | (0.330, 0.450) | 2025 BF (n=47,198) — *fed score_power with B6a flag on* | — / 0.268 / **0.314** / 0.362 / — | **58.9** | 4.0 | **0** | **HIGH** (anchor mismatch on backfill scoring — net-new) |
| `recent_barrel_real_14d` | (8, 18) | 2025 BF (n=39,224) | — / 6.25 / **10.00** / 14.71 / — | 37.9 | 14.8 | 20 | MEDIUM (long upper tail) |
| `recent_iso_14d` | (0.100, 0.300) | 2025 BF (n=43,530) | — / 0.097 / **0.163** / 0.250 / — | 26.8 | 15.1 | 31 | MEDIUM (bimodal-ish) |

xwoba_contact note: `bulk_batter_xwoba` is deliberately skipped for
backfill mode ([generate_picks.py:1140](generate_picks.py:1140)) because
the Savant leaderboard returns season-final aggregates — would be
look-ahead bias. Backfill substitutes `recent_xwoba_contact_14d` via
`USE_RECENT_STATCAST_BLEND=True` in
[etl/backfill_2025.py:387-391](etl/backfill_2025.py:387). The
substitution itself is sound; the anchor on the substitute has the same
calibration problem as the original (see row above).

### Form factor inputs

| Input | Anchor | Sample | p10 / p25 / p50 / p75 / p90 | %@0 | %@100 | p50_score | Verdict |
|---|---|---|---|---|---|---|---|
| `recent_hr_10g` | (0, 5) | 2025 BF (n=54,509) | 0 / 0 / **1** / 2 / 3 | **36.5** | 1.6 | **20** | **MEDIUM** (lumpy by design — 0 IS data) |
| `recent_iso_30g` | (0.100, 0.300) | 2025 BF (n=52,140) | 0.071 / 0.109 / **0.157** / 0.214 / 0.279 | 21.2 | 7.5 | 28.5 | **MEDIUM** (p50 near boundary) |
| `recent_iso_30g` | (0.100, 0.300) | 2026 live (n=2,413) | — / 0.107 / **0.149** / 0.203 / — | 21.4 | 5.0 | **24.5** | **HIGH** (p50<30 on live) |

### Matchup / Weather

| Input | Anchor | Sample | p50 | p50_score | Verdict |
|---|---|---|---|---|---|
| `woba_vs_hand` | (0.290, 0.395) | 2025 BF (n=55,068) | 0.346 | 53 | OK |
| `woba_vs_hand` | (0.280, 0.420) (doc-stated, NOT what code uses) | 2025 BF | 0.346 | 47 | Documentation drift — `How_The_HR_Model_Works.md` Factor 1 table lists (0.330, 0.450), Factor 2 narrative says (0.280, 0.420). Code at [score_batters.py:870](score_batters.py:870) uses (0.290, 0.395). LOW (doc-only, code is OK). |
| `temperature_f` | piecewise | 2025 BF outdoor (n=42,111) | 72.7°F | 53 | OK |
| `humidity_pct` | linear 35+0.30h | 2025 BF outdoor (n=38,756) | 58 | 52 | OK (theoretical max-min = 35-65, never clamps) |

Bin breakdown for temperature confirms the curve is well-calibrated:
80.6% of outdoor games fall in 60-95°F (the middle of the curve where the
slope discriminates). Tails (<40°F = 0.5%, >95°F = 0.9%) are rare and
land near the anchors as intended.

### Quantified impact (% of populated rows scoring ≥ 50 — "above neutral")

Source: same script. Captures the asymmetric-distribution effect of the
anchor mismatches in cumulative terms.

| Input | 2025 BF | 2026 live |
|---|---|---|
| `barrel_pct` | 16.7% | 19.0% |
| `hr_fb_pct` | 1.8% | 4.0% |
| `iso` | 20.8% | 20.2% |
| `xwoba_contact` | (NULL) | **9.5%** |
| `recent_xwoba_contact_14d` | 14.8% | (not used in live) |
| `recent_hr_10g` | 20.8% | (skip-on-missing) |
| `recent_iso_30g` | 30.5% | (small sample) |
| `woba_vs_hand` | **52.7%** | **52.8%** |

`woba_vs_hand` is the only Power/Matchup input with a roughly-balanced
distribution under its anchor. The five power-side inputs all score the
top 1.8-21% of the population above 50 — Power's mean of those five (plus
HR-floor) is structurally bottom-heavy. The composite then weights Power
at 0.250 of a (likewise compressed) distribution. The aggregate effect
is that real signal lives in the top decile of inputs while the bulk of
the population is collapsed into a narrow low band — by design at the
extremes, but tighter than intended at the middle.

## B. Calculation-path defects

### B1. HIGH — `pull_fb_pct` is read by score_power but never set on the batter dict

Empirical: 0 of 69,911 `pick_inputs` rows have non-NULL `pull_fb_pct`.

Trace:
- **Reader:** `score_power` reads `batter.get("pull_fb_pct")` at
  [score_batters.py:697-701](score_batters.py:697). If None, the input
  is skipped from the mean (no default, no neutral fallback — the
  comment in `generate_picks.py:1524` is wrong about "defaults to 50/
  neutral in score_power").
- **Writer (tier path):** [generate_picks.py:1627-1628](generate_picks.py:1627)
  reads `adv.get("pull_fb_pct")`. But `adv` comes from `batter_adv`,
  which is constructed at line 1520 as
  `{pid: {"xwoba_contact": v} for pid, v in bulk_xwoba.items()}` — only
  `xwoba_contact` is in the dict, so `adv.get("pull_fb_pct")` is always
  None. The `if adv.get("pull_fb_pct") is not None: entry["pull_fb_pct"] = ...`
  branch is dead code.
- **Writer (T4 untiered):** Same dead branch — [generate_picks.py:1869-1872](generate_picks.py:1869).
- **Writer (offline sim):** `simulate_slate` uses hardcoded batter data
  from `mlb_2025_tiers.py` which doesn't carry `pull_fb_pct` either.

Consequence: The documented 6-input `score_power` is in practice 5
inputs. `How_The_HR_Model_Works.md` lines 81-92 lists pull-FB% as one
of six Power inputs with anchor (8, 22). Backtest_factors hits the same
empty path, so refits don't measure it either.

Proposed fix pointer: either drop pull_fb_pct from `score_power`,
`compute_composite.inputs_snapshot`, `pick_inputs` schema (or just from
the writers), and the docs — OR add a bulk pull_fb_pct fetcher
analogous to `fetch_batter_xwoba_bulk`. Drop is the smaller PR and what
the prior comment hints at ("Savant has no bulk endpoint for it").

### B2. HIGH — backtest_factors.rescore_row uses different formulas than production for 3 of 6 factors

[backtest_factors.py:218](backtest_factors.py:218) intentionally passes
`pf_df = pd.DataFrame()` and `slate_ctx=None`. The score_* functions
therefore take their fixed-anchor / v1 fallback paths, which differ
from what production runs daily:

- **score_park**: with empty `park_factors` and no `slate_ctx`, the
  fallback at [score_batters.py:1009](score_batters.py:1009) sets
  `pf=100.0` and returns `min_max_scale(100, 70, 130) = 50.0`. Every
  pick_inputs row scores park=50 in backtest. The hr_park_factor
  column is READ from pick_inputs and pulled into the dataframe, but
  never threaded into score_park. Empirical: factor_quintile_table for
  `new_park` would always be a single-value distribution.
- **score_weather**: with no `slate_ctx`, the fallback at
  [score_batters.py:1379-1385](score_batters.py:1379) returns
  `temp_score * 0.45 + wind_score * 0.35 + humidity_score * 0.20`.
  Production uses `base_pct * 0.60 + wind * 0.40` (slate percentile
  base × per-batter wind alignment, [score_batters.py:1376](score_batters.py:1376)).
  Different formula, different inputs.
- **score_matchup**: with no slate_ctx and no batter_team, takes the v1
  fallback ([score_batters.py:845-851](score_batters.py:845)) — uses
  `min_max_scale(hr_per_9, 0, 4.5) + min_max_scale(hh_pct, 25, 50)
  + woba_vs_hand + platoon_bonus + rookie_bonus`. Production uses
  `slate_ctx["pitcher_pct"][pname] + woba + team_total_pct`
  ([score_batters.py:834-835](score_batters.py:834)). Different
  formula, different inputs.

Consequence: any weight refit (A1) trained on rescored backtest_factors
data fits coefficients to scores that aren't what production produces.
The handoff doc mentions the related B12/Phase-2 column gaps as
intentional, but this 3-factor formula divergence is a deeper structural
issue — refits learn the wrong shape of each factor's signal.

Mitigation: persist slate_ctx into pick_inputs at write time
(team_total_pct already is — could add park_pct, weather_pct,
pitcher_pct per-batter), or rebuild slate_ctx from the rescore row group
inside load_history. Either way, the v1 fallback shouldn't be the
training surface for production v2 weights.

### B3. MEDIUM — `bats` column hardcoded "R" in backtest despite pick_inputs storing it

[backtest_factors.py:156](backtest_factors.py:156) sets `"bats": "R"`
with the comment "not stored; platoon advantage flag captures the diff."
But `pick_inputs.bats` HAS been stored since 2026-05-03 (etl/db.py
migration line 817). load_history SQL at line 90-107 just doesn't
SELECT it.

Consequence: in backtest rescoring, every batter is RHB. Affects:
- `score_park`'s L/R adjustment ([score_batters.py:1013-1024](score_batters.py:1013))
  — backtest uses RHB park factor for every batter regardless of real
  handedness.
- `score_matchup` v1 platoon bonus ([score_batters.py:895-897](score_batters.py:895))
  — backtest treats every batter as same-handed as the (R-defaulted)
  pitcher → no platoon bonus discrimination.

Proposed fix pointer: add `pi.bats, pi.throws` to the SELECT list at
[backtest_factors.py:106](backtest_factors.py:106), then read them in
rescore_row.

### B4. MEDIUM — T4 untiered path duplicates the same xwoba/pull-FB gaps

Verified by reading [generate_picks.py:1869-1872](generate_picks.py:1869):
T4 builds `batter_adv` the same way the tiered path does — only
xwoba_contact, no pull_fb_pct. Same dead branch at lines 1918-1922
checking pull_fb_pct. Same xwoba_contact NULL-on-backfill behavior.

Implication: B1/B2 fixes need to cover both paths. The handoff doc's
"three paths (live-tiered, T4 untiered, offline sim)" warning applies
here.

## C. DB value-range hygiene

### C1. MEDIUM (net-new) — 8,521 outcomes (2026+) have no matching daily_lineup row; 891 of those rows are HRs

```
SELECT COUNT(*), SUM(hr_count)
FROM outcomes o WHERE o.date >= '2026-04-01'
  AND NOT EXISTS (SELECT 1 FROM daily_lineup dl
                  WHERE dl.date=o.date AND dl.player_id=o.batter_id);
-- 8,521 rows, 891 HRs
```

Most of these are normal substitutions: per-game-average outcomes is
~20.3 batters vs daily_lineup ~18.0 (2 starters × 9 spots), so ~2-5
extras per game per day is the pinch-hitter/late-sub baseline. Across
~15 games × 53 days that's ~3,000-4,000 extras "expected." The remainder
(~4,000-5,000) is harder to explain at a glance.

Specific case: Yordan Alvarez (id 670541), regular Astros DH, hit a HR
in game_pk 822899 on 2026-05-25 with 3 ABs — but he's not in the Astros
daily_lineup for that game. Astros side has 9 batters in the lineup
(Matthews/Peña/Walker/Paredes/Meyers/Smith/Dezenzo/Vázquez/Allen). The
model couldn't have picked him because he wasn't in the eligible pool.

Two hypotheses to investigate (out-of-scope for this audit, surface only):
1. MLB API posted-lineup update happened after our noon fetch (player
   substituted into the lineup pre-game but after our cutoff).
2. Lineup parser miss when a lineup has duplicates or unusual position
   labels (DH-only games, position-player on the mound, etc.).

Per CLAUDE.md hard rule #6: surface only, do not assume root cause.

### C2. MEDIUM (net-new) — 67 pitcher_arsenals rows with avg_fb_velo < 80 mph

```
SELECT COUNT(*) FROM pitcher_arsenals WHERE avg_fb_velo < 80 AND avg_fb_velo > 0;
-- 67
```

All have `pitcher_name=NULL`, `source='statcast'`, velo range 57.9-79.x.
These are position players who pitched in blowouts (knuckleball, eephus,
batting-practice fastball). Because `pitcher_name=NULL`, the live-slate
lookup-by-name in `_splits_to_batters` won't match — but if a victim
profile build OR a stats query joins on `pitcher_id`, these rows could
pollute the distribution. The lower bound 57.9 mph fastball would map to
the absolute floor of any anchor that doesn't clip.

Recommend: either filter out pitcher_name IS NULL rows at fetch time in
[etl/etl_nightly.py](etl/etl_nightly.py) (these aren't real arsenals), or
add a clamp `WHERE avg_fb_velo >= 80` to the consumer queries.

### C3. MEDIUM (restated) — pitcher_fb_pct_allowed > 100

23 rows in pick_inputs, max 102.5. Known Savant parse bug per CLAUDE.md
false-alarms list. Restated for completeness — no new evidence beyond
what's already filed.

### C4. MEDIUM (restated) — season_batting.team='???' for 20 2026 rows

Known per CLAUDE.md C2 (Athletics relocated). Restated.

### C5. LOW — 4 rows with `selected=0 AND composite > 80`

All 4 explained by the per-game cap rule:
- 2025-07-13: Aaron Judge (NYY, comp=82.6) excluded from game 777122
  because Busch (CHC) and Bellinger (NYY) — both higher comp — already
  filled the 2-per-game cap.
- 2025-07-11 Busch, 2025-07-20 Suárez (bench), 2025-07-25 Judge: same
  pattern (cap or bench).

Working as intended. NOT a finding.

## D. As-of-date / look-ahead leaks

### D1. HIGH (net-new) — `_fetch_season_batting_splits(start, end_str)` uses MLB API endDate=inclusive in 2025 backfill

[fetch_daily_data.py:778](fetch_daily_data.py:778):
```python
cur_splits = _fetch_season_batting_splits(cur_start_str, date_str)
```

[fetch_daily_data.py:601-614](fetch_daily_data.py:601):
```python
url = f"{MLB_STATS_API}/stats"
params = {
    "stats": "byDateRange",
    "startDate": start_str,
    "endDate": end_str,           # MLB API treats this as INCLUSIVE
    ...
}
```

For backfill of date D, this aggregate includes games played on D —
look-ahead bias.

- The backfill driver `etl/backfill_2025.backfill_one_date` passes
  `as_of_date=date_str` ([etl/backfill_2025.py:259-261](etl/backfill_2025.py:259))
  to `generate_card`, which propagates as_of_date through `fetch_form_data_batch`
  ([generate_picks.py:1510](generate_picks.py:1510)) and victim profiles
  ([fetch_daily_data.py:1326](fetch_daily_data.py:1326) for the
  game-log filter).
- But `build_live_tiers(date_str, ...)` does NOT accept an `as_of_date`
  arg ([fetch_daily_data.py:728-739](fetch_daily_data.py:728)) and the
  internal `_fetch_season_batting_splits` call uses the slate date
  inclusive.

Affected scoring inputs:
- `barrel_pct`, `exit_velo`, `hr_fb_pct`, `iso` come from
  `_splits_to_batters` which derives them from the byDateRange aggregate
  (synthetic estimates per `hr_per_pa * 200` etc.). All four leak.
- `hr_per_pa` itself, which drives tier qualification — leak.
- `season_batting` table is populated by `etl_nightly.sync_season_batting`
  which uses the same endpoint — possibly the same leak in different
  context (production-side, not backfill — verify in a follow-up).

For 188 backfill dates, the leak's magnitude per row is small (one day
of HRs at the end of a season aggregate is typically <1% of the cum
total), but for batters who hit their first HR ON date D it can flip
tier qualification. Refits trained on this backfill have a small
systematic bias in tier assignments.

Fix path: thread `as_of_date` through `build_live_tiers` and pass
`endDate = (date_str - 1 day)` when in backfill mode (or always — at
noon production no games have started yet, so strict-less-than is
free). Then propagate to `prior_splits` (already safe) and
`_splits_to_batters`.

### D2. MEDIUM — Same call pattern at live noon is currently safe but fragile

At noon ET, no games on `date_str` have started, so MLB API byDateRange
endDate=date_str returns aggregate through D-1 effectively. A late
noon run (say 1:00 PM ET after a 12:35 PM ET first pitch) would silently
leak. Same fix as D1 makes the safety explicit.

### D3. — Other paths confirmed honest

Verified the strict-less-than pattern (`date < ?` or `game_date < ?`)
on:
- [generate_picks.py:199-242](generate_picks.py:199) — `load_season_hr_lookup`
  uses `WHERE date >= ? AND date < ?` ✓
- [fetch_daily_data.py:1326](fetch_daily_data.py:1326) —
  `if as_of_date is not None: splits = [g for g in splits if (g.get("date") or "") < as_of_date]` ✓
- [pitcher_profile.py:1156](pitcher_profile.py:1156) — `WHERE game_date >= ? AND game_date < ?` ✓
- [features_v2.py:771](features_v2.py:771), [:1493](features_v2.py:1493) — `WHERE bhe.game_date < ?` ✓
- [etl/etl_nightly.py:618](etl/etl_nightly.py:618) — `WHERE game_date < ? ...` ✓
- [etl/backfill_park_archetype.py:136](etl/backfill_park_archetype.py:136) — `WHERE game_date < ?` ✓
- [etl/backfill_form_archetype.py:150,154,355](etl/backfill_form_archetype.py:150) — strict-less-than ✓

The byDateRange call is the only place I found where the convention
breaks.

## E. Backtest column coverage

Method: enumerate every `pick_inputs` column (65 total per
`PRAGMA table_info`), confirm written by `load_picks_to_db.py`, confirm
read by `backtest_factors.rescore_row` OR explicitly on the false-alarms
list.

### E1. MEDIUM (net-new) — `bats` column written but not read by backtest

`pi.bats` is written by load_picks_to_db.py (line 142 in the INSERT)
since 2026-05-03. backtest_factors.load_history's SQL at
[backtest_factors.py:90-107](backtest_factors.py:90) does NOT select it,
and rescore_row hardcodes `"bats": "R"` ([backtest_factors.py:156](backtest_factors.py:156))
with a stale comment claiming it's not stored. Detail in B3.

### E2. MEDIUM (net-new) — `pull_fb_pct` is read with default-None masking "never written" state

`pi.pull_fb_pct` IS in the load_history SQL (line 93) and IS read into
rescore_row's batter dict (line 142). But upstream (B1), it's 100% NULL.
Per spec: "MEDIUM: column read with a default that masks 'was never
written' state." Backtest never sees this input's signal because it's
always None — same as production, but the column-coverage check passes
because the read exists.

### E3. — Columns intentionally not read (per false-alarms list)

Confirmed against CLAUDE.md's "False alarms — DO NOT try to fix" list:
- `recent_barrel_real_21d`, `recent_xwoba_contact_21d`, `recent_iso_21d`,
  `recent_barrel_real_28d`, `recent_xwoba_contact_28d`, `recent_iso_28d`
  (B12 wider-window backtest-only). On the list.
- `form_archetype_centroid_json`, `form_archetype_window`,
  `form_archetype_n_hrs` (Phase 2 form-archetype). On the list.
- `park_archetype_centroid_json`, `park_archetype_n_hrs` (Phase 2
  park-archetype). On the list.
- `fb_slg`, `fb_pa`, `br_slg`, `br_pa`, `os_slg`, `os_pa` (Phase 2
  pitch-type splits). On the list.

These columns ARE written by load_picks_to_db.py. They're NOT in
load_history SQL. When the corresponding `USE_*` flag flips on, the SQL
will need rows for them. NOT flagged as findings per the false-alarms
policy.

### E4. — Legacy columns kept for historical replay

- `recent_hr_14d`, `recent_barrel_pct_14d`, `ev_trend_14d`. Per the
  comments in [etl/db.py:547-549](etl/db.py:547), these are legacy
  proxies (pre-2026-05-19); retained on historical rows for backtest
  replay of pre-#56 dates. Backtest reads `recent_hr_10g` / `recent_iso_30g`
  / `recent_avg_30g` (new columns) instead. NOT a finding — by design.

- `recent_avg_30g` IS in load_history SQL (line 94) and READ via
  `batter["recent_avg_30g"]` in rescore_row (line 145), but `score_form`
  per B11 (2026-05-26) no longer uses it (dropped from the mean). Stays
  loaded for replay of pre-B11 dates. NOT a finding.

## Items I would have flagged but they're on CLAUDE.md's False Alarms list

Listed here to prove I read the list (per spec) and didn't waste a HIGH
on a known issue:

1. **`pick_inputs.ev_trend` 100% NULL.** Confirmed empirically (gated
   on A2, real Statcast EV ETL). Skipped.
2. **`daily_lineup.batting_order > 9` (428 historical rows).** Confirmed
   empirically: last 10 dates all show 0 rows >9. The 428 residue is
   pre-fix; recurrence would be a real bug. Skipped.
3. **`backtest_factors.rescore_row` missing B12 21d/28d columns.**
   Confirmed in E3. Skipped.
4. **`backtest_factors.rescore_row` missing Phase 2 archetype columns.**
   Confirmed in E3. Skipped.
5. **`hr_fb_pct` anchor `(8, 20)` mis-cal.** Restated in A; flagged
   HIGH for completeness but explicitly marked "restated."
6. **`pitcher_fb_pct_allowed > 100` (23 rows).** Restated in C3. Known
   Savant parse bug.
7. **`season_batting.team='???'` for ~20 Athletics rows.** Restated in
   C4. Known.
8. **T4-untiered NULL `barrel_pct_source`** (B13 in BACKLOG). Not
   directly investigated; T4 is mentioned in B4 for the xwoba/pull_fb
   gaps.
9. **`score_lineup_position` table anti-correlated with HR rate** (B15).
   Not re-investigated.
10. **Weather API failures since 2026-05-12.** Confirmed empirically:
    `weather_source='api_failed_default'` is present on recent dates.
    Known.

## What I did NOT audit (exhausted vs sampled)

**Exhausted:**
- Every fixed-anchor scoring input listed in the spec section A. All
  have empirical citations.
- Every column in `pick_inputs` checked against `backtest_factors.load_history`
  SELECT and `rescore_row` reads.
- Every DB hygiene query in spec section C.
- Every `as_of_date` / look-ahead pattern in `generate_picks.py`,
  `fetch_daily_data.py`, `pitcher_profile.py`, `features_v2.py`,
  `etl/etl_nightly.py`, `etl/backfill_*.py`.

**Sampled:**
- **Calculation paths in `pitcher_profile.py`** — checked the
  `effective_hr9/era/k9` blend, did not exhaustively trace every
  victim-profile / arsenal calculation. The B6a/B12 backtest harnesses
  appear to be more thorough on this; I read but didn't run them.
- **`features_v2.py` Phase 2 pipeline.** Schema columns are populated
  per CLAUDE.md; centroid math itself not audited (relies on the
  design doc — out of scope).
- **`refit_weights.py`** — read enough to confirm it reads from
  pick_inputs and uses `score_*` functions, did not trace its
  OOS-holdout logic or the `--custom` flag implementation.
- **`mlb_hr_bet_site/` dashboard inputs.** Out of scope (presentation
  layer).
- **`outcomes-without-daily_lineup` root cause analysis** (C1).
  Surfaced the gap and one example (Yordan Alvarez); did not
  characterize whether it's lineup-parser misses vs. legitimate late
  add-ins.
- **Park factors data freshness** (B3 in BACKLOG / handoff). Schema
  read; data values not compared against Savant.
- **`live-hr` worker** (`workers/live-hr/`). Out of scope.

## Reproducibility

- Empirical script: [`_review/audit_inputs_run.py`](../_review/audit_inputs_run.py)
- Summary data: [`_review/audit_inputs_data.json`](../_review/audit_inputs_data.json)
- DB: `C:/dev/Claude/Projects/data/hr_bets.db` after
  `python infra/r2_sync.py pull` at 2026-05-26 ~22:35.
- Python 3.14 on Windows / PowerShell (verified by running the audit
  script under the user's runtime per CLAUDE.md hard rule #5).
