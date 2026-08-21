# Model logic + data-accuracy audit — 2026-08-21

> **Read-only audit.** No code, data, scoring, or ETL changes. The only file
> added by this PR is this document. Deliverable is the ranked fix list below;
> nothing here has been fixed.

- **DB audited:** `C:\dev\Claude\Projects\data\hr_bets.db`, pulled fresh from R2
  at audit time (`python infra/r2_sync.py pull`, 105.8 MB). **The local copy was
  5 weeks stale** (`pick_inputs` stopped at 2026-07-16) — anything audited
  against an unpulled local DB before today was auditing July.
- **Window:** `2026-06-03 → 2026-08-20` for live behaviour (the A1 weights
  landed in production on **2026-06-03**, not 06-02 — see "Checked and clean").
  Coverage is additionally sliced Jun / Jul / Aug.
- **Excluded:** `2026-07-13/14/15` (B33 dead days — `mode='offline_simulation'`
  on 7/13 + 7/15, synthetic `game_pk`s on 7/14). `2026-07-16` is a real 1-game
  All-Star-break day, not a dead day.
- **Environment:** Python 3.14 / Windows / PowerShell, worktree
  `model-data-accuracy-audit-6e65e3`. All DB access via explicit
  `--db` / read-only URI so the worktree `.parent`-math trap never fired.
- **Respected as known-intentional:** everything on the CLAUDE.md false-alarms
  list, plus B33's dead days. One false-alarm entry is itself now stale — see
  P3-13.

---

## Top 3 findings

### 1 — 8.3% of published picks were structurally incapable of hitting a HR (P0)

**49 of 590** picks in the window either did not start the game (34) or were in
a game that was postponed that date (15). Monthly: Jun 19/219 (8.7%),
Jul 18/215 (8.4%), **Aug 12/156 (7.7%)**.

Root cause for the non-starter half: **512 of 590 picks (86.8%) carry
`lineup_source = 'recent:<date>'`** — i.e. *yesterday's* batting order — and
only 78 (13.2%) a genuinely `posted` lineup. Every single non-starter came from
a `recent:` row; **0 of the 78 `posted` picks failed** (100% started in the
stored slot). The `daily-picks` workflow fires at `cron: "7 13 * * *"`
= 13:07 UTC = **09:07 ET** ([.github/workflows/daily-picks.yml:55](.github/workflows/daily-picks.yml:55)),
hours before MLB lineups post — so the 14-day recent-lineup fallback that
`How_The_HR_Model_Works.md` describes as an occasional safety net is in fact the
normal path.

Consequence: the doc's selection rule 3 ("must be a confirmed starter,
`batting_order` int 1-9", enforced at
[generate_picks.py:2264](generate_picks.py:2264)) **does not do what it says**.
A stale 1-9 order satisfies the integer test, so a player who is not in today's
lineup at all passes the gate.

```bash
python -c "import sqlite3;c=sqlite3.connect('file:C:/dev/Claude/Projects/data/hr_bets.db?mode=ro',uri=True);print(c.execute(\"select case when lineup_source like 'recent:%' then 'recent' else coalesce(lineup_source,'NULL') end s, count(*) from pick_inputs pi join daily_picks dp on dp.date=pi.date and dp.batter_id=pi.batter_id and dp.selected=1 where pi.date>='2026-06-03' group by 1\").fetchall())"
```

Verified against MLB Stats API boxscores (`battingOrder`: `N00` = starter in
slot N, `N01+` = substitute) for all 434 distinct pick-games in the window.

### 2 — Power (weight 0.48, the heaviest factor) scores on synthetic contact quality, unclamped (P0)

`barrel_pct_source` is `synthetic_hr_per_pa` or `season_batting_fallback` on
**100%** of live rows — never Statcast. `barrel_pct`, `exit_velo` and
`hr_fb_pct` are algebraic transforms of `hr_per_pa` / `slg`
(`barrel ≈ hr_per_pa × 200`, `ev ≈ 82 + slg × 15`, `hr_fb ≈ hr_per_pa × 180`),
so three of the five live power inputs are re-encodings of the same season HR
rate — which the season-HR floor then encodes a fourth time.

Measured against Baseball Savant 2026 (`leaderboard/statcast`, `brl_percent` /
`avg_hit_speed`), all 156 August published picks:

| input | n | mismatch (>1 unit) | mean Δ | mean abs Δ | max abs Δ | as % of the score anchor span |
|---|---:|---:|---:|---:|---:|---|
| `barrel_pct` | 151 | **118 (78%)** | −1.96 pts | 3.02 pts | 18.10 pts | **37.8%** of the 3→11 span |
| `exit_velo` | 151 | **101 (67%)** | −1.48 mph | 2.15 mph | 13.30 mph | **21.5%** of the 85→95 span |

There is also **no plausibility clamp**: `exit_velo` reaches **127.0 mph** on a
published pick (2026-08-16, `season_batting_fallback`, `iso = 2.25`), and **9 published picks in the window carry `exit_velo > 96`, five of them
above 100** — physically impossible as a season *average*. All nine are
`season_batting_fallback` rows for batters with **3 to 17 season ABs to date**,
whose tiny-sample `slg` blows up the `82 + slg × 15` formula, and **all nine
scored `power_score = 100.0`**. (This is the mechanism behind the
already-scoped min-AB / P=100 problem, and B1 predicted the synthetic-input
half. What is new here is the measurement against Savant and the total absence
of a range guard.)

### 3 — The B14 weather premise is wrong: weather is ~78% real, not ~100% defaulted (P2, downgrade)

