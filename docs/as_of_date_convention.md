# As-of-date convention

Reference for the 2025-season backfill (PR 4) and any future historical
reconstruction work. Added 2026-05-21 in PR 3.

## What `as_of_date` means

A `YYYY-MM-DD` string that answers the question: "what did the model know
at noon on this date?"

When a function accepts `as_of_date`:
- **`None` (default)** = production behavior = today. No filter; backwards
  compat with every existing caller.
- **A historical date** = simulate the model's view of that morning.
  Pitch events, HR events, and any "rolling window" data are filtered to
  `game_date < as_of_date` (strictly before — the games on `as_of_date`
  itself haven't been played yet at noon, so they don't enter the
  reconstruction).

Same semantics across the pipeline:
| Where | Behavior with `as_of_date` set |
|---|---|
| `pitcher_profile._fetch_batter_hr_events` | Drops HR events on/after the date. |
| `pitcher_profile._fetch_pitcher_arsenal_statcast` | Drops pitch events on/after the date. |
| `pitcher_profile.build_victim_profile` | Filters HR events + passes through to the per-pitcher arsenal lookups. |
| `pitcher_profile.build_pitcher_profile` | Filters Statcast pitches. |
| `pitcher_profile.build_pitcher_profiles_batch` | **Bypasses DB cache** when `as_of_date != today` (DB is snapshot-as-of-nightly-ETL, not as-of-date — would inject look-ahead). Falls through to the per-pitcher Statcast path. |
| `pitcher_profile.build_victim_profiles_batch` | Same DB-bypass logic. |
| `features_v2.fetch_batter_recent_statcast_14d` | (PR 1 / B6a) already as-of-date-aware; window is `[as_of_date − 14d, as_of_date)`. |
| `fetch_daily_data.get_recent_pitcher_game_log` | (PR 2 / B4) already aware via `today_str` param. |
| `fetch_daily_data.get_weather` | Auto-routes to Open-Meteo **archive** endpoint when `game_time_iso` is more than ~5 days in the past. Production keeps using the forecast endpoint. |
| `fetch_daily_data._fetch_season_batting_splits` | Already accepts `start_str` / `end_str`. Backfill passes `end_str = as_of_date − 1 day`. **MLB API HR-aggregate lag is moot** for any date more than ~3 days in the past, so historical reconstructions get accurate season totals. |

## What's intentionally NOT as-of-date-aware

- **`LEAGUE_AVG_VICTIM`, `LEAGUE_AVG_PITCHER`** — by construction not
  date-dependent. Backfill keeps using them as fallback.
- **`park_factors`** — hardcoded seed table, stable across the
  reconstruction window.
- **`_fetch_pitcher_arsenal_mlb_api` (MLB API fallback)** — only returns
  season-aggregate stats with no per-pitch detail, so it can't be honestly
  date-filtered without a different endpoint. It's a low-resolution
  league-mean approximation either way. Accepted approximation when
  Statcast misses a pitcher in the backfill window.
- **Vegas implied totals** — the-odds-api free tier doesn't provide
  historical odds. Backfill leaves `vegas_team_total_pct` / `_raw` as
  `None`, and the matchup scorer's `skip-on-missing` handles it cleanly.

## How PR 4 (backfill orchestrator) uses this

For each historical date `D` in the backfill window:
1. Pull schedule + lineups for `D` via `statsapi schedule?hydrate=lineups`
   (the endpoint accepts historical dates).
2. Pull season splits as of `D − 1` via `_fetch_season_batting_splits(start_of_season, D - 1d)`.
3. Pull weather: `get_weather(venue, "D 19:00 local")` — auto-routes to archive.
4. Build pitcher profiles: `build_pitcher_profiles_batch(pids, season, as_of_date=D)`.
5. Build victim profiles: `build_victim_profiles_batch(bids, season, as_of_date=D)`.
6. Fetch recent batter Statcast (14d): `fetch_batter_recent_statcast_14d(as_of_date=D)`.
7. Fetch recent pitcher window: `fetch_pitcher_recent_form_batch(..., today_str=D)`.
8. Run scoring with the standard `compute_composite` pipeline.
9. Persist to `pick_inputs` with the row's `date = D`.

Every "what did the model know" input passes through with `as_of_date=D`,
so the resulting `pick_inputs` row reflects only past-as-of-D data — no
look-ahead bias.

## Cache key isolation

All as-of-date-aware fetchers include `as_of_date` in their cache key, so
production noon runs and historical backfills don't poison each other's
cache. A production fetch for `pitcher_arsenals/0_2026` and a backfill
fetch for `pitcher_arsenals/0_2026_asof_2025-06-15` live in different
files.

## Smoke tests

`tests/smoke.py` (PR 3 additions):
- `pin_filter_before_drops_after_date` — strict `<` semantics.
- `pin_filter_before_none_is_noop` — production unchanged.
- `pin_filter_before_empty_safe` — handles None / empty df / missing column.
- `pin_as_of_date_signatures_present` — all 6 profile functions accept the kwarg with `None` default.
- `pin_weather_archive_threshold_present` — endpoint routing wired.
