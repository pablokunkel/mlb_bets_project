# Scoring-pipeline audit — 2026-05-20

Read-only audit of the 6-factor composite. All file paths are relative to project root unless noted.

## 1. Factor inventory

End-to-end flow per factor. Line numbers in *score_batters.py* unless prefixed.

### Power (weight 0.250)
- **DB sources.** `season_batting.{barrel_pct, exit_velo, hr_fb_pct, iso, woba, hr, games, pa}` (synthetic — see §2); `pick_inputs.{xwoba_contact, pull_fb_pct}` (real Savant via `features_v2.fetch_batter_xwoba_bulk`). `pull_fb_pct` is NULL on the daily path (`generate_picks.py:1131-1133`).
- **ETL.** `etl/etl_nightly.sync_season_batting` writes `season_batting` from MLB Stats API splits (synthetic `barrel ≈ hr/pa × 200`, `ev ≈ 82 + slg × 15`, `hr_fb ≈ hr/pa × 180`). Live path also re-derives the same estimates per-noon via `fetch_daily_data._splits_to_batters` (`fetch_daily_data.py:613-655`).
- **Assembly.** Live tier: `generate_picks.py:1184` calls `enrich_with_season_batting`; line 1192-1208 builds `entry`. T4 untiered: `generate_picks.py:1324-1346` (NO `hr` key set — see §4). Offline sim: `generate_picks.py:1479-1493`.
- **Scoring.** `score_power` at line 533-610.