The brief assumed the Open-Meteo failure since 2026-05-12 meant weather has been
constant for three months. It has not. **Jul+Aug outdoor `pick_inputs` rows:
8,083 `open_meteo` (77.8%) vs 2,397 defaulted (22.2%)** — 1,994
`api_failed_default` + 403 `coords_missing_default`. The failure is per-venue
and partial, never a whole slate: worst single date is 44.6%, and 0 of 51 dates
exceed 50%. On published outdoor picks it is 17.3% (Jul) / 17.1% (Aug).

Nor is the fallback the `75°F / 5 mph` branch the brief pointed at
([generate_picks.py:922-927](generate_picks.py:922)) — that outer `except`
essentially never fires, because `get_weather` catches its own exceptions first
and returns `68°F / 5 mph / dir 0 / no humidity`
([fetch_daily_data.py:1122](fetch_daily_data.py:1122)).

**Selection impact of the weather factor as a whole**, recomputing the top-8
from stored factor scores over the 77 live days:

| counterfactual | pick-slots changed | days affected |
|---|---:|---:|
| weather **removed** (weights renormalised ÷0.92) | 36 / 610 = **5.9%** | 33 / 77 |
| weather **forced neutral 50** | 37 / 610 = **6.1%** | 34 / 77 |

So weather moves roughly half a pick per day, and the 22% defaulted share is a
fraction of that. **B14 stays open but is not a final-stretch blocker.**

---

## (a) Coverage — re-run of B31, sliced by month

`python -m diagnostics.data_integrity_audit --db C:\dev\Claude\Projects\data\hr_bets.db`
(recent-live window resolved to 2026-08-07 → 2026-08-20; era counts
`2025_backfill`=55,638 / `2026_pre_recent`=35,209 / `2026_recent_14d`=4,192).

**Headline: no column regressed.** Every column that was HEALTHY on 2026-06-03
is HEALTHY or better in August. The largest negative delta vs the 6/03
`2026_recent_14d` baseline is −4.3 pts (`slate_pitcher_vulnerability_pct`) and
the largest positive is +25.9 (`slate_weather_pct`). Nothing crosses a −5 pt
regression flag. Monthly slicing shows no drift either — July dips 3–6 pts on
several columns (`xwoba_contact`, `vegas_*`, `weather_source`, `lineup_source`)
purely because the 7/13–15 dead days are inside that month; excluding them,
July tracks June and August.

**Expected-0% columns confirmed still 0% in all three months** (B32a/b never
ran, as expected): `recent_barrel_real_21d`, `recent_xwoba_contact_21d`,
`recent_iso_21d`, `recent_barrel_real_28d`, `recent_xwoba_contact_28d`,
`recent_iso_28d`, `fb_slg`, `fb_pa`, `br_slg`, `br_pa`, `os_slg`, `os_pa`,
`form_archetype_*`, `park_archetype_*`, plus `ev_trend` and `pull_fb_pct`
(intentional). `batter_form_archetype` and `batter_pitch_type_splits` are still
empty; `batter_park_archetype` now has 63,432 rows but only **653 (1.0%)**
non-NULL centroids — the venue-lookup starvation is unchanged, just accumulating
more empty rows.

Support tables are current: `season_batting` 2026 last refreshed
2026-08-20 08:52, `pitcher_arsenals` 2026 last 2026-08-20 08:49.
`career_batting` is still frozen at 2026-05-02 with `career_barrel_pct` 0%
(LIVE_GAP, flag-gated, unchanged).

