# B16 — parity check fails by ~0.24 on forward path; need a design call

## What I implemented (matches brief literally)

- `etl/db.py`: three idempotent ALTERs adding `slate_park_pct`,
  `slate_weather_pct`, `slate_pitcher_vulnerability_pct`. Migration runs
  cleanly; columns confirmed via PRAGMA.
- `score_batters.py`: three optional kwargs added to `score_park` /
  `score_weather` / `score_matchup` with the exact signatures specified
  in the brief. When None (default), production scoring is byte-identical
  (smoke pins `pin_score_park_kwarg_none_safe` /
  `pin_score_weather_kwarg_none_safe` / `pin_score_matchup_kwarg_none_safe`
  all pass). `compute_composite` computes the three percentiles using the
  same `percentile_rank_dict` mid-rank method `compute_slate_context` uses
  and persists them into `inputs_snapshot`. Park's snapshot is
  post-handedness-adjusted to match `score_park`'s slate-relative branch.
- `load_picks_to_db.py`: INSERT writes the three new columns
  (60 columns / 60 placeholders / 60 values verified).
- `backtest_factors.py` + `refit_weights.py`: `load_*` SELECTs `pi.bats,
  pi.throws, pi.slate_park_pct, pi.slate_weather_pct,
  pi.slate_pitcher_vulnerability_pct`. Both `rescore_row` functions stop
  hardcoding `bats="R"` / `throws="R"` (B19 fold-in) and pass the three
  slate values as kwargs.
- `diagnostics/backfill_slate_pct.py`: replays `compute_slate_context`'s
  logic per-date from already-persisted raw columns. Ran on canonical
  DB; coverage on the 2025 backfill range (188 dates, 55,638 rows):
  87.5% park (gap = rows without persisted hr_park_factor),
  63.1% weather (gap = 23.6% domes + 6.0% partial-weather, both
  intentional skip-on-missing matching production), 99.5% pitcher.
- `tests/smoke.py`: 5 new pins added; 114 PASS total (up from 109 PASS
  baseline) + same 1 pre-existing HALT (batting_order>9 residue, on
  CLAUDE.md false-alarms list) + same 1 pre-existing WARN.

## The blocker

Brief verification step 2: "The rescored composite must match the
persisted composite within ±0.1." Failing.

### Forward parity (cleanest test — in-process)

A synthetic batter scored via `compute_composite` with a synthetic
`slate_ctx` produces composite=73.10. Reading the persisted slate_pct
values from the resulting `inputs_snapshot` and re-running
`refit_weights.rescore_row` on a mock pick_inputs row carrying them:

| factor   | production | rescore  | delta    |
|----------|-----------:|---------:|---------:|
| power    | 70.00      | 70.00    | +0.00    |
| matchup  | 83.40      | 82.57    | **-0.83**|
| park     | 100.00     | 100.00   | +0.00    |
| form     | 52.50      | 52.50    | +0.00    |
| weather  | 60.60      | 60.57    | -0.03    |
| lineup   | 78.00      | 78.00    | +0.00    |
| **composite** | **73.10** | **72.86** | **-0.24** |

Delta = -0.24 → above the ±0.1 threshold.

### Real-row parity (Kyle Tucker, 2026-05-25, v1, batter_id=663656)

- daily_picks.composite: **46.5**
- refit_weights.rescore_row composite: **45.37**
- Delta: **-1.13**

(Real-row delta is worse than synthetic delta because the slate_pct
values used here are from MY backfill, which can't replay
`compute_slate_context`'s low-IP pull-toward-neutral nudge — `ip` isn't
in pick_inputs. Pre-B16 rows are the only rows currently available;
no row exists yet that was written by the B16 code path.)

### Root cause

Production `score_matchup` with `slate_ctx` + `batter_team` averages
THREE signals `[pitcher_pct, woba, team_total_pct]`. The rescore
calls `score_matchup(batter, pitcher,
slate_pitcher_vulnerability_pct=88.0)` — without `batter_team`, so
`team_total_pct` is silently skipped, leaving only `[pitcher_pct, woba]`.

`vegas_team_total_pct` IS persisted in pick_inputs (since
2026-05-03). Both `load_*` SELECTs already pull it. But neither
`rescore_row` passes it back to `score_matchup`. This is a pre-existing
gap that B16's three-kwarg spec doesn't close.

### Closing the gap

A ~10-line addition to both `rescore_row` functions:
```python
team_total_raw = row.get("vegas_team_total_pct")
batter_team = row.get("batter_team")
synthetic_slate_ctx = None
if team_total_raw is not None and batter_team:
    synthetic_slate_ctx = {
        "active": True,
        "team_total_pct": {batter_team: float(team_total_raw)},
    }
...
"matchup": score_matchup(
    batter, pitcher,
    slate_ctx=synthetic_slate_ctx,
    batter_team=batter_team,
    slate_pitcher_vulnerability_pct=slate_pitcher_vulnerability_pct,
),
```

No schema change (vegas_team_total_pct already persisted, already in the
SELECT). No new kwarg. With this, forward parity drops to ~0.00; real-row
parity within the brief's threshold for any v1 row where the persisted
data is recent enough.

## What needs to be decided

The brief is strict on two things:
1. "Add additive optional kwargs to `score_park`, `score_weather`,
   `score_matchup`" — three kwargs listed verbatim.
2. "The rescored composite must match the persisted composite within
   ±0.1."

These are inconsistent: the 3-kwarg spec can't achieve the ±0.1
parity. **I'm not going to invent a 4th kwarg or invisibly thread
batter_team without your sign-off.** The CLAUDE.md hard rule "Do not
assume / If a path is ambiguous, ask the user" applies — this is
exactly that.

**Option A.** Allow the small `batter_team` plumbing (10 lines per
rescore_row, no new schema, no new kwarg — it's just passing data
that's already SELECTed). Close the parity gap. Ship.

**Option B.** Keep strict to the 3-kwarg scope. Document the
~±0.3 forward-path parity (down from ~5+ pp pre-B16) in PR description.
File the vegas plumbing as a follow-up B-series.

**Option C.** I'm wrong about something — the rescore IS supposed to
match without vegas plumbing, and I'm missing a detail. Please point
to what.

## What I haven't done (deliberate, until you decide)

- I haven't pushed the implementation to GitHub yet.
- I haven't updated the PR description with parity numbers (waiting
  for the decision so the documentation reflects the chosen path).

## Files touched (review-ready)

Modified:
- `etl/db.py` (migration)
- `score_batters.py` (kwargs + percentile snapshot)
- `load_picks_to_db.py` (INSERT columns)
- `backtest_factors.py` (load_history + rescore_row + B19 fold-in)
- `refit_weights.py` (load_from_db + rescore_row + B19 fold-in)
- `tests/smoke.py` (5 new pins, all PASS)

Created:
- `diagnostics/backfill_slate_pct.py`

Not touched (out of brief scope):
- `pitcher_profile.py` (v2 path), `generate_picks.py` (all 3 batter
  paths route through `compute_composite` which is where I do the
  snapshot, so no separate plumbing needed)
- BACKLOG.md, WEIGHT_REFIT_LOG.md (PM-owned)
