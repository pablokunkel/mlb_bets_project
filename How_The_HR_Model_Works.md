# How the MLB HR Model Picks Its Daily Card

A plain-English walkthrough of how the model decides who's most likely to hit a home run on any given day. Last updated 2026-05-06.

> For the deploy / release process, see `DEPLOY.md`. For component map and DB tables, see `ARCHITECTURE.md`.

> **Tuning approach:** we fix score curves before refitting weights. The diagnostic dashboard surfaces "this input has signal we're not capturing" before "this factor is over- or under-weighted." Curve fixes change what each input *means*; weight refits then re-balance the now-correct inputs against each other. Doing them in the other order (weight refit on broken curves) just baked the brokenness in.

---

## The 30-second version

Every batter in every MLB game today gets a score from 0 to 100 called the **composite**. The composite is a weighted blend of six factors: how much raw power the batter has, how bad a matchup the pitcher is for him, how friendly the ballpark is to his handedness, how hot he's been lately, what the weather looks like at first pitch, and where he hits in the lineup.

We sort the full board by composite, then take the top 8 — but with a couple of guardrails: no more than 2 picks from any single game (so we're not over-stacked on one matchup), and only confirmed starters with a real batting-order spot 1-9 (no bench guys). Tiers (T1/T2/T3 by season HR rate) appear as labels on the board but **don't gate selection** — the picks are just the top 8 composite scores subject to those guardrails.

That's the whole thing. Everything below is the detail.

---

## Step 1: Build the daily slate

Before anything can be scored, the model has to know what games are being played today, who's pitching, and who's in the lineup.

- **Schedule and probable pitchers** come from the free MLB Stats API. We get every game on today's date, the starting pitchers, venue, and first-pitch time.
- **Confirmed lineups** come from MLB's Stats API `schedule?hydrate=lineups` endpoint. The parser respects the order MLB returns (positions 1-9); anyone beyond 9 is a bench reserve and gets marked as such (bench reserves never make the final card, regardless of their composite). When a posted lineup isn't yet up at scoring time (afternoon manager fills, late-arriving slates), the model falls back to that team's most-recent posted lineup from the prior 14 days — a real batting order from a few days ago is dramatically more accurate than the alphabetical 26-man roster, which was the previous fallback. Bdfed roster (alphabetical) remains as the last-resort fallback, with `lineup_source` stamped on every batter so the dashboard shows where the order came from. (Critical bug fix 2026-05-04: prior to this, the model used MLB's bdfed/matchup endpoint, which returns the alphabetical 26-man roster — not the batting order. The model was effectively scoring random hitters at "position 1-9" for any team without a posted lineup. Hit rate dropped to ~17% during the bug window; recovered to ~37% after the fix.)
- **Weather** comes from Open-Meteo, a free forecast API. We look up the ballpark's coordinates, convert first-pitch to the stadium's local timezone, and pull the hourly temperature, wind speed, wind direction (meteorological "from" convention), and humidity. Domes are flagged so weather scoring can return a flat neutral instead of fictional readings.
- **Park factors** come from a curated table stored in our own database. Each park has an overall HR factor, a left-handed batter HR factor, and a right-handed batter HR factor. 100 is league average, 130 is Coors, 82 is Oracle Park.
- **Pitcher season stats** (ERA, HR/9, hard-hit percentage allowed, strikeout rate) come from the MLB Stats API.
- **Pitcher fly-ball percentage allowed** and **batter xwOBA on contact** come from Baseball Savant via bulk CSV downloads.
- **Pitcher "arsenals"** (average fastball velo, pitch mix, spin rate, release extension) come from Baseball Savant Statcast pitch-by-pitch data, cached for a week.
- **Every HR the batter has hit over the last 18 months** — the pitcher who gave it up, the pitch type, the velo, the spin — is stored in our database. This feeds the archetype matching explained later.
- **Vegas implied team totals** come from the-odds-api.com (DraftKings book). The game total is split between the two teams, weighted by the moneyline-implied win probability, and used as a "this team is expected to score a lot" signal.

All of this is pulled by the morning ETL once and cached so the noon scoring runs in a few minutes (cold cache the first time can be 30-50 minutes due to the per-pitcher Statcast pulls; cached subsequent runs are quick).

---

## Step 1.5: Live tier data and the `season_batting` fallback

The "live tiers" used to bucket batters into T1/T2/T3 are built from MLB Stats API splits with synthesized Statcast estimates: `barrel_pct ≈ hr_per_pa × 200`, `exit_velo ≈ 82 + slg × 15`, `hr_fb_pct ≈ hr_per_pa × 100 × 1.8`. These are noisy day-to-day — a hitter who suddenly went 0-for-8 across two games can see his synthetic barrel estimate halve overnight even though his real Statcast contact quality is unchanged.