| column | class | 6/03 baseline | Jun | Jul | Aug | Δ Aug−base | picks-only Aug |
|---|---|---:|---:|---:|---:|---:|---:|
| `barrel_pct` | HEALTHY | 92.7% | 95.0% | 97.9% | 98.3% | +5.6 | 100.0% |
| `exit_velo` | HEALTHY | 100.0% | 100.0% | 100.0% | 100.0% | +0.0 | 100.0% |
| `hr_fb_pct` | HEALTHY | 92.7% | 95.0% | 97.9% | 98.3% | +5.6 | 100.0% |
| `iso` | HEALTHY | 97.7% | 98.6% | 99.5% | 99.5% | +1.8 | 100.0% |
| `xwoba_contact` | INTENTIONAL | 99.4% | 100.0% | 95.1% | 100.0% | +0.6 | 100.0% |
| `pull_fb_pct` | INTENTIONAL | 0.0% | 0.0% | 0.0% | 0.0% | +0.0 | 0.0% |
| `recent_hr_14d` | INTENTIONAL | 0.0% | 0.0% | 0.0% | 0.0% | +0.0 | 0.0% |
| `recent_barrel_pct_14d` | INTENTIONAL | 0.0% | 0.0% | 0.0% | 0.0% | +0.0 | 0.0% |
| `ev_trend_14d` | INTENTIONAL | 0.0% | 0.0% | 0.0% | 0.0% | +0.0 | 0.0% |
| `pitcher_hr_per_9` | HEALTHY | 100.0% | 100.0% | 100.0% | 100.0% | +0.0 | 100.0% |
| `pitcher_era` | HEALTHY | 100.0% | 100.0% | 100.0% | 100.0% | +0.0 | 100.0% |
| `pitcher_hh_pct` | HEALTHY | 100.0% | 100.0% | 100.0% | 100.0% | +0.0 | 100.0% |
| `pitcher_k_per_9` | HEALTHY | 100.0% | 100.0% | 100.0% | 100.0% | +0.0 | 100.0% |
| `pitcher_fb_pct_allowed` | HEALTHY | 99.5% | 99.6% | 94.2% | 99.8% | +0.3 | 100.0% |
| `woba_vs_hand` | HEALTHY | 99.5% | 99.8% | 100.0% | 99.9% | +0.4 | 100.0% |
| `archetype_similarity` | INTENTIONAL | 67.4% | 65.8% | 62.5% | 65.6% | -1.8 | 91.7% |
| `vegas_team_total_pct` | INTENTIONAL | 99.4% | 100.0% | 94.9% | 100.0% | +0.6 | 100.0% |
| `platoon_advantage` | HEALTHY | 100.0% | 100.0% | 100.0% | 100.0% | +0.0 | 100.0% |
| `hr_park_factor` | HEALTHY | 97.0% | 96.7% | 97.4% | 97.0% | +0.0 | 96.2% |
| `temperature_f` | HEALTHY | 100.0% | 100.0% | 100.0% | 100.0% | +0.0 | 100.0% |
| `wind_mph` | HEALTHY | 100.0% | 100.0% | 100.0% | 100.0% | +0.0 | 100.0% |
| `wind_direction_deg` | HEALTHY | 100.0% | 100.0% | 100.0% | 100.0% | +0.0 | 100.0% |
| `humidity_pct` | LIVE_GAP | 57.3% | 53.8% | 59.7% | 56.0% | -1.3 | 65.4% |
| `is_dome` | HEALTHY | 100.0% | 100.0% | 100.0% | 100.0% | +0.0 | 100.0% |
| `batting_order` | INTENTIONAL | 76.6% | 77.7% | 79.4% | 77.3% | +0.7 | 100.0% |
| `fetched_at` | METADATA | 100.0% | 100.0% | 100.0% | 100.0% | +0.0 | 100.0% |
| `source` | METADATA | 100.0% | 100.0% | 100.0% | 100.0% | +0.0 | 100.0% |
| `bats` | HEALTHY | 100.0% | 100.0% | 100.0% | 100.0% | +0.0 | 100.0% |
| `throws` | HEALTHY | 100.0% | 100.0% | 100.0% | 100.0% | +0.0 | 100.0% |
| `weather_source` | METADATA | 100.0% | 100.0% | 95.1% | 100.0% | +0.0 | 100.0% |
| `barrel_pct_source` | METADATA | 92.7% | 95.0% | 93.0% | 98.3% | +5.6 | 100.0% |
| `vegas_team_total_raw` | INTENTIONAL | 99.4% | 100.0% | 94.9% | 100.0% | +0.6 | 100.0% |
| `lineup_source` | METADATA | 100.0% | 100.0% | 95.1% | 100.0% | +0.0 | 100.0% |
| `pitcher_recent_hr9_21d` | HEALTHY | 96.1% | 93.5% | 89.2% | 92.9% | -3.2 | 97.4% |
| `pitcher_recent_starts_21d` | HEALTHY | 97.1% | 95.6% | 90.9% | 94.4% | -2.7 | 98.1% |
| `recent_hr_10g` | HEALTHY | 99.4% | 100.0% | 100.0% | 100.0% | +0.6 | 100.0% |
| `recent_iso_30g` | HEALTHY | 99.4% | 100.0% | 100.0% | 100.0% | +0.6 | 100.0% |
| `recent_avg_30g` | HEALTHY | 99.4% | 100.0% | 100.0% | 100.0% | +0.6 | 100.0% |
| `recent_window_days` | HEALTHY | 99.0% | 99.7% | 99.9% | 99.9% | +0.9 | 99.4% |
| `ev_trend` | INTENTIONAL | 0.0% | 0.0% | 0.0% | 0.0% | +0.0 | 0.0% |
| `pitcher_recent_era_21d` | HEALTHY | 87.5% | 93.5% | 89.2% | 92.9% | +5.4 | 97.4% |
| `pitcher_recent_k9_21d` | HEALTHY | 87.5% | 93.5% | 89.2% | 92.9% | +5.4 | 97.4% |
| `season_hr` | HEALTHY | 90.7% | 100.0% | 100.0% | 100.0% | +9.3 | 100.0% |
| `recent_barrel_real_14d` | HEALTHY | 81.7% | 88.9% | 84.5% | 89.3% | +7.6 | 96.8% |
| `recent_xwoba_contact_14d` | HEALTHY | 81.7% | 88.9% | 84.5% | 89.3% | +7.6 | 96.8% |
| `recent_iso_14d` | HEALTHY | 81.7% | 88.9% | 84.5% | 89.3% | +7.6 | 96.8% |
| `recent_barrel_real_21d` | BROKEN_BACKFILL | 0.0% | 0.0% | 0.0% | 0.0% | +0.0 | 0.0% |
| `recent_xwoba_contact_21d` | BROKEN_BACKFILL | 0.0% | 0.0% | 0.0% | 0.0% | +0.0 | 0.0% |
| `recent_iso_21d` | BROKEN_BACKFILL | 0.0% | 0.0% | 0.0% | 0.0% | +0.0 | 0.0% |
| `recent_barrel_real_28d` | BROKEN_BACKFILL | 0.0% | 0.0% | 0.0% | 0.0% | +0.0 | 0.0% |
| `recent_xwoba_contact_28d` | BROKEN_BACKFILL | 0.0% | 0.0% | 0.0% | 0.0% | +0.0 | 0.0% |
| `recent_iso_28d` | BROKEN_BACKFILL | 0.0% | 0.0% | 0.0% | 0.0% | +0.0 | 0.0% |
| `fb_slg` | BROKEN_BACKFILL | 0.0% | 0.0% | 0.0% | 0.0% | +0.0 | 0.0% |
| `fb_pa` | BROKEN_BACKFILL | 0.0% | 0.0% | 0.0% | 0.0% | +0.0 | 0.0% |
| `br_slg` | BROKEN_BACKFILL | 0.0% | 0.0% | 0.0% | 0.0% | +0.0 | 0.0% |
| `br_pa` | BROKEN_BACKFILL | 0.0% | 0.0% | 0.0% | 0.0% | +0.0 | 0.0% |
| `os_slg` | BROKEN_BACKFILL | 0.0% | 0.0% | 0.0% | 0.0% | +0.0 | 0.0% |
| `os_pa` | BROKEN_BACKFILL | 0.0% | 0.0% | 0.0% | 0.0% | +0.0 | 0.0% |
| `form_archetype_centroid_json` | BROKEN_BACKFILL | 0.0% | 0.0% | 0.0% | 0.0% | +0.0 | 0.0% |
| `form_archetype_window` | BROKEN_BACKFILL | 0.0% | 0.0% | 0.0% | 0.0% | +0.0 | 0.0% |
| `form_archetype_n_hrs` | BROKEN_BACKFILL | 0.0% | 0.0% | 0.0% | 0.0% | +0.0 | 0.0% |
| `park_archetype_centroid_json` | BROKEN_BACKFILL | 0.0% | 0.0% | 0.0% | 0.0% | +0.0 | 0.0% |
| `park_archetype_n_hrs` | BROKEN_BACKFILL | 0.0% | 0.0% | 0.0% | 0.0% | +0.0 | 0.0% |
| `slate_park_pct` | HEALTHY | 97.0% | 96.7% | 92.6% | 97.0% | +0.0 | 96.2% |
| `slate_weather_pct` | LIVE_GAP | 57.3% | 80.8% | 79.0% | 83.2% | +25.9 | 86.5% |
| `slate_pitcher_vulnerability_pct` | HEALTHY | 100.0% | 96.5% | 91.6% | 95.7% | -4.3 | 98.1% |

