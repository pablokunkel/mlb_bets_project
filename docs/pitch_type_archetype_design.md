# Pitch-type archetype matchup signal — design

Status: **Phase 1 — schema + scaffolding (this PR)**. The signal is built
behind a feature flag with weight 0 in the composite. Phases 2-4 detailed
in [Rollout](#rollout-plan) below — each is a separate, reviewable PR.

## Problem statement

Today's `score_matchup` blends pitcher vulnerability (HR/9 + recent
HR/9 + HH% + K/9 + ERA + FB% allowed), batter wOBA vs. handedness, a
Vegas team-total percentile, a rookie pitcher bonus, and a platoon
bonus. It is **blind to batter pitch-type preferences vs the specific
pitcher's arsenal mix.**

Concrete miss: when Chas McCormick (career .550 SLG vs. fastballs, .310
vs. breaking) faces a pitcher who throws 70% fastballs, our matchup
score reads the same as when he faces a pitcher who throws 40%
fastballs and 50% breaking — because all the pitcher-vulnerability
inputs aggregate **across** pitch types, not per pitch type. The
archetype-similarity signal in `score_matchup_v2` measures pitcher
shape similarity but says nothing about how a specific batter performs
against the *kinds* of pitches in that shape.

## Signal definition

```
batter_xSLG_vs_arsenal =
      pitcher_today.fb_usage_pct × batter.fb_slg
    + pitcher_today.br_usage_pct × batter.br_slg
    + pitcher_today.os_usage_pct × batter.os_slg
```

Inputs:

- `pitcher_today.{fb,br,os}_usage_pct` — already produced by
  `pitcher_profile._classify_pitch_mix`. Lives on the pitcher arsenal
  dict consumed by `compute_composite`. Sums to ~1.0.
- `batter.{fb,br,os}_slg` — **new data**. SLG by pitch-type group,
  season-to-date through `date_through`. Built by a new ETL function
  `features_v2.fetch_batter_pitch_type_splits`.