To stabilize, `generate_picks.enrich_with_season_batting()` backfills any zero/missing power input on a tier batter from the canonical `season_batting` table (refreshed nightly). Marks `_power_source = "season_batting_fallback"` for diagnostics.

**Caveat:** the `season_batting` table itself is currently populated by the same synthetic-estimate path via `etl_nightly.sync_season_batting`, so the "fallback" can sometimes be one synthetic value replacing another. Real Statcast values from FanGraphs / Savant aren't yet stored alongside synthetic ones. Tracked as a known issue (see "Known issues" below).

**Earlier bug, now fixed:** prior to 2026-05-01, `fetch_daily_data._splits_to_batters` also applied a within-tier renormalization that compressed `barrel_pct`, `exit_velo`, `hr_fb_pct`, and `iso` into tier-relative ranges. That step was removed because it was destroying real Statcast signal — a Tier 1 hitter with a 92 mph exit velo (mid-pack for T1) was being normalized to ~50 instead of being recognized as a real-world above-league-average reading. With the renormalization gone, scores reflect actual contact quality, not within-tier rank.

**Untiered confirmed starters (added 2026-05-02):** `build_live_tiers` qualifies on `games >= 5 AND hr >= 1`. Real confirmed starters who don't meet that bar (slow starts, returning IL, rookies) are scooped up by a 4th `score_untiered_starters` pass and tagged `tier=4` ("T4-Untiered"), so the dashboard sees every batter actually starting tonight. Originating bug: 2026-05-02 SEA/KC autopsy showed 5 of 9 SEA starters silently dropped at the tier-filter step.

---

## Step 2: Tiers (just labels, not gates)

Players are bucketed into three tiers based on their rolling 40-game HR-per-PA rate:

- **Tier 1 (Chalk):** top ~15% of qualified batters. Judge, Ohtani, Schwarber types.
- **Tier 2 (Mid):** next ~30%. Solid power bats.
- **Tier 3 (Longshot):** next ~30%. Real but volatile power.
- Bottom ~25% don't get scored.

**Important change from earlier versions of this doc:** tiers are now **labels on the board**, not selection gates. The current selection logic just takes the top 8 composite scores (subject to the per-game cap and starter filter). Tiers are still useful context for thinking about the picks, and they show up in the dashboard's tier label column, but the model doesn't enforce a tier mix.

---

## Step 3: Score each batter on six factors

For every batter in the slate, the model computes six sub-scores on a 0-100 scale. Here's each one in plain English with the inputs that feed it.

### Factor 1 — Power Score (weight: 0.250)

"How much raw power does this batter have?"

Six inputs from season-to-date stats and Statcast:

- **Barrel percentage.** The share of batted balls that are "barreled" — the ideal exit velocity / launch angle combo that produces HRs.
- **Exit velocity.** Average speed off the bat.
- **HR/FB percentage.** Of all fly balls, what share clear the fence.
- **Isolated Slugging (ISO).** Slugging minus average — pure extra-base power.
- **xwOBA on contact.** Expected wOBA computed from Statcast launch parameters on every batted-ball event. Filters out luck on outcomes; rewards quality of contact.
- **Pull-FB percentage.** Of fly balls hit, the share pulled to the batter's natural HR side. Some hitters elevate to all fields but only clear the fence on pull; this isolates that.

Each input is scaled to 0-100 against fixed anchors that reflect actual MLB distributions (league-avg → 0, elite → 100), and the available ones are averaged:

| Input             | League avg → 0 | Elite → 100 |
|-------------------|----------------|-------------|
| barrel %          | 5              | 15          |
| exit velo (mph)   | 85             | 95          |
| HR/FB %           | 8              | 20          |
| ISO               | 0.130          | 0.300       |
| xwOBA on contact  | 0.330          | 0.450       |
| pull-FB %         | 8              | 22          |