_Board rows per month: Jun 9,010 / Jul 8,672 / Aug 5,947. Published picks per month: Jun 235 / Jul 239 / Aug 156 (the Jul/Aug totals include the excluded dead days and the 1-game 7/16 slate; the audited pick set after exclusions is 590)._

---

## (b) Data-accuracy spot-check — 30 published picks (10 each Jun / Jul / Aug)

Sample drawn deterministically (`random.Random(20260821).sample`) from published
picks in `2026-06-03 → 2026-08-20`, dead days excluded. Sources of truth: MLB
Stats API (`/schedule?hydrate=lineups`, `/game/{pk}/boxscore`,
`/people/{id}/stats`), Baseball Savant `leaderboard/statcast`, and
`etl.park_factors_seed.get_seed_dataframe()`.

| input | source of truth | n | mismatches | rate | notes |
|---|---|---:|---:|---:|---|
| `hr_park_factor` | `park_factors_seed` `hr_pf_overall` | 28 | **0** | **0.0%** | exact to 0.05 on all 28. The other 2 picks are at venues absent from the seed table (below). |
| `vegas_team_total_raw` | sanity band 2.5–7.5 | 30 | **0** | **0.0%** | window range 3.07–7.65; 18 of 22,639 rows season-wide exceed 7.5 (max 7.65, Coors) — not a defect. |
| `pitcher_hr_per_9` | MLB Stats API season-to-date | 29 | **0** | **0.0%** | 28 agreed to ≤0.02 (the residual is the as-of-date boundary). The 29th (Dean Kremer, 8/16, stored 2.25 vs API 3.00) was **the reference being wrong, not the model** — Kremer was traded, so `stats=season` returns per-team splits; his gameLog through 8/15 is 12 HR / 48.0 IP = **2.25**, exactly what the model stored. 1 pick faced a `TBD` pitcher and has no id to check. |
| `batting_order` | boxscore `battingOrder` | 29 | **3** | **10.3%** | all 3 were `lineup_source='recent:'` rows (Langeliers 6/09 stored 1 / actual 3; Torkelson 7/20 stored 5 / actual 7; Caminero 7/04 stored 3 / actual 2). See the full-population number below. |
| `team` | MLB Stats API game teams | 30 | 1 | 3.3% | `OAK` vs the API's current `ATH` abbreviation — cosmetic, same family as C2. |
| `barrel_pct` | Savant `brl_percent` | 29 | **22** | **75.9%** | mean abs Δ 3.20 pts, max 18.50. |
| `exit_velo` | Savant `avg_hit_speed` | 29 | **24** | **82.8%** | mean abs Δ 2.52 mph, max 10.30. |