Output: a single 0-1 number (xSLG against the *expected* mix of this
specific pitcher). Mapped to 0-100 via fixed anchors (see
[Score mapping](#score-mapping)).

### Pitch-type grouping (FB / BR / OS)

Matches `pitcher_profile.FASTBALL_TYPES / BREAKING_TYPES /
OFFSPEED_TYPES`:

| Group | Statcast `pitch_type` codes |
|---|---|
| **FB** (fastballs) | FF (4-seam), SI (sinker), FC (cutter), FT (2-seam), FA (generic FB) |
| **BR** (breaking) | SL (slider), CU (curveball), KC (knuckle-curve), SV (slurve), ST (sweeper), CS, EP |
| **OS** (offspeed)  | CH (changeup), FS (splitter), FO (forkball), KN (knuckleball), SC (screwball) |

**Why three buckets instead of per-pitch-type splits.** A typical
batter sees ~2,000 pitches and ~450 batted balls in a season. Splitting
SLG into 12 individual Statcast pitch codes:

- median ~50 batted balls per code for the *most common* codes (FF, SL),
- single-digit batted balls for rarer codes (SV, KN, SC, FA),
- under 20 batted balls for code-level splits on the fringe of a
  batter's profile (CU for a batter who doesn't see many curves).

SLG on under-30 batted balls is dominated by sequence noise — a single
HR moves it 100+ points. The three-bucket grouping aggregates enough
events to be stable: a typical batter has 150+ batted balls vs FB,
80-150 vs BR, and 40-100 vs OS. That's the same range of sample sizes
the existing `pitcher_profile` archetype work has been operating on for
months without complaints. We can revisit finer-grained (5-bucket:
4-seam vs sinker/cutter, slider vs curve, etc.) once the 3-bucket
signal earns its place in the composite.

### Sample-size handling

**Policy: None+skip, NOT league-avg fallback** (set 2026-05-26 per
user feedback).

If ANY of the three pitch-type groups has fewer than `PITCH_TYPE_SPLIT_MIN_BB`
batted balls (currently 30), or is entirely missing, `_compute_xslg_vs_arsenal`
returns `None` and `score_matchup` skips the sub-signal term — the
batter's matchup score is built from the *other* matchup signals, no
imputed value enters the composite. Same convention `score_form` uses
for `None` inputs (skipped from the mean, not imputed).

**Why not league-avg fallback?** That was the original Phase 1 design;
we reversed it before merge. A league-avg fill artificially flattens
every small-sample batter to a neutral xSLG (~.405 at typical 70/20/10
arsenal usage), which then *inflates* their matchup score above where
their actual signal would land. The matchup composite is honest about
uncertainty by skipping the term — the missing-data hit gets absorbed
across the remaining signals.

The 30-batted-ball threshold is itself a defensible default — it
mirrors the `min_batted_balls=10` cutoff in `_aggregate_recent_statcast`
scaled up for a season-long denominator. Phase 3 should re-test it
against the empirical noise floor (could be 20, could be 50 — TBD by
the backtest).

For reference (used in Phase 3 score-mapping calibration, not in the
scoring math itself), the population SLG anchors per group are:

| Group | League-avg SLG | Source |
|---|---|---|
| FB | .420 | 2024 Statcast leaderboard (qualified batters), mean SLG vs `pitch_type in (FF, SI, FC)`. Holds within ±.010 across 2022-2024 — the population doesn't shift much year-over-year. |
| BR | .350 | Same source, `pitch_type in (SL, CU, KC, SV, ST)`. |
| OS | .380 | Same source, `pitch_type in (CH, FS)`. |

These anchors will inform the Phase 3 `min_max_scale(xslg, lo, hi)`
mapping (where to place the 0 and 100 endpoints on the SLG distribution)
but are NOT used as fallback values inside `_compute_xslg_vs_arsenal`.

### As-of-date snapshotting

Same convention as every other historical input
([`docs/as_of_date_convention.md`](as_of_date_convention.md)).
Snapshots live in a new table:

```sql
CREATE TABLE batter_pitch_type_splits (
    player_id       INTEGER NOT NULL,
    date_through    TEXT NOT NULL,    -- 'YYYY-MM-DD'; season-to-date through this date
    fb_slg          REAL,
    fb_pa           INTEGER,          -- batted-ball count for sample-size gating
    br_slg          REAL,
    br_pa           INTEGER,
    os_slg          REAL,
    os_pa           INTEGER,
    fetched_at      TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (player_id, date_through)
);
CREATE INDEX idx_bpts_date ON batter_pitch_type_splits(date_through);
CREATE INDEX idx_bpts_player ON batter_pitch_type_splits(player_id);
```

- Refreshed nightly with the rest of Statcast (Phase 2).
- A backfill script populates one row per (batter, date) across the
  2025 season for the A1 refit — same one-shot pattern as
  `etl/backfill_statcast_windows.py` (on the `claude/b12-wider-statcast-windows`
  branch) and `etl/backfill_2025.py`.

Reads in `score_matchup` look up the row where `date_through =
as_of_date - 1 day` (the previous completed day's snapshot). At noon
on `D`, the most recent snapshot is `D - 1 day` — today's games haven't
happened yet, so games ON `D` are correctly excluded.

### Score mapping

The raw `xSLG_vs_arsenal` is a SLG number (typical range 0.300 to
0.550). Mapped to 0-100 via the same `min_max_scale` helper
`score_matchup` already uses on `woba_vs_hand`:

```
xslg_vs_arsenal_score = min_max_scale(xslg, 0.350, 0.500)
```

Anchors picked to match the rough season-long league distribution
(.350 ~ league avg, .500 ~ elite arsenal-matchup). Will be re-tuned in
Phase 3 after the backtest is run on real backfill data — same
methodology as `score_power`'s anchor re-tune on 2026-05-03.

### Wiring into `score_matchup` — sub-signal vs. new top-level factor

Two design options. Picking **sub-signal** for the reasons below.

| Option | Pros | Cons |
|---|---|---|
| **A. Sub-signal of `score_matchup`** (chosen) | Refit-safe — existing weight calibration on the 6 top-level factors (power 0.250, **matchup 0.264**, park 0.000, form 0.279, weather 0.057, lineup 0.150) doesn't change. The arsenal signal slots in alongside `vuln`, `sim`, `total`, `woba` inside `score_matchup`'s mean. | Slightly less granular control — can't dial arsenal independently of vulnerability. Mitigated by the in-function flag + sub-weight (Phase 3). |
| B. New top-level factor `arsenal_score` | Maximum dial-in flexibility — independent weight. | Needs an A1 refit cycle on all 7 top-level factors. Without that, the +1 factor inflates the matchup family's effective weight, which is exactly the kind of un-calibrated change the WEIGHT_REFIT_LOG warns against. |

**Decision: sub-signal of `score_matchup` (Option A).** Adding a 7th
top-level factor would change `WEIGHT_CONFIGS["default"]` in a way that
requires its own refit on its own data, blocking the rollout behind A1.
The sub-signal approach is strictly additive and reversible — if the
backtest shows the signal is noise, we set its weight to 0 and the
composite output is byte-identical to today.

## Implementation in this PR (Phase 1)

Strictly additive scaffolding. No production behavior changes.

### 1. DB migration — `etl/db.py`

The `batter_pitch_type_splits` CREATE TABLE block above, plus an
idempotent ALTER pattern for existing DBs (mirrors the migration
blocks already in `create_tables`).

### 2. ETL skeleton — `features_v2.py`

```python
def fetch_batter_pitch_type_splits(
    player_ids: list[int],
    as_of_date: str | None = None,
    season: int | None = None,
) -> dict[int, dict]:
    """
    Build batter SLG splits by pitch-type group (FB/BR/OS) season-to-date.

    Returns {player_id: {fb_slg, fb_pa, br_slg, br_pa, os_slg, os_pa}}.

    Phase 1 (this PR): signature + docstring + PITCH_TYPE_SPLIT_MIN_BB
    threshold. Implementation is a TODO — Phase 2 wires it to the bulk
    Statcast pull pattern used by `fetch_batter_recent_statcast_14d`.
    """
    ...
```

The per-group threshold below which the sub-signal returns None (see
"Sample-size handling" above):

```python
PITCH_TYPE_SPLIT_MIN_BB = 30
```

No league-avg fallback constants are exposed — Phase 1's policy is
None+skip. Population SLG anchors for FB/BR/OS are documented above
for Phase 3 score-mapping calibration but are not consumed by the
scoring code path.

### 3. Scoring hook — `score_batters.py` + `generate_picks.py`

In `generate_picks.py` the batter-dict assembly (`score_live_slate`,
`score_untiered_starters`, offline simulation) gets three new keys
defaulting to `None`:

```python
"fb_slg": None,   # set by fetch_batter_pitch_type_splits in Phase 2
"br_slg": None,
"os_slg": None,
```

In `score_batters.py` a new module-level flag and helper:

```python
USE_ARSENAL_SUBSIGNAL = False  # Phase 1 default. Flip in Phase 3 after backtest.

def _compute_xslg_vs_arsenal(batter: dict, pitcher: dict) -> float | None:
    """Return blended xSLG-vs-arsenal, or None if any input is missing
    OR any group is below the PITCH_TYPE_SPLIT_MIN_BB threshold.

    Reads batter.{fb,br,os}_slg + batter.{fb,br,os}_pa / pitcher.{fb_usage_pct,
    breaking_usage_pct, offspeed_usage_pct}. Returns None (caller skips
    the term) when:
      - the pitcher arsenal usage isn't available, OR
      - any batter group has < PITCH_TYPE_SPLIT_MIN_BB batted balls.

    No league-avg imputation. See "Sample-size handling" above.
    """
    ...
```

`score_matchup` reads the helper output behind the `USE_ARSENAL_SUBSIGNAL`
guard. With the flag off (default), `score_matchup` is byte-identical
to today's behavior — verified by a pin test.

### 4. Backtest harness skeleton — `diagnostics/backtest_arsenal_inputs.py`

Modeled on `backtest_power_inputs.py` and `backtest_form_anchors.py`.
Two variants:

- `current` — `score_matchup` as it ships today (no arsenal term).
- `arsenal_blend` — `score_matchup` + `xslg_vs_arsenal_score` averaged in.

Same `auc / top10_lift / quint_mono / avg_rank_hr` metrics. Same
"common subset" filter (rows where both signals are computable).
Same caveat block about caveats. **Not actually runnable yet** —
the SQL fetch references `pi.fb_slg / pi.br_slg / pi.os_slg` which
won't exist in `pick_inputs` until Phase 2. There's a guard at the
top of `main()` that bails with a clear message until then.

### 5. Smoke tests

Pinned in `tests/smoke.py`:

- `pin_batter_pitch_type_splits_table_exists` — the new table is
  created by `create_tables`.
- `pin_score_matchup_arsenal_flag_default_off` — `USE_ARSENAL_SUBSIGNAL`
  defaults to `False`.
- `pin_score_matchup_arsenal_flag_off_no_op` — for a fixed
  `(batter, pitcher)` pair, `score_matchup` with `fb_slg` / `br_slg`
  / `os_slg` set on the batter dict returns the same score as
  without them (the additive change is gated, flag-off behavior is
  byte-identical to historical).
- `pin_compute_xslg_vs_arsenal_basic` — function signature exists,
  returns the documented blend with synthetic inputs.
- `pin_fetch_batter_pitch_type_splits_signature` — the ETL function
  exists with `as_of_date` kwarg defaulting to `None`.

## Rollout plan

Phased intentionally. Each phase ships as its own PR. The user reviews
each before approving the next.

### Phase 1 — schema + scaffolding (this PR)

- `batter_pitch_type_splits` table created.
- `fetch_batter_pitch_type_splits` signature + TODO body.
- `xslg_vs_arsenal` keys on the batter dict (default `None`).
- `USE_ARSENAL_SUBSIGNAL = False`, sub-signal guarded.
- Backtest harness skeleton.
- Smoke tests pin the foundation.

**No production behavior changes.** No new Statcast load. Nothing
hits the new table.

**Reviewer checks for Phase 1.** Schema looks right; flag defaults to
off; smoke tests pass; no live calls; design doc covers the math
clearly.

### Phase 2 — populate `batter_pitch_type_splits` for 2025 backfill

Separate follow-up PR.

- Implement the body of `fetch_batter_pitch_type_splits`. One bulk
  `pybaseball.statcast` pull per as-of-date, sliced per batter, then
  aggregated per pitch-type group. Same bulk-pull-and-slice pattern as
  `fetch_batter_recent_statcast_14d`.
- Wire into nightly ETL (`etl/etl_nightly.py`) behind a `# Step 7:
  pitch-type splits` block, refreshing the table for today's batters.
  Production starts populating the new table here.
- One-shot backfill script `etl/backfill_pitch_type_splits.py` that
  walks 2025-03-27 → 2025-09-30 and writes one row per (batter, date).
  Same chunked, `--max-runtime` orchestrator pattern as
  `etl/backfill_2025.py`.
- `pick_inputs.fb_slg / br_slg / os_slg` added by an idempotent ALTER
  in `etl/db.py`.
- Backfill writes those values into `pick_inputs` for every 2025 row.
- Smoke probe: count of `batter_pitch_type_splits` rows for the latest
  date in `pick_inputs` is non-zero.

**Reviewer checks for Phase 2.** Backfill is honest as-of-date
(spot-check: a row dated 2025-06-01 only includes pitches before
2025-06-01); table is densely populated for active batters; the new
columns on `pick_inputs` are populated.

### Phase 3 — run the backtest, set the weight, enable the signal

Separate follow-up PR. **Requires evidence.**

- Run `diagnostics/backtest_arsenal_inputs.py` on the full 2025 backfill.
- Compare `current` vs `arsenal_blend` on AUC / top10_lift /
  quint_mono / avg_rank_hr.
- Decision rule (lifted from B6 + B12 precedent): the arsenal blend
  ships only if it wins on at least 2 of 4 metrics with no decisive
  loss on the others, validated on full season + ≥3 monthly slices.
- Set the sub-signal weight in `score_matchup` — most likely as an
  equal-mean addition (one of the 5 signals: vuln, sim, total, woba,
  arsenal), but the backtest may justify a weighted average. Whatever
  is picked goes in `WEIGHT_REFIT_LOG.md` with the supporting numbers.
- Flip `USE_ARSENAL_SUBSIGNAL = True`.
- Pin tests updated.

**Reviewer checks for Phase 3.** Backtest output attached to the PR.
Decision is documented in `WEIGHT_REFIT_LOG.md`. Sub-signal weight
is defensible.

### Phase 4 — monitor, then promote via A1 refit cycle

- Run for 14 days post-Phase-3 with the sub-signal on. Inspect:
  daily picks unchanged in the obvious-correct direction (Chas
  McCormick types ranked higher when facing a fastball-heavy starter,
  ranked lower when facing a breaking-heavy one). No HALT regressions
  in smoke tests. AUC tracking — sub-signal contribution
  should be visible in `backtest_factors.rescore_row`.
- After 14 clean days, fold into the next A1 refit cycle. At that
  point the matchup family's relative weight may need to come down a
  hair (the sub-signal makes matchup more potent), but that's a refit
  decision, not an ad-hoc weight change. Outside this design's scope.

## What this design does NOT change

- The existing pitcher-vulnerability path (HR/9 + recent HR/9 + HH%
  + K/9 + ERA + FB% allowed) is untouched.
- The existing archetype similarity path (`score_matchup_v2`) is
  untouched.
- The existing weight configurations (`WEIGHT_CONFIGS`) are untouched.
- The existing batter feature pipeline (`fetch_batter_advanced_stats`,
  `fetch_batter_recent_statcast_14d`) is untouched.

The new code adds one new ETL function, one new DB table, one new
helper, and a guarded read inside `score_matchup`. All flag-gated to
off by default.

## File touch list (Phase 1)

| Path | Change |
|---|---|
| `docs/pitch_type_archetype_design.md` | New — this file. |
| `etl/db.py` | `batter_pitch_type_splits` CREATE TABLE + ALTER block. |
| `features_v2.py` | `PITCH_TYPE_SPLIT_MIN_BB`, `fetch_batter_pitch_type_splits` skeleton. |
| `score_batters.py` | `USE_ARSENAL_SUBSIGNAL`, `_compute_xslg_vs_arsenal` (None+skip policy), guarded read in `score_matchup`. |
| `generate_picks.py` | Three `fb_slg / br_slg / os_slg` keys default to `None` on the batter dict (live + untiered + offline paths). |
| `diagnostics/backtest_arsenal_inputs.py` | New — harness skeleton. |
| `tests/smoke.py` | Eight new pin tests (incl. short-sample None+skip behavior). |

## Reference

- `pitcher_profile.py` — pitch-type classification (`FASTBALL_TYPES`
  / `BREAKING_TYPES` / `OFFSPEED_TYPES`) and arsenal building.
- `features_v2.py::fetch_batter_recent_statcast_14d` — the bulk-pull
  pattern Phase 2 will follow.
- `etl/backfill_2025.py` — the backfill-orchestrator pattern Phase 2 will follow.
- `diagnostics/backtest_power_inputs.py`,
  `diagnostics/backtest_form_anchors.py` — backtest harness patterns Phase 3 will follow.
- `docs/as_of_date_convention.md` — historical-reconstruction semantics.