### Matchup (weight 0.264)
- **DB.** Pitcher slice from `season_pitching` (`hr_per_9, era, hard_hit_pct_allowed, k_per_9, fb_pct_allowed, ip`) + `pick_inputs.{pitcher_recent_hr9_21d, pitcher_recent_starts_21d}` (added PR #57). Batter side: `season_batting.woba` / `woba_vs_hand`. Vegas: `slate_ctx.team_total_pct/raw` via the-odds-api.
- **ETL.** `etl/etl_morning.py` calls `fetch_pitcher_stats_mlb` per starter, then `features_v2.fetch_pitcher_fb_bulk` (Savant CSV) plus `fetch_pitcher_recent_form_batch` (MLB API gameLog) — `generate_picks.py:857-904`.
- **Assembly + slate-rank.** `compute_slate_context` (`score_batters.py:149-322`) builds `pitcher_pct` from blended `effective_hr9` + era + hh + k9 + (fb_pct−35) — line 244-294.
- **Scoring.** `score_matchup` v1 line 613-699; `score_matchup_v2` `pitcher_profile.py:685-761`; vulnerability `pitcher_profile.py:608-678`.

### Park (weight 0.000; +0.05 additive)
- **DB.** `park_factors` table (`hr_pf_overall`, `hr_pf_lhb`, `hr_pf_rhb`). Hardcoded seed via `fetch_daily_data.get_hardcoded_park_factors` — never refreshed (B3 backlog).
- **ETL.** `etl/etl_nightly.sync_park_factors`.
- **Scoring.** `score_park` line 702-766; additive park bonus at `compute_composite` line 1107.
- Wind orientation lookup keyed off `PARK_CF_BEARING` (line 59-91) — default 0 when venue is missing.

### Form (weight 0.279)
- **DB sources.** `pick_inputs.{recent_hr_10g, recent_iso_30g, recent_avg_30g, recent_window_days, ev_trend}` (Form rebuild PR #56).
- **ETL.** None — the form pull is real-time from MLB API per-pick via `fetch_daily_data.get_recent_game_log` (`fetch_daily_data.py:1060-1152`); orchestrated by `generate_picks.fetch_form_data_batch` (line 494-509).
- **Scoring.** `score_form` line 785-822, with layoff dampener `_layoff_dampener` line 769-782. `ev_trend` is skip-on-missing (always None until A2 ships).

### Weather (weight 0.057)
- **DB.** `daily_slate.{temperature_f, wind_mph, wind_dir_deg, humidity_pct, dome}` populated by `fetch_daily_data.get_weather` (Open-Meteo). `_source` field stamped on the weather dict (line 851-855).
- **Scoring.** `score_weather` line 958-998; `score_temperature` line 855-897; `score_wind` line 906-944; `score_humidity` line 947-955. Composes 60% slate-rank + 40% wind alignment when `slate_ctx.weather_pct[gpk]` is set.

### Lineup (weight 0.150)
- **DB.** `daily_lineup` (batting_order 1-9 or fallback flags) from `fetch_daily_data.get_lineup` (`fetch_daily_data.py:475-529`).
- **Scoring.** `score_lineup_position` line 825-852. Mapping: 1→85 … 9→38, "bench"/"roster_only"→15, None→**35**.

## 2. Per-factor walkthrough

### Power
- **Anchor block.** `score_batters.py:545-560` — anchors `barrel 5-15 / EV 85-95 / HR/FB 8-20 / ISO 0.130-0.300 / xwOBA-contact 0.330-0.450 / pull-FB 8-22`, calibrated 2026-05-03 to real MLB distributions.
- **Floor.** `SEASON_HR_FLOOR_TIERS = [(5,50),(8,60),(12,70),(18,78),(25,85)]` (`score_batters.py:505-511`). Calibration comment line 497-503 ties tiers to specific players ("Drake Baldwin / Pete Alonso pace" etc.). Only ELEVATES.
- **What 50 means.** Composite mean of league-average inputs OR no inputs measured at all (`scores=[]` → 50.0 at line 596).
- **Hardcoded numerics:** anchor pairs above (calibration comment present); `SEASON_HR_FLOOR_TIERS`; `CAREER_PRIOR_K = 200` (line 350, comment present); `min(25, ...)` cap on synthetic barrel in `_splits_to_batters` line 623 — no rationale.

### Matchup
- **Anchors.** `effective_hr9` blend `RECENT_HR9_BLEND_WEIGHT=0.60, RECENT_HR9_MIN_STARTS=2` (`pitcher_profile.py:152-153`, comment present). HR/9 → 0-4.5 (`score_pitcher_vulnerability:654`). ERA 2-6, hh 25-50, k9 4-14 (inverted), fb_pct centered at 35 (lines 656-674). wOBA-vs-hand 0.290-0.395 (line 670; comment block line 660-665 records 2026-05-01 re-anchor decision).
- **Hardcoded numerics.** `ROOKIE_MATCHUP_BONUS = 15` line 452, calibration comment present. `LEAGUE_AVG_PITCHER` line 455-464 — comment notes "2026 real HR/9 closer to 1.27" but the constant is still `1.2`. Platoon bonus +10 v1 vs 0 v2 (A4 backlog).
- **What 50 means.** `score_matchup_v2` returns the mean of available signals; without inputs falls to 50. v1 returns 50 if all components missing.

### Park
- **Anchors.** Slate-percentile (no fixed anchors). Fallback uses 70-130 → 0-100 (`min_max_scale(pf, 70, 130)` line 766). Handedness skew adds `±50 * (lhb_pf − overall)/overall` (line 736-742).
- **Hardcoded.** `PARK_CF_BEARING` dict for 30 venues (line 59-91). Sutter Health Park bearing flagged "verify" in code (line 90). Comment at line 1098-1107 records 2026-05-03 +0.05 additive bonus rationale.
- **Note.** Park weight is 0.000 in `WEIGHT_CONFIGS["default"]` (line 37) — only the additive bonus contributes. Comment at line 32 documents "park -0.011 (drop)".

### Form
- **Anchors.** `recent_hr_10g 0-5` (line 805), `recent_iso_30g 0.100-0.300` (line 809), `recent_avg_30g 0.210-0.330` (line 813), `ev_trend -3.0..+3.0` (line 817). Comment block 786-799 documents rebuild PR #56 reasoning. Dampener kicks in at `window_days > 55`, ramping to 60% pull at 90d (line 779-781) — calibration comment line 771-778.
- **What 50 means.** No inputs measured → 50.0 (line 819-820).
- **Hardcoded.** Same set of anchor pairs; layoff thresholds 55/90; window args `hr_window=10, rate_window=30` defaults `fetch_daily_data.py:1061`.

### Weather
- **Temperature.** Anchor table 40→25, 50→35, 60→44, 68→50, 75→55, 85→63, 95→72, 100→78 (`score_batters.py:879-888`). Hand-tuned per historical_calibration (calibration comment block line 857-878).
- **Wind.** `if wind_mph < 2: return 50.0` (line 921). Linear speed_factor `min(1.0, wind_mph / 15)` (line 937, 942). Magnitude `±25` (line 938, 944). LHB target = CF + 45°; RHB = CF − 45° (line 927-933).
- **Humidity.** `35 + h × 0.30` linear (line 955). No calibration comment.
- **Blending.** When slate_ctx active: 60% base-pct + 40% wind alignment (line 988-989). Without slate_ctx: 0.45 temp + 0.35 wind + 0.20 humidity (line 998).
- **What 50 means.** Dome (line 975-976) or missing direction (line 921-922).

### Lineup
- **Anchors.** Hardcoded score table line 834-837 (`1: 85, 2: 82, … 9: 38`). Comment line 830-833 documents AB/G rationale.
- **Asymmetry (low confidence — verify).** None → 35.0 (line 840); "bench" / "roster_only" → 15.0 (line 843-844). The "no lineup data yet" case scores 20 points HIGHER than the same batter once we know they're benched. Used to score the same batter differently on the morning ETL (lineup_source="roster_fallback" → batting_order="roster_only" → 15) vs. the noon-run during which the field can be None.

## 3. Magic-string audit

Grouped by file.

### `score_batters.py`
- `"R"`, `"L"`, `"S"` handedness literals: lines 462, 683-684, 731-762, 910, 917, 927-933, 961, 1076. No central constant.
- `"league_avg"`, `"league_avg_default"`: lines 456, 463. Provenance tag.
- `"synthetic_hr_per_pa"`, `"season_batting_fallback"`, `"career_shrunk"`, `"estimate_from_hr_per_pa"`: provenance tags (line 1221, `generate_picks.py:381, 383, 350`, `fetch_daily_data.py:654, 760, 818`).
- Anchor venue names embedded in `PARK_CF_BEARING` (30 strings, line 59-91).
- `"default"`, `"v1_learned"`, `"legacy"`, `"matchup_heavy"`, `"power_heavy"`, `"form_heavy"`, `"no_weather"`: weight-config keys line 27-46.

### `generate_picks.py`
- `"posted"`, `"recent:YYYY-MM-DD"`, `"roster_fallback"`, `"bench"`, `"roster_only"`: lineup_source / batting_order sentinel strings (lines 999, 1014, 1018-1020, 1091, 1094, 1112, 1332). No enum.
- `"TBD"`: pitcher name fallback (lines 780, 782, 816, 818, 1161, 1163, 1166, 1375-1376, 1379). A "TBD" pitcher silently becomes `LEAGUE_AVG_PITCHER` at line 1165-1167.
- `"home"`, `"away"`: side keys (many).
- `"T1-Chalk"`, `"T2-Mid"`, `"T3-Longshot"`, `"T4-Untiered"`: tier labels (lines 1604, 1628).
- `EXCLUDED_PLAYERS = {"Anthony Santander", "Eli White"}` (line 133-136).

### `pitcher_profile.py`
- `FASTBALL_TYPES`, `BREAKING_TYPES`, `OFFSPEED_TYPES` pitch-classification literals (line 192-194).
- `"statcast"`, `"mlb_api_estimate"`, `"league_avg_default"`, `"unknown_pitcher_default"`: source tags (line 334, 378, 530, 790).
- Cache namespace strings `"batter_hr_events", "victim_profiles", "pitcher_arsenal", "pitcher_profiles"` (lines 226-227, 285-286, 350-352, 401-402, 508-509).

### `fetch_daily_data.py`
- `MLB_STATS_API`, `BDFED_MATCHUP_API` (line 51-52).
- `DOME_STADIUMS` set (line 55-59) — 8 venues hardcoded.
- `_TEAM_NAME_TO_ABBREV` mapping (line 563-579). "Athletics" vs "Oakland Athletics" alias documented at `generate_picks.py:387-419` after the 2026-05-02 incident.
- `"???"` team fallback (line 632). C2 backlog item.
- `"dome_default"`, `"coords_missing_default"`, `"open_meteo"`, `"api_failed_default"`: weather provenance (lines 858, 863, 902, 919).
- `"R"` throws default (line 366, 374, 527).
- Status string `"posted"`, `"recent:..."`, `"roster_fallback"` (line 267, 295-296, 336, 404).
- `MIN_GAMES=5`, `T1_PCT=0.15` etc as default args (line 661-668) — not stringy but configurable-look.

### `etl/db.py`, `etl/etl_morning.py`, `etl/etl_nightly.py`
- Table/column names embedded throughout. Status codes (`"completed"`, `"failed"`) in `etl_log` (read in `BACKLOG.md` evidence). Not directly in scope files.

## 4. Bug hunt

### B6c — Burger 8-HR floor root cause [partial — B6c covers the question, NEW finding underneath]

**Reproduced.** Burger has `season_batting.hr=8` for season=2026 but `daily_picks.power_score = 50.0` for 5/20 (and every day 5/10–5/20). 50.0 is exactly the value `compute_season_hr_floor(season_hr)` returns for `season_hr ∈ {5, 6, 7}`. Same pattern across:

```
8-HR batters scored 50.0: Aranda, Burger, Dingler  (all tier 2)
8-HR batters scored 60.0: Schmitt, J.Rodriguez, Neto, Y.Diaz, Albies, etc.
5-HR batters scored < 50: Jacob Young 4.0, R.Laureano 5.7  (floor didn't fire at all)
12-HR scored 60.0:        2 cases (should be ≥ 70)
```

**Root cause confirmed via direct MLB API replay.** `fetch_daily_data._fetch_season_batting_splits` queries `/api/v1/stats?stats=byDateRange&endDate={date_str}&sortStat=homeRuns&order=desc&limit=300` (`fetch_daily_data.py:537-548`). The endpoint **lags HR aggregation by ~3 days**:

```
endDate=2026-05-17: Burger HR=7, games=43   (he hit his 8th on 5/17)
endDate=2026-05-18: Burger HR=7, games=44
endDate=2026-05-19: Burger HR=7, games=45
endDate=2026-05-20: Burger HR=8, games=46
```

Games count updates immediately; the `homeRuns` total propagates with a multi-day delay. So at noon on 5/20 the API returned 7 HR for Burger, even though his 8th had been logged in `outcomes` from `hr_events` 3 days earlier.

The `b["hr"]` value the live-tier path produces (`_splits_to_batters:639`) becomes `season_hr` at `score_batters.py:603-605`. Score_power computes the floor from a value that is systematically lower than ground truth — the floor that should be 60 fires as 50, and the 5-HR floor doesn't fire at all for batters who actually have 5 HR.

**Fix prerequisite.** Read `hr` from `season_batting` (or `outcomes` cumulative) instead of from the live splits. `season_batting.fetched_at` shows the morning ETL refreshes it from the same lagging API (`season_batting.fetched_at = 2026-05-20 11:02:37` while `season_batting.hr = 8` for Burger — so the nightly ETL must use a different/fresher source or the storage cumulates differently). The cleanest is `outcomes`-cumulative: `SELECT SUM(hr_count) FROM outcomes WHERE batter_id = ? AND date < ?` (already used by `compute_lab_accuracy.py:120`).

### `score_untiered_starters` (T4) never carries `hr` — `[NEW]`

`generate_picks.py:1324-1346` builds the T4 stub with only `name, team, player_id, _lineup_source`, then `enrich_with_season_batting` (which only writes `barrel_pct/exit_velo/hr_fb_pct/iso/woba` — `generate_picks.py:371`) and optional `games`. **`hr` is never set.** So `score_power`'s `batter.get("hr")` is None for every T4 batter → `compute_season_hr_floor(None) = 0.0` → floor never fires for T4 even when `season_batting.hr ≥ 5`.

Evidence on 2026-05-20: Nick Kurtz (8 HR, T4) scored 33.5; Brent Rooker (7 HR, T4) scored 26.3; Riley Greene (4 HR, T4) scored 21.9. None lifted to the 50/60 floor they qualify for. Stub assembly needs `stub["hr"] = sb_row.get("hr")` alongside the existing games copy at line 1342-1343.

### `backtest_factors.rescore_row` can never apply the season-HR floor — `[NEW]`

`backtest_factors.py:116-130` rebuilds a batter dict from `pick_inputs`. **`pick_inputs` has no `hr` or `season_hr` column** (verified against `PRAGMA table_info(pick_inputs)`). So every backtest re-score returns `base_score`, even with `USE_SEASON_HR_FLOOR=True`. Any weight-refit run over historical data sees floor-less power scores, while production sees floor-applied scores. This is a silent backtest-vs-live divergence that affects refit weight selection.

### Caller signature audit — `[NEW]`

Verified the recently-refactored functions:
- `get_recent_game_log(player_id, season, hr_window=10, rate_window=30)` — `fetch_daily_data.py:1060`. Callers: `generate_picks.py:506` (`get_recent_game_log(pid, season)` — uses defaults, OK), no other callers.
- `get_recent_pitcher_game_log(pitcher_id, season, today_str=None, days=21)` — `fetch_daily_data.py:1155`. Callers: `generate_picks.py:539` (OK); `diagnostics/counterfactual_recency_2026_05_12.py:116` (OK).
- `score_form(batter)` — single arg. Callers `backtest_factors.py:172`, `diagnostics/factor_diagnostics.py:417`, `score_batters.py:1074`. All pass the batter dict.
- `score_matchup`, `score_pitcher_vulnerability` — no signature changes detected, callers consistent.

No new TypeError-class bugs from PR #56/#57.

### `factor_diagnostics.py` reads OLD Form columns — `[covered by A3]`

`diagnostics/factor_diagnostics.py:403-405` builds batter dict with `recent_hr_14d, recent_barrel_pct_14d, ev_trend_14d`. These are the pre-PR-#56 columns; `score_form` no longer reads them. The diagnostic always returns 50 for the form factor now. Already in A3 backlog.

### Three batter-dict assembly paths — fields that diverge — `[NEW]`

| Field | Live tiered (~1192) | T4 untiered (~1386) | Offline sim (~1479) |
|---|---|---|---|
| `hr` | ✓ from `_splits_to_batters` | **MISSING** | ✓ from `mlb_2025_tiers` |
| `pa`, `ab` | ✓ | MISSING | ✓ |
| `games` | ✓ | only when `sb_row["games"] is not None` | MISSING (no platoon dampener) |
| `bats` | ✓ | from `_splits_to_batters` via enrich — actually NOT set since enrich doesn't write `bats`; stub has no `bats` either | ✓ |
| `barrel_pct`, `exit_velo`, `hr_fb_pct`, `iso`, `woba` | ✓ via enrich | ✓ via enrich (when `sb_row` exists) | ✓ from tier dict |
| `xwoba_contact`, `pull_fb_pct` | ✓ from bulk | ✓ from bulk (`xwoba_contact` only) | not set |
| `recent_hr_10g` / `recent_iso_30g` / `recent_avg_30g` | ✓ from log | ✓ from log | ✓ synthesized noise |
| `_lineup_source` | ✓ | ✓ | not set |
| `team` | ✓ via enrich (overwrites tier abbrev with mismatched cases) | ✓ from full→abbrev | ✓ from tier dict |

**Two field-level bugs from this:** T4 has no `hr` (see above), and T4 stubs have no `bats` → `compute_composite` line 1076: `batter.get("bats", "R") or "R"` → defaults R. Park handedness skew and platoon bonus may both be miscalculated for every left-handed and switch T4 hitter.

### `try_fetch_statcast_recent` is dead code — `[covered by A2 / session notes]`

`generate_picks.py:467-491`. Function exists, returns the deprecated `recent_hr_14d / recent_barrel_pct_14d / ev_trend_14d` triple. Grep confirms no live caller — only `_archive_docs_2026-05-02_DO_NOT_USE` references it. Documented dead per BACKLOG.md A2. Safe to delete with A2.

### `_platoon_dampener`: `max_games` only set when live_tiers present — `[NEW]`

`generate_picks.py:1571-1576` computes `max_games` from `live_tiers.{1,2,3}`. In offline mode, `slate_ctx` is None — passed through to `compute_composite` as None — `_platoon_dampener(games, None)` returns 1.0 (no-op). OK for offline.

But: **T4 stubs (line 1342) only set `games` when `sb_row["games"] is not None`**. T4 batters with no `season_batting` row (true rookies, just-recalled minors) → `games = None` → `_platoon_dampener` returns 1.0, gets the daily-starter multiplier. Inverse, every T1/T2/T3 batter has `games` from `_splits_to_batters`. So a true rookie called up today gets MORE composite credit than an actual platoon hitter with 30 games. Asymmetry between T4 and the rest.

### Asymmetric clamps and floors — `[NEW]`

- `score_matchup` v1 line 699: `min(100, base + platoon_bonus + rookie_bonus)`. If `base = 95` and both bonuses fire (`+10 + 15 = +25` → 120 capped to 100), the +15 rookie bonus can't actually fire above 90 base. The +10 platoon and +15 rookie are nominally additive but become non-additive at the top. Score_matchup_v2 has the same `min(100, ...)` clamp at line 761 of `pitcher_profile.py`.
- Park additive bonus `composite += 0.05 * park` at `score_batters.py:1107` — bonus IS NOT clamped, but `park ∈ [0, 100]` so `+5 max`. Then `_platoon_dampener` multiplies the whole composite, which scales the bonus too. The comment line 1116-1118 calls this out intentionally.
- `score_humidity` line 954: `h = max(0, min(100, humidity_pct))`. Real humidity is always ≥ 0; bound is unnecessary but harmless.

### `score_lineup_position` asymmetric None vs "bench" — `[NEW, low confidence — verify]`

`score_batters.py:840` returns 35.0 for `None`; line 843 returns 15.0 for `"bench"` or `"roster_only"`. A 20-point gap. The comment at line 845-852 acknowledges that the fall-through at line 852 silently returns 35.0 for unexpected inputs.

In `score_live_slate`, `batting_order` is set to `"roster_only"` when no lineup data exists for the game (line 1094) or `"bench"` when there IS lineup data but the batter isn't in it (line 1091). True `None` should be rare on the live path. **But it does appear in T4 untiered (`generate_picks.py:1366-1424`)** — T4 stubs pass `batting_order=i` from the lineup payload's index (line 1346), so this is the 1-9 path. So None is mostly hit on backtest_factors / factor_diagnostics. Production impact: low. Worth a one-line consistency fix anyway.

### Slate context wind handling — `[NEW, low confidence — verify]`

`compute_slate_context:217-220` REQUIRES temp, wind, AND humidity to be non-None for a game to enter `weather_pct`. But `score_weather` line 978: `wind_mph = weather.get("wind_mph", 5) or 0` — defaults to 5 mph then ORs to 0 if falsy. When slate_ctx **does** have the game in `weather_pct`, `score_weather` returns `base_pct * 0.60 + wind_score * 0.40` (line 988-989) — the wind_score uses whatever `weather.get("wind_mph", 5) or 0` produces. The 60/40 blend is consistent.

When slate_ctx does NOT have the game, line 991-998 mixes temp/wind/humidity — and `weather.get("temperature_f", 68)`, `weather.get("humidity_pct", None)` default in. So a partial weather dict that fails the slate-ctx 3-component gate STILL gets scored via the fallback with imputed values. The skip-on-missing principle (`compute_slate_context` comment at 217-220) only protects the percentile rank, not the fallback. Two different code paths can score the same game with two different numbers.

### `pitcher_profile.build_pitcher_profile` league-avg masquerading — `[NEW, low confidence]`

`pitcher_profile.py:520-532` — final fallback profile gets `source = "league_avg_default", confidence = 0.2` but the same numeric values as a real Statcast profile. `archetype_similarity` then blends with confidence at line 596-599 (`raw * combined_confidence + 50 * (1 - combined_confidence)`). At confidence=0.2, the similarity is 80% pulled toward 50, so impact is limited — but the underlying issue is the same "synthetic vs measured" mix as Power (B6).

### `team='???'` in season_batting — `[covered by C2]`

`fetch_daily_data.py:632`: `team_abbrev = (team.get("abbreviation") or _TEAM_NAME_TO_ABBREV.get(team_name, "???"))`. Already in C2.

## 5. Cross-check vs backlog

| # | Finding | Tag |
|---|---|---|
| 1 | Burger 8-HR floor: MLB API HR-aggregate lags 3 days, `b["hr"]` reaches `score_power` undercount | `[partial — B6c asks the question; root cause is NEW]` |
| 2 | T4 untiered path never sets `hr` → floor never fires for T4 (Kurtz/Rooker/Greene evidence) | `[NEW]` |
| 3 | `backtest_factors.rescore_row` cannot apply season-HR floor (no column in pick_inputs) | `[NEW]` |
| 4 | T4 stubs have no `bats` → handedness defaults to "R" silently | `[NEW]` |
| 5 | T4 stubs have `games=None` when no season_batting row → platoon dampener no-ops for rookies | `[NEW]` |
| 6 | "TBD" pitcher path silently becomes LEAGUE_AVG_PITCHER, no provenance | `[NEW]` |
| 7 | `factor_diagnostics.py` reads pre-PR-#56 Form columns | `[covered by A3]` |
| 8 | `try_fetch_statcast_recent` dead code | `[covered by A2]` |
| 9 | v1 platoon +10 vs v2 +0 inconsistency | `[covered by A4]` |
| 10 | Two weather scoring paths can disagree (slate_ctx vs fallback) on partial weather dicts | `[NEW]` |
| 11 | Asymmetric `score_lineup_position` (None=35, bench=15) | `[NEW, low confidence]` |
| 12 | `score_matchup` v1 `min(100, base + bonuses)` makes top-end bonuses partial | `[NEW]` |
| 13 | Hardcoded `LEAGUE_AVG_PITCHER.hr_per_9 = 1.2` vs comment "real 2026 ~1.27" | `[NEW]` |
| 14 | `PARK_CF_BEARING.get(venue, 0)` silently defaults to 0° for non-mapped venues | `[NEW]` |

## 6. Priority recommendation

**Ship before B6 (Power rebuild):**
1. **Finding 1 + 2** — read `season_hr` from `outcomes` (cumulative SUM as in `compute_lab_accuracy.py:120`), set it on the batter dict in **all three** assembly paths (live, T4, offline) under the key `season_hr` (not `hr`). This fully removes the MLB-API-lag dependency and decouples the floor from the live splits — and it lets B6's smooth curve actually fire on T4 batters too. Without this, the B6b log-curve rewrite inherits the same lag.
2. **Finding 3** — add `season_hr` to `pick_inputs` (write at compose time) so `backtest_factors.rescore_row` can apply the same floor. Otherwise B6 refit lands on backtest data that scored without the floor.

**Can ship in parallel with B6:**
3. **Finding 4** — T4 stubs need `bats` from `season_lookup` (column already in `season_batting`, just not in `load_season_batting_lookup`'s SELECT at `generate_picks.py:164-172`). One-line SELECT change + one assignment in `score_untiered_starters`.
4. **Finding 6** — gate `slate["pitchers"].get(...)` so a literal "TBD" doesn't get scored at all (skip the batter, mark `selected=0`, document on the row).
5. **Finding 10** — make `score_weather` skip-on-missing when slate_ctx doesn't have the game (return 50, not fallback fixed-anchor mix on imputed values).
6. **Finding 5** — pull `games` from `season_lookup` regardless of value (treat None as "no signal" by falling through to platoon=1.0, but mark the row).

**Defer:**
7. **Finding 11** — None→35 vs "bench"→15: rare in production. One-line consistency improvement when touching `score_lineup_position` for another reason.
8. **Finding 12** — clamp-at-100 making top-end bonuses partial: real impact is small (bonus rarely fires at 95+ base); revisit when the matchup blend gets refit.
9. **Finding 13** — LEAGUE_AVG_PITCHER 1.2 vs 1.27: code already comments this is stale; bump as part of next refit cycle.
10. **Finding 14** — PARK_CF_BEARING default 0: only matters for venues not in the table (currently all 30 are listed). Add a defensive `KeyError`-style guard only if a new venue surfaces.