Barrel / exit-velo were re-run over the **full August pick population** (156
picks, 5 absent from Savant's ≥25-BBE leaderboard) to remove sampling noise —
those are the numbers in Top-Finding 2. Caveat: Savant is season-to-date *as of
2026-08-21*, so June picks compare against a longer window than the model saw.
That widens June deltas somewhat; it does not explain August's 78% / 67%, and it
cannot explain a 127-mph average exit velocity.

**Batting order over the full population** (all 590 audited picks, boxscore
truth — this is the number that matters, not the 30-row sample):

| outcome | n | share |
|---|---:|---:|
| started in the stored slot | 479 | 81.2% |
| started, different slot | 74 | 12.5% |
| entered as a substitute | 16 | 2.7% |
| did not appear at all | 21 | 3.6% |

Split by `lineup_source`: **`posted` (n=78) — 78/78 = 100.0% correct.**
`recent:` (n=512) — 78.3% correct, 14.5% wrong slot, 4.1% DNP, 3.1% sub.

---

## (c) Weather truth check

**Defaulted fraction (the number the brief asked for).** Counting only outdoor
rows (domes are an intentional flat 50 and are excluded), `weather_source` in
`pick_inputs`:

| window | outdoor rows | `open_meteo` | `api_failed_default` | `coords_missing_default` | **defaulted** |
|---|---:|---:|---:|---:|---:|
| Jun | 6,572 | 4,845 | 1,428 | 299 | **26.3%** |
| Jul | 6,482 | 4,755 | 1,172 | 225 | **21.6%** |
| Aug | 4,328 | 3,328 | 822 | 178 | **23.1%** |
| **Jul+Aug** | **10,810** | **8,083** | **1,994** | **403** | **22.2%** |

(July's 330 NULL-`weather_source` outdoor rows are the 7/13–15 dead days.)
On published outdoor picks: Jun 30.3%, Jul 17.3%, Aug 17.1%.

Per-date the failure is partial, never total — distribution of the per-date
outdoor default rate across the 81 days Jun–Aug: 12 days at 0–9%, 18 at 10–19%,
28 at 20–29%, 17 at 30–39%, 5 at 40–49%, 1 at 50–59%. **Zero days ≥60%.**

**What a defaulted row actually scores.** `compute_slate_context` requires
temp + wind + humidity and the fallback carries no `humidity_pct`
([score_batters.py:221-224](score_batters.py:221)), so defaulted games are
excluded from `weather_pct` and fall through to the fixed-anchor branch
([score_batters.py:1430-1437](score_batters.py:1430)):
`temp_score(68)=50 × 0.45 + wind_score × 0.35 + humidity_score(None)=50 × 0.20`.
Empirically that lands at **mean 48.5 (range 45.3–52.9)** — near-neutral, not a
distorting constant. The cross-check holds exactly: `slate_weather_pct` is
non-NULL on 12,677/12,677 `open_meteo` rows and 5,950/5,950 dome rows, and NULL
on 100% of the 4,030 defaulted rows.

**Selection impact** (recomputed top-8 from stored factor scores, 77 live days,
610 pick-slots): weather removed → **36 slots change (5.9%)**, on 33/77 days
(30 days lose 1 slot, 3 days lose 2). Weather forced to a flat 50 → 37 slots
(6.1%). So the *entire* weather factor is worth ~0.47 pick-slots/day, and the
22% defaulted share is a minority of that.

**Verdict:** B14 is real but mis-scaled in the brief's framing. It is worth
fixing for signal hygiene, not as a final-stretch blocker. What *is* worth
fixing before a refit is P2-6 below (the dome contamination of the percentile),
which affects 100% of outdoor rows, not 22%.

---

## (d) Logic + data findings, ranked

### P0-1 — The "confirmed starter" gate is satisfied by yesterday's lineup

**What.** 512/590 picks (86.8%) scored on `lineup_source='recent:<date>'`.
34 picks did not start (21 DNP, 16 sub — 3 of those also in postponed games);
74 more started in a different slot. All failures are `recent:` rows.

**Why it happens.** `.github/workflows/daily-picks.yml:55` runs at 13:07 UTC
(09:07 ET). MLB lineups are not posted then, so `fetch_daily_data`'s 14-day
recent-lineup fallback is the normal path, and
[generate_picks.py:2264](generate_picks.py:2264)'s
`isinstance(bo, int) and 1 <= bo <= 9` test cannot tell a stale order from a
confirmed one.

**Impact.** ~5.8% of the card is dead on arrival every day, plus 12.5% of picks
carry a wrong lineup slot into any lineup-position analysis (and into B15's
planned rebuild).

**Fix shapes.** (i) Move / add a run closer to first pitch — BACKLOG item #4
Option B already sketches this; (ii) gate the top-8 on
`lineup_source='posted'` and only fall back to `recent:` if fewer than 8
qualify; (iii) stamp a re-check between generation and publish. This is a
design call for the PM chat — I am not picking one.

**Files.** `.github/workflows/daily-picks.yml:55`,
[generate_picks.py:2264](generate_picks.py:2264), `fetch_daily_data.py`
(lineup fallback).

### P0-2 — Power's contact-quality inputs are synthetic and unclamped

**What.** `barrel_pct_source` is never `statcast`; barrel / EV / HR-FB are
transforms of `hr_per_pa` and `slg`. Against Savant, August picks miss by a mean
3.02 barrel pts (37.8% of the 3-to-11 anchor span) and 2.15 mph (21.5% of the
85-to-95 span). No range guard exists: `exit_velo` reaches 127.0 mph, and
`barrel_pct` is bounded only by whatever the source produced (season-wide max
25.0). Nine published picks in the window carry `exit_velo > 96`, five of them
above 100; all nine are `season_batting_fallback` rows for batters with 3-17
season ABs, and **all nine scored `power_score = 100.0`**.

**Impact.** The 0.48-weight factor is largely a re-encoding of season HR rate,
and small-sample batters get handed the top of the scale.

**Fix shapes.** (i) A cheap plausibility clamp + warn in
[score_batters.py:686](score_batters.py:686) or at the ETL write (`exit_velo`
to roughly [80, 96], `barrel_pct` to [0, 25]) — small, independent, and it
would have caught every one of these; (ii) the real fix is a Savant bulk pull
into `season_batting` alongside the synthetic values (B1 / B6 territory).

**Files.** [score_batters.py:679-706](score_batters.py:679),
`etl/etl_nightly.py::sync_season_batting`,
`generate_picks.py::enrich_with_season_batting`.

**Note.** This overlaps the already-scoped min-AB / P=100 item. The clamp is
the part a min-AB filter would *not* cover — a full-season hitter with a parse
error would still slip through.

### P1-3 — `daily_picks.opp_pitcher_id` is 0 on 100% of rows since 2026-05

**What.** 35,057 of 35,057 rows from 2026-05 onward have `opp_pitcher_id = 0`.
`daily_slate` carries the real ids (only ~4% missing), so the data exists.

**Why.** `compute_composite`'s result dict never carries the id; `generate_picks`
sets only the name ([generate_picks.py:1686](generate_picks.py:1686),
[:1968](generate_picks.py:1968), [:2089](generate_picks.py:2089)), so the JSON
writer's `p.get("opp_pitcher_id", 0)`
([generate_picks.py:2523](generate_picks.py:2523),
[:2562](generate_picks.py:2562)) always writes 0.

**Impact.** Every pitcher-side join off `daily_picks` has to fall back to name
matching. Name matching is exactly what breaks on traded or duplicate-name
pitchers — the Dean Kremer case in section (b) is a live example of why ids
matter.

**Fix.** One line per call site: set `result["opp_pitcher_id"]` from the id
already in scope.

### P1-4 — The postponed-game filter only catches games already postponed at 09:07 ET

**What.** 15 published picks across 11 dates were in games postponed later that
day, including **2 on 2026-08-20** (`game_pk` 824589, ATL @ CWS — postponed and
replayed the same date). The filter itself is correct and present
([generate_picks.py:881-884](generate_picks.py:881)); it just runs before the
rain does.

**Impact.** ~2.5% of picks graded 0-for against games that never happened on
the published date. Distinct from B33, which covers whole no-game days.

**Fix shape.** A pre-publish or post-hoc status re-check, or fold it into the
same "run closer to first pitch" change as P0-1, which shrinks both failures at
once.

### P2-5 — `pick_inputs`'s key cannot represent a doubleheader

**What.** `pick_inputs` is keyed `(date, batter_id)` (see `CREATE TABLE
pick_inputs` in [etl/db.py](etl/db.py)), but `daily_picks` correctly carries two
rows for a batter who plays a doubleheader. On 2026-06-24, 07-07 and 07-11
(15 batters), the single `pick_inputs` row holds the inputs of only one of the
two games — so joining `daily_picks` to `pick_inputs` on `(date, batter_id)`
silently attributes the wrong weather, park, pitcher and Vegas inputs to the
other game.

**Evidence.** 6/24 `game_pk` 823613 (Citi Field): five batters show
`open_meteo` 81.8 F / 35% humidity and two show `api_failed_default`
68 F / NULL humidity — impossible within one game, and exactly what a
cross-game overwrite looks like.

**Impact.** Scoring is unaffected (each board row was scored with its own
game's data). Persistence, `backtest_factors`, `refit_weights`, the dashboard
decomposition — and this audit — are all affected. Small today (15 rows) but it
recurs every doubleheader and silently corrupts refit training rows.

**Fix.** Add `game_pk` to the `pick_inputs` key and to every join against it.

### P2-6 — Dome games contaminate the within-slate weather percentile

**What.** [score_batters.py:216](score_batters.py:216) assigns dome games a raw
weather quality of `50.0`, while outdoor games get
`temp + wind * 0.5 + humidity * 0.05`
([score_batters.py:225](score_batters.py:225)) — which on a summer slate is
~85-100. The two are not on the same scale, so domes always occupy the bottom
ranks of `weather_pct` and compress every outdoor game into the top
`(100 - dome_share)%` of the range. `score_weather` short-circuits domes to a
flat 50 **before** ever reading `weather_pct`
([score_batters.py:1408](score_batters.py:1408)), so the dome entries
contribute nothing but distortion.

**Evidence.** The outdoor `slate_weather_pct` floor tracks dome share exactly:
2026-08-17 (10.9% domes) floor 15.0, spread 80.0; 2026-08-20 (43.8% domes)
floor 50.0, spread 44.4. Across the window, days with dome share >=25% average a
54.4-point outdoor spread vs 26.7 on days below 15%.

**Impact.** The weather factor's effective dynamic range moves day to day with
the dome share (observed 11%-44%). Within a day it dampens weather's
discrimination; across days it puts the A1 refit's weather coefficient on a
moving scale. Bounded by the 0.08 weight.

**Fix.** Exclude domes from `game_weather_q` entirely (they never read it), or
put them on the same units as outdoor games.

### P2-7 — `How_The_HR_Model_Works.md` is materially wrong in seven places

Last updated 2026-05-06; A1 (06-02), B11 (05-26) and B17 (05-27) all landed
after. Anyone reading it to reason about the model is reading a different model.

| doc says | code does | ref |
|---|---|---|
| Power .250 / Matchup .264 / Park .000 / Form .279 / Weather .057 / Lineup .150 | **.48 / .28 / .04 / .12 / .08 / .00** | [score_batters.py:41](score_batters.py:41) |
| `composite += 0.05 x park` additive bonus | removed in A1; park is a weighted 0.04 factor | [score_batters.py:1532](score_batters.py:1532) |
| barrel 5-15, HR/FB 8-20, ISO .130-.300, xwOBA .330-.450 | **3-11, 3-10, .100-.250, .260-.390** (B17) | [score_batters.py:683](score_batters.py:683) to [:701](score_batters.py:701) |
| Form has four inputs including `recent_avg_30g` | three; AVG dropped by B11 | [score_batters.py:1216](score_batters.py:1216) |
| v1 matchup is "two-thirds vulnerability, one-third the others"; "No platoon bonus" | equal-weight mean of available signals **plus a flat +10 platoon bonus** | [score_batters.py:920](score_batters.py:920), [:934](score_batters.py:934) |
| Selection has five rules | plus a dedupe-by-name and the B7 IL filter | [generate_picks.py:2257](generate_picks.py:2257), [:2270](generate_picks.py:2270) |
| "noon scoring runs" | 09:07 ET | [.github/workflows/daily-picks.yml:55](.github/workflows/daily-picks.yml:55) |

Also stale: the comment at [score_batters.py:495](score_batters.py:495) still
says the season-HR floor "propagates forward at the standard 0.25 weight" (it
is 0.48). B23 already covers the `woba_vs_hand` anchor drift specifically; this
is the broader sweep. CLAUDE.md's own "noon pipeline" line has the same 09:07
problem.

### P2-8 — B29 confirmed still open, with the root cause located

`daily_picks.lineup_score` is NULL on **all 35,057** rows from 2026-05 onward.
Root cause: the `full_board` serialiser
([generate_picks.py:2566-2572](generate_picks.py:2566)) omits `lineup_score`,
while the 8-pick `picks` list at [:2530](generate_picks.py:2530) includes it;
`load_picks_to_db` loads from `full_board`
([load_picks_to_db.py:63](load_picks_to_db.py:63)), so
`row.get("lineup_score")` at [:246](load_picks_to_db.py:246) is always None.
Zero scoring impact (weight 0.00), but B15's lineup-table rebuild needs this
column populated before it can be validated.

### P3-9 — Three venues fall through every geographic lookup, silently

`Sutter Health Park` (567 board rows, 31 picks), `Las Vegas Ballpark`
(113 / 10) and `Field of Dreams` (22 / 0) are missing from
`etl.park_factors_seed`, so `hr_park_factor` is NULL and `score_park` returns a
flat 50 via `min_max_scale(100, 70, 130)`
([score_batters.py:1063](score_batters.py:1063)). They are also missing from
`VENUE_COORDS` — which is the entire source of the 403
`coords_missing_default` weather rows — and `Las Vegas Ballpark` and
`Field of Dreams` are missing from `PARK_CF_BEARING`, so `score_wind` computes
alignment against a fabricated due-north CF bearing
([score_batters.py:1347](score_batters.py:1347)). Every one of these degrades
silently. Low impact (park 0.04, weather 0.08), but it is three separate tables
needing the same three rows.

### P3-10 — `daily_picks.weight_config` is always the literal `'default'`

[load_picks_to_db.py:64](load_picks_to_db.py:64) reads
`data.get("scoring_config", "default")`, but `generate_picks` never writes a
`scoring_config` key into the picks JSON. A run with `--config power_heavy`
would still be recorded as `default`. 100% of rows say `default` — which
happens to be true, but the field proves nothing.

### P3-11 — The v1 matchup path (14.9% of picks) diverges from the doc and from v2

91 of 610 picks and 8,573 of 23,482 board rows used `matchup_version='v1'`.
That path equal-weights whatever signals are present and adds a flat +10
platoon bonus ([score_batters.py:920](score_batters.py:920)), where v2 folds
handedness into archetype similarity and adds nothing
([pitcher_profile.py:930](pitcher_profile.py:930)). v1 and v2 rows are
therefore not on the same scale, and the doc describes neither accurately.
A4 ("matchup v1 consolidation") already exists; this quantifies it.

### P3-12 — The B7 IL filter is effectively inert

`is_likely_out` fired on 28 board rows in 79 days (0.12%), and
`promoted_due_to` is NULL on every row in the window — the filter has never
promoted anyone into the card. Meanwhile 21 picks did not appear at all. The
filter catches roster IL status; it does not catch "the manager rested him",
which is what actually happens. Not a bug, but it should not be counted as
coverage against P0-1.

### P3-13 — CLAUDE.md's false-alarm list is itself stale

The list still names the `hr_fb_pct` anchor `(8, 20)` as a "known anchor
mis-cal". B17 shipped 2026-05-27 and the live anchor is `(3, 10)`
([score_batters.py:693](score_batters.py:693)). Worth striking so a future
session does not go hunting a bug that is already fixed. Every other
false-alarm entry I touched in passing was confirmed still accurate.

---

## (e) Checked and clean — so silence is not ambiguous

Everything below was actively tested and passed. Where a number is given, it is
the measured result, not an assertion.

**Composite arithmetic**

- `composite = weighted_mean(power, matchup, park, form, weather, lineup) x
  platoon_dampener` reproduces from the stored per-factor scores for
  **23,599 of 24,037** live board rows from 2026-06-03 onward. Every one of the
  438 exceptions is explained: 407 are 2026-06-01/06-02 rows (still on the
  pre-A1 weights + the `+0.05 x park` additive bonus — **the A1 weights went
  live on 06-03, not 06-02**), and the remaining 31 are low-composite rows
  where 0.05 rounding on a composite near 10 exceeds the 0.15% tolerance.
- Live weights match the brief exactly: `.48 / .28 / .04 / .12 / .08 / .00`
  ([score_batters.py:41](score_batters.py:41)).
- The `+0.05 x park` additive bonus is gone, and park is a weighted 0.04 factor
  — no double-count ([score_batters.py:1532](score_batters.py:1532)).
- `_platoon_dampener` behaves as documented: floor 0.90, 1.0 for `games=None`
  or missing slate context ([score_batters.py:440](score_batters.py:440)).

**Scorers vs design**

- Power anchors are the post-B17 values (barrel 3-11, EV 85-95, HR/FB 3-10,
  ISO .100-.250, xwOBA .260-.390, pull-FB 8-22), with `is not None and > 0`
  skip-on-missing on every input ([score_batters.py:679-706](score_batters.py:679)).
- Season-HR floor tiers are the documented 5/8/12/18/25 -> 50/60/70/78/85,
  elevate-only, smooth variant off
  ([score_batters.py:575](score_batters.py:575), [:616](score_batters.py:616)).
- Form = `recent_hr_10g` + `recent_iso_30g` + `ev_trend`, equal weight,
  skip-on-missing, `recent_avg_30g` correctly dropped per B11, then the
  layoff dampener ([score_batters.py:1216-1246](score_batters.py:1216)).
  `ev_trend` is 0% populated **by design** (A2 not built).
- Matchup v2 is exactly the documented four-signal mean (vulnerability,
  similarity, Vegas total pct, wOBA-vs-hand) with variable arity, then the ace
  dampener at `vuln < 25 -> x0.70` / `vuln < 40 -> x0.85`
  ([pitcher_profile.py:966-986](pitcher_profile.py:966)). No platoon bonus on
  the v2 path.
- Rookie-pitcher bonus is +15, capped at 100
  ([score_batters.py:466](score_batters.py:466), [:934](score_batters.py:934)).
- All optional sub-signal flags are OFF as expected: `USE_RECENT_STATCAST_BLEND`,
  `USE_PARK_ARCHETYPE`, `USE_ARSENAL_SUBSIGNAL`, `USE_FORM_ARCHETYPE`,
  `USE_SMOOTH_HR_FLOOR`, `USE_CAREER_PRIOR` all `False`; `USE_SEASON_HR_FLOOR`
  `True`.

**Selection integrity**

- **Top-8 recomputed from stored composites matches the published card exactly
  — membership *and* order — on 77 of 77 live days** (2026-06-03 to 2026-08-20).
  Replicating the real algorithm (sort by composite desc, dedupe by name,
  require `batting_order` int 1-9, skip `is_likely_out`, cap 2 per `game_pk`,
  stop at 8) reproduces the published picks with zero discrepancies.
- **Max 2 per game: 0 violations** across 434 pick-games. Also checked the
  doubleheader edge case — no day put more than 2 picks in a single `game_pk`,
  and no same-teams matchup exceeded 2 either.
- The postponed/cancelled/suspended filter is present and correct at scoring
  time ([generate_picks.py:881-884](generate_picks.py:881)), including the
  all-games-postponed early exit. Its limitation is timing, not logic (P1-4).
- Bench / `roster_only` / NULL `batting_order` rows never reach the card
  ([generate_picks.py:2264](generate_picks.py:2264)) — 100% of published picks
  carry an integer 1-9.
- Only one day in the window produced fewer than 8 picks: **2026-07-16**, with
  2 picks. That is correct — the All-Star-break slate had exactly one game, and
  max-2-per-game caps it at 2. Not a defect.

**Input accuracy (measured against sources of truth)**

- `pitcher_hr_per_9`: **29/29 verified correct** against the MLB Stats API.
  28 agreed to within 0.02; the 29th (Kremer) was the *reference* that was
  wrong, and reconstructing his gameLog confirmed the model's stored 2.25.
- `hr_park_factor`: **28/28 exact** against `etl.park_factors_seed`
  `hr_pf_overall`. The other 2 sampled picks are at unmapped venues (P3-9).
- `vegas_team_total_raw`: **30/30 in a sane band**; season-wide 3.07-7.65 over
  22,639 rows, with only 18 rows above 7.5 (all Coors-class, max 7.65).
- `batting_order` for `lineup_source='posted'` picks: **78/78 exact** against
  the boxscore. The failures are entirely on the `recent:` path (P0-1).
- `is_dome` / `temperature_f` / `wind_mph` / `wind_direction_deg`: 100%
  populated on live rows in all three months.
- `weather_source` provenance is internally consistent: `slate_weather_pct` is
  non-NULL on 100% of `open_meteo` and dome rows and NULL on 100% of the 4,030
  defaulted rows — the skip-on-partial-data rule at
  [score_batters.py:221](score_batters.py:221) does exactly what it claims.

**Coverage**

- **No column regressed** vs the 2026-06-03 baseline in any of Jun / Jul / Aug.
- Every expected-0% column is still 0% (B32a/b never ran, as expected), and no
  expected-0% column has been accidentally partially populated.
- `season_batting` and `pitcher_arsenals` 2026 rows are current to 2026-08-20.

**False alarms re-confirmed, not re-filed**

`ev_trend` 100% NULL; `daily_lineup.batting_order > 9` residue;
`backtest_factors.rescore_row` missing 21d/28d + archetype columns;
`pitcher_fb_pct_allowed > 100`; `season_batting.team = '???'`; T4-untiered NULL
`barrel_pct_source`; `score_lineup_position` anti-correlation (B15); the
7/13-15 dead days (B33). All present, all still intentional or already filed.
The one exception is the `hr_fb_pct` anchor entry, which is now obsolete
(P3-13).

---

## Suggested fix order

| # | finding | size | independent? |
|---|---|---|---|
| 1 | P0-2 exit-velo / barrel plausibility clamp | small | yes |
| 2 | P1-3 `opp_pitcher_id` (one line x 3 call sites) | small | yes |
| 3 | P2-8 `lineup_score` in `full_board` (B29) | small | yes |
| 4 | P2-6 drop domes from `game_weather_q` | small | yes |
| 5 | P2-7 doc refresh + P3-13 CLAUDE.md strike | small | yes |
| 6 | P0-1 lineup timing — **needs a PM design call first** | medium/large | no |
| 7 | P1-4 postponement re-check | medium | folds into 6 |
| 8 | P2-5 `pick_inputs` key + joins | medium | yes |
| 9 | P3-9 / P3-10 / P3-11 / P3-12 | small each | yes |

Items 1-5 are one small PR each off `main` and would land without touching
scoring behaviour except item 4 (which changes `weather_pct` and therefore
composites — it should be backtested, not just shipped).

## How to reproduce

```bash
python infra/r2_sync.py pull
```

```bash
python -m diagnostics.data_integrity_audit --db C:\dev\Claude\Projects\data\hr_bets.db --md docs\data_integrity_audit_2026-08-21.md
```

Everything else in this doc came from read-only SQLite queries against
`C:\dev\Claude\Projects\data\hr_bets.db` plus live calls to
`statsapi.mlb.com` and `baseballsavant.mlb.com`; the query behind each number is
stated inline or is a one-line aggregate over `pick_inputs` / `daily_picks`
joined on `(date, batter_id)`.