The 2026-05-03 anchor re-tune (PR #25) tightened these from earlier generous ranges (barrel 0-25, EV 80-100, HR/FB 0-30, ISO 0.100-0.350, xwOBA 0.280-0.500, pull-FB 5-25) so MLB-leading values actually reach the top of the scale. Aaron Judge's 17% barrel was scoring 68 under the old anchors; under the new ones it's 100. Mid-tier scores tightened ~3-5 pts; elite scores widened ~15-20 pts; under-replacement bottoms out near 0 (was ~20). Net effect: more rank discrimination at both tails.

**Every input uses an `is not None and > 0` skip-on-missing check** — a missing or zero reading is dropped from the average rather than dragging it toward zero. Prior to 2026-05-01, missing barrel% and HR/FB% were silently scored as 0, which dragged elite hitters with sparse Statcast data (e.g., Buxton on a return-from-IL day) to power scores in the teens despite their actual contact quality. The fix made the skip-on-missing behavior uniform across all six inputs.

**Season-HR floor (added 2026-05-03, PR #25, default on).** A hard, score-level floor on power score keyed off the batter's accumulated season HR count. The floor only ELEVATES — it never pulls a good score down. Tiers:

| Season HR | Power-score floor |
|-----------|-------------------|
| 5+        | 50                |
| 8+        | 60                |
| 12+       | 70                |
| 18+       | 78                |
| 25+       | 85                |

Originating case: 2026-05-02 HR autopsy showed real power hitters being muffled by noise from the other 5 composite factors. Drake Baldwin homered for his 8th of the season ranked #97 on our board; Jake Burger homered for his 6th ranked #243. Same season-HR count was producing wildly different ranks (Buxton 10 HR rank #8, Walker 10 HR rank #85, Baldwin 8 HR rank #97). The floor says: an 8-HR hitter shouldn't be scoring below league-average on power. Highest qualifying tier wins. Gated behind the `USE_SEASON_HR_FLOOR` flag; flipped on 2026-05-03 after a 14-day backtest harness (`backtest_flags.py`) showed decisive wins on all 4 metrics (top-8 hit rate, top-30 hit rate, AUC, Spearman rank correlation).

A second related flag, `USE_CAREER_PRIOR`, exists in `score_batters.py` (off by default). It would Bayesian-shrink small-sample per-PA rates toward career mean — a complementary fix to the floor for "his rates LOOK bad due to small sample" cases. Parked off until backtest validation; the harness can compare floor-only vs floor+prior to tell whether stacking both adds signal.

**B6a real recent-Statcast inputs (collected but not scored, gated behind `USE_RECENT_STATCAST_BLEND`).** Three rolling 14-day Statcast metrics — `recent_barrel_real_14d`, `recent_xwoba_contact_14d`, `recent_iso_14d` — are populated nightly into `pick_inputs` by the bulk Statcast ETL. The intent was for these to blend into `score_power` alongside the season inputs (a slow-starter-now-hot hitter would get credit before his season aggregate catches up). As of 2026-05-23, **the blend flag stays off**: `backtest_power_inputs.py` shows the 14d window is too noisy to discriminate HR hitters from the field — synthetic season inputs (which are essentially smoothed past performance) beat the 14d real metrics by ~0.10 AUC across multiple probe variants (anchor-tightening, dropping the HR-rate-encoded synthetic inputs). The infrastructure is preserved (inputs collected, harness exists, columns in `pick_inputs`) and a wider-window variant (21d/28d) is queued as B12 before declaring B6's blend permanently dead. See WEIGHT_REFIT_LOG.md 2026-05-25 for the empirical detail.

### Factor 2 — Matchup Score (weight: 0.264 — the heaviest factor)

"How vulnerable is today's pitcher, and does his style match the kind of arm this batter feasts on?"

This is the smartest factor. When archetype data is available (the daily path with `USE_PER_PLAYER_STATCAST=True`), it has up to four signals blended in equal weight:

**Signal A: Pitcher vulnerability.** Combines HR/9, ERA, hard-hit percentage allowed, strikeout rate, and fly-ball percentage allowed. An ace gets a vulnerability score of 10-15; a back-end starter gets 70-80. All within-slate-percentile-ranked so the day's best matchup is always near 100 and the worst near 0, regardless of slate quality.

Vulnerability inputs use a season + recent-window blend (`effective_hr9` / `effective_era` / `effective_k9` in `pitcher_profile.py`, added 2026-05-13 for HR/9 and extended 2026-05-21 to ERA + K/9). When the pitcher has 2+ starts in the configured recent window, the recent value blends in at 0.60 weight against the season number. Catches collapsed-form pitchers whose season aggregate hasn't caught up yet (e.g., Brady Singer on 2026-05-12: season HR/9 of 1.89 vs. recent 3.07 across 4 starts — effective HR/9 lands at 2.6, properly midway). The recency window is configurable (`PITCHER_RECENT_WINDOW_TYPE` = "days" | "starts", `PITCHER_RECENT_WINDOW_N` defaulting to 21 days) — alternate windows tunable via the B4 backtest harness.

**Signal B: Archetype similarity.** For every batter, we look at every HR he's hit over the last 18 months and build a "victim profile" — the weighted-average pitcher type he crushes (fastball velo, pitch mix, handedness, spin, extension). That profile gets compared against today's pitcher across seven dimensions. A higher similarity score means today's pitcher matches the archetype this batter has historically homered off.

Victim-profile + pitcher-arsenal lookups read from the local SQLite cache, refreshed nightly by `etl_nightly`. The 2026-05-22 DB-backed-as-of-date path (`pitcher_profile._build_victim_profiles_from_db`) means historical reconstruction (backfill, backtest) can grade archetype matchups in seconds per date instead of hours — the per-player Statcast roundtrip that used to dominate backfill runtime is gone. The live noon path still falls through to per-pitcher Statcast for any starter not yet picked up by the nightly arsenal sync (rookies, fresh callups).

**Signal C: Vegas implied team total.** The percentile rank of this team's expected runs across the slate. Captures "this is a slugfest game in Coors with Vegas at 11.5" vs. "this is a 7.5-total pitching duel."

**Signal D: Batter wOBA vs. pitcher handedness.** (Added 2026-04-30.) The 2026-04-30 input calibration found `woba_vs_hand` was the single biggest signal-not-captured input — HR rate climbs 4.5x across woba quintiles, but the v2 matchup score wasn't using it at all (only v1 was). Added as a fourth signal, with anchors tightened to 0.280-0.420 (the empirical 20th-80th percentile of the active distribution) so the curve actually spreads across real data rather than compressing into a 30-60 band.

The four signals get averaged (variable arity — Vegas and woba drop out if data is missing for a batter, leaving just vulnerability + similarity). Then **three adjustments**:

- **Ace dampener.** If the pitcher's vulnerability is below 25 (truly elite), we multiply the whole matchup score by 0.70. Below 40 (good), we multiply by 0.85. Stops the model from getting cute and picking against Skubal just because his arsenal happens to match a batter's victim profile.
- **Rookie pitcher bonus (added 2026-05-03, PR #26).** When the opposing pitcher has fewer than 300 career Statcast pitches, we add +15 to every batter's matchup score against him (capped at 100). Originating case: a fresh callup with 50 career pitches has thin Savant data, so all the archetype/vulnerability inputs land at league-average and the matchup scores in the 50s — but rookie pitchers historically allow ~20% more HRs per 9 than their season-three veterans. The +15 baseline corrects for that prior. The rookie set is computed by `generate_picks.load_rookie_pitcher_ids` and stamped on the pitcher dict as `is_rookie=True`.
- **No platoon bonus.** Earlier versions added a flat +5 for opposite handedness, but that was double-counted with the handedness already baked into archetype similarity. Removed in v2.

If archetype data is missing (new pitcher, no HR history for the batter, or it's a slate where per-player Statcast isn't running), the model falls back to **v1 matchup scoring**: pitcher vulnerability + woba_vs_hand + Vegas implied total, two-thirds vulnerability and one-third the others.

### Factor 3 — Park Score (weight: 0.000 in weighted average, but with a 0.05 additive bonus)

"How HR-friendly is this specific ballpark for a batter of this handedness?"

Each park has three numbers in our table: overall HR factor, LHB factor, RHB factor. The model picks the one matching batter handedness (switch-hitters get the average) and scales it to 0-100 via within-slate percentile.

**Why the regression weight is zero:** when the model was retrained on enriched data with logistic regression (March 2026), park factor came out essentially non-predictive *net of the other features*. The signal in park factor was already being captured indirectly through pitcher vulnerability (pitchers in homer-friendly parks have inflated HR/9) and weather (warm parks tend to be hitter-friendly). The coefficient was tiny and got dropped from the weighted average.

**But the additive bonus changes the story (added 2026-05-03, PR #25).** Park-as-fixed-anchor is a noisy signal because batters play their home park ~50% of games (signal washes), but park-as-within-slate-percentile *is* a real edge: Yankee Stadium today (PF 115, top of the slate) vs. Petco (PF 92) is a 25-point park-score spread that a zero weight throws away. So we add a purely additive `+0.05 × park_score` bonus on top of the weighted-average composite. This shifts every composite up a few points (+2.5 average, +5 for top parks, +0 for the worst), but rankings are what matter, and the bonus brings hot-park batters up the board where they belong without re-stealing weight from another factor and re-fitting all the others.

So in practice the park score contributes 0-5 points to the final composite. The dashboard still surfaces park as its own column for diagnostic context.

### Factor 4 — Form Score (weight: 0.279 — the heaviest factor alongside matchup)

"Is this batter hot right now?"

Four inputs drawn from MLB Stats API game logs over split game-count windows (rebuilt 2026-05-19, PR #56):

| Input              | Window           | Anchor (low → high) | Notes |
|--------------------|------------------|---------------------|-------|
| `recent_hr_10g`    | 10 games         | 0 → 5 HR            | Short window; HRs are lumpy. |
| `recent_iso_30g`   | 30 games         | 0.100 → 0.300       | Power-specific, longer sample. |
| `recent_avg_30g`   | 30 games         | 0.210 → 0.330       | Contact signal. **Note: empirical backtest (2026-05-23) shows this term is net-noise for HR prediction — drop pending under BACKLOG.md B11.** |
| `ev_trend`         | nightly Statcast | -3.0 → +3.0 mph     | Real exit-velocity trend vs. season. **Currently always NULL — gated on the A2 nightly Statcast ETL.** |

Each populated input is scaled to 0-100 against its anchor; available inputs are equally-weight averaged. A None input is SKIPPED (no data); a real 0 is scored honestly. Score is the mean of however many inputs were measured; falls back to 50 if all four are NULL.

**Long-rest dampener.** After the mean, `_layoff_dampener` reads the `recent_window_days` value attached to the form fetch. When the window stretches > 55 days (IL absence, long roster gap), the score is pulled toward 50 — ramping to 60% pull at 90 days. Prevents stale 30-game windows from carrying a hot reading that's actually months old.

This factor turns out to carry more signal than earlier versions assumed — the regression refit pushed form's weight from ~0.15 to ~0.28.

**Pre-PR-#56 history.** The pre-#56 form factor used estimated 14-day barrel% (`recent_barrel_pct_14d`) and a synthetic EV-trend (`ev_trend_14d`) on the same 14-day window. Both were dropped: barrel estimation on a 14-day game-log window is mostly noise (PR #56 backtest), and the EV trend was always a hand-wave proxy waiting for the nightly Statcast ETL to ship. Legacy columns `recent_hr_14d` / `recent_barrel_pct_14d` / `ev_trend_14d` remain in `pick_inputs` (NULL for new rows) for backtest replay of pre-#56 days.

### Factor 5 — Weather Score (weight: 0.057 — small but real)

"Is the weather helping or hurting HRs today?"

Domes return a flat neutral score (50). Outdoor games combine three signals:

**Temperature.** Piecewise curve calibrated to MLB HR/temperature data. Below 50°F: cold dense air kills carry (score 25 or worse). 68°F is neutral (50). 85°F is a real boost (72). 95°F+ is the kind of heat that turns warning track outs into souvenirs (88+).

**Wind.** This is the clever one. We don't just look at wind speed — we look at *wind direction relative to the pull side for this batter's handedness*. For each park we have the compass bearing from home plate to center field. We compute the angle between the wind's "to" direction (where it's blowing toward) and the batter's pull-side bearing (CF + 45° for lefties pulling to RF, CF - 45° for righties pulling to LF). The cosine of that angle, scaled by wind speed (capped at 15 mph for the speed factor), produces a -25 to +25 modifier on top of the neutral 50. A 15 mph wind blowing perfectly out toward a lefty's pull side scores 75; blowing in scores 25.

**Humidity.** Mild linear modifier (35 + h × 0.30, where h is percent). Counterintuitively, humid air carries baseballs slightly farther because water vapor displaces denser N₂ and O₂ — the warm humid combo is HR-friendly.

The three signals blend with within-slate percentile weighting plus the per-batter wind component (60% slate base quality + 40% wind alignment when slate context is active).

### Factor 6 — Lineup Score (weight: 0.150)

"Where in the order does this batter hit?"

Simple piecewise scoring of batting position 1-9. Top of the order (1-3) gets the highest score because they get more plate appearances per game. Cleanup hitters (4) get a small premium for protecting the top. Middle of the order (5-6) is solid. Bottom (7-9) gets the lowest — fewer PAs, often weaker hitters anyway.

This factor wasn't in earlier versions and was added specifically because expected-PAs is a real HR signal we were ignoring.

---

## Step 4: Combine into the composite

The composite is a weighted average of the six sub-scores plus a park bonus and a platoon multiplier:

| Factor   | Weight |
|----------|--------|
| Power    | 0.250  |
| Matchup  | 0.264  |
| Park     | 0.000  |
| Form     | 0.279  |
| Weather  | 0.057  |
| Lineup   | 0.150  |

```
composite = (0.250 × power
           + 0.264 × matchup
           + 0.000 × park
           + 0.279 × form
           + 0.057 × weather
           + 0.150 × lineup)
           + 0.05 × park                    ← additive park bonus (PR #25)
composite *= platoon_dampener(games, slate_max_games)   ← multiplicative haircut (PR #28)
```

These weights were learned via logistic regression on a 20-day enriched backfill (~5,200 batter-game rows, hit_hr as target, 7 features standardized). Backtest progression:

- **Legacy fixed-anchor model:** 36.04% top-8 hit rate
- **v1 (learned weights + within-slate percentile rerank):** 38.75%
- **v2 (current — learned weights + xwoba_contact + fb_pct_allowed + Vegas):** 40.00%

> **About that 40%:** that figure came from an in-sample backtest. Live performance was running ~36-40% through April 2026 — and then dropped to ~17% in early May before we discovered (2026-05-04) that lineups were being read from MLB's bdfed/matchup endpoint, which returns the alphabetical 26-man roster, not the batting order. The model was effectively scoring random hitters at "position 1-9" for any team without a posted lineup — which is most teams during the noon scoring window. After the lineup endpoint fix (PR #32) and recent-lineup fallback (PR #33), live performance recovered to 37.5% on the first two clean days (5/4 + 5/5: 6 of 16). We're treating the 36-40% backtest band as the realistic target, not a guarantee.

The model supports other configs for ablation comparison (`legacy`, `matchup_heavy`, `power_heavy`, `form_heavy`, `no_weather`) but `default` is what runs day-to-day.

**Park bonus (additive).** After the weighted average, we add `0.05 × park_score`. See Factor 3 above for the rationale — it brings hot-park batters up the board without re-stealing weight.

**Platoon dampener (multiplicative, added 2026-05-03, PR #28).** Multiplicative haircut on the composite for batters who don't start every game, computed as:

```
play_rate     = batter_games / slate_max_games        (0 to 1)
dampener      = 0.90 + 0.10 × play_rate               (0.90 to 1.0)
composite    *= dampener
```

`slate_max_games` is the highest `games` count in today's batter pool — the daily-starter benchmark. A batter at 75% play rate (typical platoon hitter who sits vs. same-handed starters) gets multiplied by ~0.925; a daily starter gets 1.0 (no haircut). The floor at 0.90 means platoon bats stay visible but slip a few ranks vs. otherwise-equivalent daily starters — this is right because a platoon bat is more likely to be late-scratched or pulled mid-game when the matchup flips. The dampener is `1.0` (no-op) when `games is None` (don't penalize rookies / IL returns) or when slate context isn't available (offline path). Applied AFTER the park bonus so the multiplier scales the entire composite cleanly.

**Example walkthrough.** Bryce Harper (L) at Yankee Stadium against a middling righty on a mild sunny day, daily starter (play_rate = 1.0):

- Power = 82
- Matchup = 70
- Park = 95
- Form = 60
- Weather = 55
- Lineup = 80 (he hits 2nd or 3rd)

```
weighted_avg = 0.250×82 + 0.264×70 + 0.000×95 + 0.279×60 + 0.057×55 + 0.150×80
             = 65.625
park_bonus   = 0.05 × 95 = 4.75
composite    = (65.625 + 4.75) × 1.0 = 70.4
```

That'd put him near the top of the board — likely a pick. If Harper were a 75% platoon bat instead, the dampener would knock him to ~65.1.

---

## Step 5: Build the 8-pick card

After scoring, we have a "full board" of every scored batter sorted by composite. Selection has five rules, applied in order:

1. **Take the highest composite score available.**
2. **Max 2 picks per game (game_pk).** If a single game's lineup has 4 batters in the top 8 by raw composite, only the top 2 of those make the card. The rest stay on the visible board but don't get a star. This prevents one rained-out game from torching half the card.
3. **Must be a confirmed starter.** `batting_order` integer between 1 and 9. Anyone with batting_order = NULL, "bench", or 10+ is excluded. (This was a real bug fix — before the filter was tightened, the model occasionally picked Seiya Suzuki at bench position 11.)
4. **Game must not be postponed/cancelled/suspended (added 2026-05-05, PR #40).** `generate_picks` filters games out of the slate before scoring whenever `gameStatus.detailedState` matches `Postponed*`, `Cancelled*`, or `Suspended*` (substring + case-insensitive). Originating case: 2026-05-05 NYM @ COL was scheduled and posted lineups, then got rained out and rescheduled — but the lineup data was still in our DB at noon scoring. Without this filter, generate_picks would happily score that game's batters and the card could include picks for a game that won't be played. When every game on the calendar is postponed, the script exits early ("no slate today") rather than producing zero picks.
5. **Pick-input row hygiene** (added 2026-05-01): `load_picks_to_db.py` excludes from `pick_inputs` any row where all four power inputs (barrel%, exit velo, HR/FB%, ISO) are missing or zero. These rows would otherwise poison the per-factor decomposition charts and the weight refit's training data with all-zero feature vectors.

We loop down the sorted board, applying these filters, until we have 8 picks. The card gets sorted by composite (best first) for display.

**No tier quotas. No repeat penalty.** Earlier versions of this doc described a (3 T1, 2 T2, 3 T3) tier split and a "subtract 3 points if picked in last 5 days" repeat penalty. Both have been removed. The model just picks the top 8 by composite subject to the two guardrails above.

---

## Step 6: The diagnostic dashboard

After picks ship, the model isn't done — we run a battery of retro diagnostics on every day's outcomes to learn where it's right and wrong. The dashboard has six tabs (Today's Picks, Big Board, HR Recap, Hitters, History, Performance) with the Performance tab housing the diagnostic suite:

- **Factor Trends.** Per-factor average scores over time, with a v1→v2 model boundary marker so you can see when the math changed.
- **Composite Score → HR Rate.** A histogram of the entire board's composites with HR rate overlaid as a line. Confirms the model's signal climbs cleanly from low single digits at composite 20 to ~40-50% at composite 75+.
- **Per-Factor Mean.** For each factor, mean score among batters who HR'd vs. those who didn't, last 30 days. Big green-vs-gray gap = factor doing real work; flat pair = factor not differentiating.
- **Factor Decomposition.** What raw inputs are inside each factor score, with HR vs. miss means for each. Filter chips for composite band (0-40, 40-60, 60-80, 80+) and rank band (#1-10, #11-30, #31-100, #101-300) to slice the picture.
- **Why Are HR Hitters Stuck at 40-60?** Compares HR hitters who scored 40-60 (model under-rated them) vs. HR hitters scored 60-80 (model caught them). The biggest input gaps are tuning levers.
- **HR Hitters by Daily Rank.** Three-way comparison: HR hitters we picked (#1-10) vs. fringe (#11-30) vs. deep (#31-100). Controls for slate-to-slate variance.
- **Input Calibration.** For each raw input, we bin into 5 quantile buckets and compare the empirical HR rate per bucket against the average sub-factor score. Status flags: ALIGNED, SIGNAL_NOT_CAPTURED, OVER_WEIGHTED, NO_SIGNAL, NOISY. The actionable diagnostic for fixing per-input score curves.
- **Dome vs Outdoor.** Summary cards (suppressed when sample is too small) plus a cross-tab of dome × park-factor band → HR rate. Tests whether the model's preference for dome environments is justified by yield.
- **Wind Direction Effect.** Outdoor games binned by helping factor (cosine of wind-to-CF angle × MPH). Tests the wind-direction logic against empirical HR rates.
- **Temp × Humidity Heatmap.** 4×4 cells with HR rate per combo and an interaction delta vs. additive baseline. Surfaces synergies (hot+humid should be green) and antagonisms (cold+humid red).
- **Elite Pitcher × Archetype Heatmap.** 5×5 grid of pitcher_vulnerability_quintile × archetype_similarity_quintile. The diagnostic cell is row 1 (elite pitcher) × col 5 (high archetype match) — if HR rate is high but matchup score is low, the dampener is firing too hard on cases where archetype is screaming "vulnerability."
- **Pick Composition.** Distribution of selected picks by park factor, batting order, dome status. Surfaces systematic biases that aren't visible per-pick.

The diagnostics are how we know what to tune next, and they live in `export_site_data.py`'s `_factor_decomp_*`, `_input_calibration`, `_temp_humidity_heatmap`, `_archetype_dampening_diagnostic`, `_dome_vs_outdoor`, `_wind_direction_diagnostic`, and `_pick_composition` functions.

### Continuous backtesting (Performance tab → Backtest panel)

Added 2026-05-01 as a nightly check that the model's per-factor signal is still real. `backtest_factors.py` reads every batter from `pick_inputs` for the last 7d / 30d / season, **rescores each row using today's scoring functions** (so a function change shows up immediately rather than only on next monthly refit), bins each factor's score into quintiles, and computes:

- **Lift:** (HR rate of top quintile) − (HR rate of bottom quintile). Positive = factor differentiates HR hitters from misses.
- **AUC:** how well the factor's score ranks HR hitters above non-HR hitters. 0.50 is random; 0.55+ is real signal.
- **Monotonicity:** does HR rate climb monotonically across quintiles? Non-monotonic signal often means the score curve has the wrong shape rather than the input being dead.

Output: `mlb_hr_bet_site/data/factor_accuracy.json`, rendered in the Performance tab's Backtest panel. Run nightly via `run_outcomes.bat` step 2.

### Live HR feed and the Topps modal (added 2026-05-02)

Separate from the picks pipeline, a Cloudflare Worker (`dingersonly-live-hr` at `api.dingersonly.cc`) polls the MLB Stats API every minute for in-progress games and serves an aggregated "every HR today" feed. The dashboard's HR Recap tab polls this every 30 seconds while visible and renders a "Live Today" panel above the day-after recap.

Each live HR card is clickable and opens a Topps-style modal with: a baseball-diamond SVG showing where the ball landed (Statcast `coordX`/`coordY` mapped to a 250×250 viewBox, with a quadratic-bezier trajectory arc from home plate to the dot), exit velo / launch angle / distance stat cards, the broadcast description, and — when the batter is on today's board — their full model card (Composite + 6 factor scores + per-factor ranks + season-stats).

This is the user-facing closing of the loop: today's picks → live HRs → click any HR → see whether the model rated that batter highly.

A few diagnostic refinements as of 2026-04-30:

- **Archetype diagnostic filtered to v2-only rows.** Mixing v1 rows (which don't use archetype) was diluting the within-row signal. The 5×5 heatmap and the input_calibration archetype row now only count rows where `matchup_version='v2'`.
- **Input calibration status taxonomy.** Each input gets one of: ALIGNED (model captures the empirical signal), SIGNAL_NOT_CAPTURED (HR rate climbs but score doesn't follow — tuning lever), OVER_WEIGHTED (score climbs but HR rate is flat or noisy), NO_SIGNAL (both flat — input is dead weight), or NOISY (low n).
- **Cell-level n suppression.** Heatmap cells with n < 5 render gray with "n too small" — prevents single-game outliers (like the early-April single dome pick that read 100% HR rate) from dominating the visual.
- **Dome verdict suppressed under n < 20.** Same idea on the dome-vs-outdoor card.

---

## Historical calibration: prior-season backfill

For environmental factors (temperature, humidity, wind, dome status), waiting for this season's outdoor games to accumulate is wasteful — the physics of HR-vs-weather is stable across years. So we backfill 2024-2025 data from public sources to expand sample size for these specific diagnostics.

**The pipeline lives in `etl/historical_calibration.py`** and has two stages:

1. **Weather backfill via Open-Meteo's archive API.** Same fields as the live daily ETL (`temperature_2m`, `windspeed_10m`, `winddirection_10m`, `relativehumidity_2m`), just fetched against historical dates. ~2,400 unique (venue, date) tuples per season; ~1 hour rate-limited per season.

2. **HR outcomes backfill via pybaseball Statcast.** Pulls every PA-ending event for the season, aggregates to (game_pk, date, batter, home_team) → hr_count + pa_count. ~30 minutes per season.

3. **Materialized join** writes `historical_calibration` table with one row per (date, game_pk, batter_id) including weather + outcome.

The dashboard's temp×humidity heatmap has a "Source" toggle: **This season** (live picks, current default), **Historical** (2024-2025 backfill), **Combined** (both, weighted by N). Use historical for filling in rare bins (HOT × HUMID early in the calendar) and combined for the most stable tunable picture.

**What this is and isn't for.** Use for environmental factors only. Don't extend it to player-specific factors (form, archetype, pitcher matchups) — those are time-dependent and player-specific, so historical seasons don't validate this season's scoring.

To run: `python etl/historical_calibration.py --seasons 2024 2025` (or `--weather-only` / `--outcomes-only` for partial runs; `--build-table` to re-materialize after both pulls).

---

## What the model is NOT doing (yet)

A few things we know we're missing:

- **Park's regression weight is still zero.** Park signal now reaches the composite via the additive `+0.05 × park_score` bonus (PR #25, see Factor 3) — that's a deliberate workaround, not a real fix. The next refit on a larger / cleaner training window may bring park back into the weighted average if we can isolate the signal from correlated effects (pitcher vulnerability already captures most of "homer park" through inflated HR/9).
- **Per-pitcher Statcast on live noon runs.** Cold pitcher arsenal cache on the daily path can still take a few minutes for new starters (rookies, fresh callups). The historical-reconstruction path (backfill, backtest) is fixed as of 2026-05-22 — `pitcher_profile._build_victim_profiles_from_db` reads everything from local SQLite. The live noon path's cold-callup case is the remaining cost; mitigated by overnight cache pre-warming.
- **B6 real-recent Statcast blend is collected but off.** `pick_inputs.recent_barrel_real_14d` / `_xwoba_contact_14d` / `_iso_14d` populate nightly; `USE_RECENT_STATCAST_BLEND=False`. Empirically the 14d window under-discriminates vs. season aggregates (see WEIGHT_REFIT_LOG.md 2026-05-25). The wider-window variant (21d/28d) is queued as B12 before declaring the blend permanently dead.
- **No actual handedness splits.** We use woba_vs_hand (the batter's wOBA against their pitcher's handedness, season average), but not granular pitch-type splits. A batter who's .310 vs. RHP fastballs but .180 vs. LHP breaking balls scores the same against both today.
- **No bullpen factor.** If the starter only goes 5 innings, the batter might face very different pitchers later. We only score against the starter.
- **No lineup-context expected PAs.** We use batting_order as a proxy for PAs, but a batter behind two .400-OBP guys gets more bases-loaded chances than the same hitter behind two .280-OBP guys.
- **Park factors are static seed data.** Roughly the Baseball Savant 3-year rolling averages, but not refreshed live. The plan is to wire in a Savant pull so the table updates monthly.
- **Pull-FB percentage only fills on the live path.** Per-player Statcast computes it; the bulk Savant CSV doesn't expose it. Backfilled rows have NULL. Live runs populate it forward; over time the calibration data fills in.

---

## The one-paragraph recap

For every batter in today's lineup, we score six things on a 0-100 scale: raw power against fixed MLB-distribution anchors with a season-HR floor (25%), pitcher matchup including vulnerability + archetype-similarity-from-HR-history + Vegas implied total + a +15 rookie-pitcher bonus (26%), park HR factor (zero-weighted in the average but with a +0.05 additive bonus on top), recent two-week form (28%), weather including handedness-aware wind direction (6%), and batting-order position (15%). The composite is multiplied by a [0.90, 1.0] platoon dampener that knocks part-time bats down a few ranks. We sort the full board by composite, take the top 8 with no more than 2 picks per game, only confirmed starters at batting positions 1-9, and only games that aren't postponed/cancelled/suspended. Tier labels (T1/T2/T3 + T4-Untiered for low-game starters) appear on the dashboard but don't gate selection. After the day finishes and outcomes come in, a battery of diagnostics — input calibration curves, temp×humidity heatmaps, elite-pitcher-archetype dampening checks, rank-band HR hitter splits — feeds the next round of tuning. That's the model.
