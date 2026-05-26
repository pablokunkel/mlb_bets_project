# Park archetype signal — design

Status: **Phase 1 — design + foundation (this PR)**. The signal is built
behind a feature flag with weight 0 in the composite. Phases 2-4 detailed
in [Rollout](#rollout-plan) below — each is a separate, reviewable PR.

## Problem statement

Today's `score_park` is a handedness-weighted lookup of three numbers per
venue: `hr_pf_overall`, `hr_pf_lhb`, `hr_pf_rhb`. Within-slate percentile
rank against the day's other parks, plus a small L/R adjustment. **It says
nothing about whether *this specific batter* has historically gone deep
in parks that look like today's park.**

Concrete miss: a power-pull LHB whose career HRs are concentrated in
short-RF, low-elevation parks reads identical to a power-spray LHB at
Yankee Stadium — both are just "LHB at +28 LHB factor." Today's
score_park ranks them the same, even though the pull-LHB profile is the
better fit. The composite has no way to express "today's park looks
like the kind of park this hitter normally crushes."

The archetype signal closes that gap by building a **per-batter centroid
of "park features at the venues this batter has homered in"** and scoring
today's park by L2 distance to that centroid.

## Signal definition

```
park_archetype_match =
      1 - normalized_L2_distance(batter.centroid, today_park.features)
```

Where:

- `batter.centroid` is the (frequency-weighted, neutral-park-down-weighted)
  mean of the park-feature vectors at the venues this batter has hit
  career HRs at. **One vector per batter, refreshed nightly.**
- `today_park.features` is the same feature vector for today's venue.
- L2 distance is computed in standardized feature space (z-scored across
  all venues), then mapped 0-100 via fixed anchors. Closer to the centroid
  -> higher score.

When the batter's career HR count is below `PARK_ARCHETYPE_MIN_HRS` (threshold
swept in the harness at 5/10/20), the signal returns **None** and the
parent `score_park` skips the term cleanly — the existing handedness-weighted
park-factor logic still scores them. See [Small-sample policy](#small-sample-policy).

## Park feature vector

The user-facing design wishlist was:

> `[cf_distance, lf_distance, rf_distance, cf_height, pull_lf_factor,
> oppo_rf_factor, elevation, foul_territory_idx, roof_open/closed/retractable]`

**Most of those are not available in the existing data layer.** The constraint
on this PR is "source from existing park-factor lookups in `score_batters.py`
— DON'T introduce a new data source if existing data covers it. If a feature
isn't available, drop it from the vector and document why."

What we actually have (and use):

| Feature                  | Source                                       | Captures                                       |
| ------------------------ | -------------------------------------------- | ---------------------------------------------- |
| `hr_pf_overall`          | `park_factors.hr_pf_overall`                 | Composite park HR factor (elevation, foul terr, mean dimensions) |
| `hr_pf_lhb`              | `park_factors.hr_pf_lhb`                     | LHB-specific HR factor (proxies short RF + pull-LF for LHB) |
| `hr_pf_rhb`              | `park_factors.hr_pf_rhb`                     | RHB-specific HR factor (proxies short LF + pull-RF for RHB) |
| `lhb_advantage`          | derived: `hr_pf_lhb - hr_pf_rhb`             | Dimension asymmetry (proxies "is this a pull-LHB-friendly park?") |
| `cf_bearing_sin`         | `PARK_CF_BEARING[venue]` -> sin(deg)         | CF orientation (component for circular geometry) |
| `cf_bearing_cos`         | `PARK_CF_BEARING[venue]` -> cos(deg)         | CF orientation (component for circular geometry) |

That's a **6-element vector**, derived entirely from existing tables /
constants in `score_batters.py`. No new data source.

What we drop and why:

| Wishlist feature           | Why dropped                                                       |
| -------------------------- | ----------------------------------------------------------------- |
| `cf_distance`              | Not stored anywhere. Would need a new `park_dimensions` table.    |
| `lf_distance`, `rf_distance` | Same — no `park_dimensions` source.                              |
| `cf_height` (wall height)  | Not stored. Fenway monster / Crawford Boxes would be ideal but no source. |
| `pull_lf_factor`, `oppo_rf_factor` | Granular handed-pull splits don't exist. `hr_pf_lhb` / `hr_pf_rhb` is the closest proxy. |
| `elevation`                | Not stored as a column. `VENUE_COORDS` has lat/lng but no altitude. Coors's signature is already absorbed in `hr_pf_overall=130`. |
| `foul_territory_idx`       | Not stored. Already partly absorbed in `hr_pf_overall`.           |
| `roof_open/closed/retractable` | `daily_slate.dome` (0/1) exists but is per-game, not per-venue static. Three-way roof status isn't recorded. |

**These can be added in a follow-up `park_dimensions` ETL** if Phase 3 shows
the 6-feature vector has signal but is leaving table-pounds-on-the-table. The
B6 / B12 pattern of adding new inputs only after the first round shows lift
applies here too. The design doc commits to documenting Phase 3 dimension
asks if/when the foundation lands the lift.

### Feature standardization

Raw features mix scales (~70-130 for `hr_pf_*`, ~-50 to +50 for sin/cos
of bearing, ~-25 to +25 for `lhb_advantage`). L2 distance on raw
vectors would be dominated by the park-factor terms. Each feature is
**z-scored across the 30 MLB venues** before centroid / distance work:

```
feature_z = (feature - venue_mean) / venue_std
```

Means and standard deviations are fit once over the curated 30-park set
from `etl/park_factors_seed.py` (extended with `PARK_CF_BEARING`) and
persisted in `features_v2.PARK_FEATURE_STATS`. Refreshed annually with
the offseason calibration pass (same place park factors get refreshed).

## Per-HR weighting

**A Coors HR tells you less about the batter's park preferences than a
Petco HR.** The naive mean-of-venue-vectors centroid double-counts the
prolific-park HRs.

Each HR's contribution to the centroid is weighted by `1 /
park_neutral_hr_factor`, where `park_neutral_hr_factor = hr_pf_overall /
100`. A Coors HR contributes `100 / 130 = 0.77`; a Petco HR contributes
`100 / 92 = 1.087`. The weighted mean is:

```
centroid = sum(w_i * features_i) / sum(w_i)
```

This is the same intuition as in baseball-savant's "neutral park HR" —
you don't want a 130 park-factor venue to dominate the profile. The
weighting is symmetric (no clipping) and preserves the centroid's
existence at any sample size.

## Small-sample policy

**None + skip, not league-avg fallback.** When a batter has fewer than
`PARK_ARCHETYPE_MIN_HRS` career HRs:

1. The builder returns `None` for that batter's centroid (rather than
   filling in a league-mean vector).
2. `_compute_park_archetype_match` returns `None` when the batter's
   centroid is `None`.
3. `score_park` checks the helper's output and **skips** the sub-signal
   term — falling back to the base handedness-weighted park-factor logic
   verbatim.

Why none+skip and not league-avg fallback: league-avg vector would
double-count the base park factor signal (which `score_park` already
scores via the `hr_pf_overall` percentile rank). The whole point of the
archetype is to express "**this** batter's preference is different from
the league average." A league-avg fallback would say "the batter has no
preference, treat them like neutral" — which is exactly the same behavior
as no signal at all. None+skip keeps the existing factor honest without
inserting a no-op contribution.

The harness (Phase 2/3) sweeps `MIN_HRS` ∈ {5, 10, 20} to find the
sample-size cliff. The B6 + B12 precedent is "the gate threshold is
itself a defensible default — sweep it."

## As-of-date snapshotting

Same convention as every other historical input
([`docs/as_of_date_convention.md`](as_of_date_convention.md)). Snapshots
live in a new table:

```sql
CREATE TABLE batter_park_archetype (
    player_id            INTEGER NOT NULL,
    date_through         TEXT NOT NULL,       -- 'YYYY-MM-DD'; HRs strictly before this date
    feature_centroid_json TEXT,               -- JSON-encoded list[float] (6 elements)
    n_hrs_used           INTEGER,             -- HR count contributing to centroid
    fetched_at           TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (player_id, date_through)
);
CREATE INDEX idx_bpa_date ON batter_park_archetype(date_through);
CREATE INDEX idx_bpa_player ON batter_park_archetype(player_id);
```

- Refreshed nightly with the rest of Statcast (Phase 2).
- A backfill script populates one row per (batter, date) across the
  2025 season for the A1 refit — same chunked orchestrator pattern as
  `etl/backfill_2025.py`.

`feature_centroid_json` is a 6-element list — JSON because SQLite has no
native array type. NULL = batter had fewer than `MIN_HRS` HRs before
`date_through` (the None+skip signal).

Reads in `score_park` look up the row where `date_through = as_of_date - 1
day` (the previous completed day's snapshot). At noon on `D`, the most
recent snapshot is `D - 1 day` — today's games haven't happened yet, so
HRs on `D` are correctly excluded.

## Wiring into `score_park` — sub-signal vs new top-level factor

Two design options. Picking **sub-signal** for the reasons below.

| Option | Pros | Cons |
| ------ | ---- | ---- |
| **A. Sub-signal of `score_park`** (chosen) | Refit-safe — existing weight calibration on the 6 top-level factors (power 0.250, matchup 0.264, **park 0.000**, form 0.279, weather 0.057, lineup 0.150) doesn't change. The archetype score slots in alongside the base handedness-weighted park factor inside `score_park`'s mean. | Slightly less granular control — can't dial archetype independently of base park. Mitigated by the in-function flag + sub-weight (Phase 3). |
| B. New top-level factor `park_archetype_score` | Maximum dial-in flexibility — independent weight. | Needs an A1 refit cycle on all 7 top-level factors. Without that, the +1 factor inflates the park family's effective weight (which is currently 0). |

**Decision: sub-signal of `score_park` (Option A).** Same precedent as the
pitch-type archetype foundation (PR #80 / commit `026d756`). Adding a 7th
top-level factor would change `WEIGHT_CONFIGS["default"]` in a way that
requires its own refit, blocking the rollout behind A1. The sub-signal
approach is strictly additive and reversible — if the backtest shows the
signal is noise, we set its weight to 0 and the composite output is
byte-identical to today.

**Note on park's current weight.** `score_park` is currently weighted
0.000 in `WEIGHT_CONFIGS["default"]` — the 20-day backfit found near-zero
predictive coefficient. The archetype signal is a candidate
for bringing park back: if a per-batter archetype lifts the factor's AUC
meaningfully on the 2025 backfill, the A1 refit cycle that follows Phase
3 could revisit park's weight. That promotion is explicitly **outside the
scope of this design** — Phase 4 monitors only.

## Implementation in this PR (Phase 1)

Strictly additive scaffolding. No production behavior changes.

### 1. DB migration — `etl/db.py`

The `batter_park_archetype` CREATE TABLE block above, plus an idempotent
ALTER pattern for existing DBs (mirrors the migration blocks already in
`create_tables`).

### 2. Builder — `features_v2.py`

```python
def compute_batter_park_archetype(
    player_ids: list[int],
    as_of_date: str | None = None,
    season: int | None = None,
) -> dict[int, dict | None]:
    """Build per-batter park-feature centroids from career HRs.

    Returns {player_id: {centroid: list[float] | None, n_hrs_used: int}}.

    Reads batter_hr_events JOINed with daily_slate.venue to get the venue
    of each HR; weights each HR by (1 / park_neutral_hr_factor); centroids
    the standardized park-feature vector.

    Returns None for the centroid when a batter has < PARK_ARCHETYPE_MIN_HRS.
    """
```

Phase 1 implements the **actual logic** (not a stub) so the math can be
validated by pin tests today. Phase 2 wires it into nightly ETL.

Constants exposed for the scoring path:

```python
PARK_ARCHETYPE_MIN_HRS = 10        # Sweep in backtest harness; current default.
PARK_FEATURE_KEYS = (
    "hr_pf_overall", "hr_pf_lhb", "hr_pf_rhb",
    "lhb_advantage", "cf_bearing_sin", "cf_bearing_cos",
)
# Z-score normalization stats fit on the 30-park seed set. See
# build_park_feature_stats() in features_v2 for how this is computed.
PARK_FEATURE_STATS: dict[str, tuple[float, float]]  # {feature: (mean, std)}
```

### 3. Scoring hook — `score_batters.py` + `generate_picks.py`

In `generate_picks.py` the batter-dict assembly (`score_live_slate`,
`score_untiered_starters`, offline simulation) gets a new key
defaulting to `None`:

```python
"park_archetype_centroid": None,   # set by compute_batter_park_archetype in Phase 2
```

In `score_batters.py` a new module-level flag and helper:

```python
USE_PARK_ARCHETYPE = False  # Phase 1 default. Flip in Phase 3 after backtest.

def _compute_park_archetype_match(
    today_park_features: list[float] | None,
    batter_archetype_vector: list[float] | None,
) -> float | None:
    """Return park archetype match score (0-100), or None if either input missing.

    Computes L2 distance in standardized feature space, maps to 0-100 via
    fixed anchors (close to centroid -> 100, far -> 0).
    """
    ...
```

`score_park` reads the helper output behind the `USE_PARK_ARCHETYPE`
guard. With the flag off (default), `score_park` is byte-identical to
today's behavior — verified by a pin test.

### 4. Backtest harness skeleton — `diagnostics/backtest_park_archetype.py`

Modeled on `backtest_form_anchors.py`. Variants:

- `default` — current park-handedness only (production baseline at the
  moment Phase 3 starts).
- `archetype_5hr` — sub-signal at weight 0.5, threshold 5 HRs.
- `archetype_10hr` — same weight, threshold 10 HRs (current default).
- `archetype_20hr` — same weight, threshold 20 HRs (high-confidence
  threshold).
- `archetype_weighted_low` — threshold 10, sub-signal weight 0.25.
- `archetype_weighted_high` — threshold 10, sub-signal weight 0.75.

Same `auc / top10_lift / quint_mono / avg_rank_hr` metrics. **Bails with
"Phase 1 — run after batter_park_archetype is populated" message** until
Phase 2 lands.

### 5. Smoke tests

Pinned in `tests/smoke.py`:

- `pin_batter_park_archetype_table_exists` — the new table is created by
  `create_tables`.
- `pin_use_park_archetype_flag_default_off` — `USE_PARK_ARCHETYPE` defaults
  to `False`.
- `pin_score_park_archetype_flag_off_no_op` — `score_park` is byte-identical
  with `park_archetype_centroid` keys on/off the batter dict.
- `pin_compute_park_archetype_match_none_passes_through` — helper returns
  `None` when either input is `None`.
- `pin_compute_park_archetype_match_basic` — helper produces the expected
  L2-distance-to-score conversion.
- `pin_compute_batter_park_archetype_below_threshold_returns_none` — builder
  returns `None` centroid for batters with fewer than `PARK_ARCHETYPE_MIN_HRS`.
- `pin_park_archetype_constants` — `PARK_ARCHETYPE_MIN_HRS`, `PARK_FEATURE_KEYS`,
  `PARK_FEATURE_STATS` are exposed and well-formed.
- `pin_backtest_park_archetype_skeleton_imports` — harness skeleton imports
  with documented variants.

## Rollout plan

Phased intentionally. Each phase ships as its own PR. The user reviews
each before approving the next.

### Phase 1 — design + foundation (this PR)

- `batter_park_archetype` table created.
- `compute_batter_park_archetype` implemented (not stubbed).
- `park_archetype_centroid` key on the batter dict (default `None`).
- `USE_PARK_ARCHETYPE = False`, sub-signal guarded.
- Backtest harness skeleton.
- Smoke tests pin the foundation.

**No production behavior changes.** No new Statcast load. Nothing
hits the new table.

**Reviewer checks for Phase 1.** Schema looks right; flag defaults to
off; smoke tests pass; no live calls; design doc covers the math
clearly; production scoring is byte-identical (verified by pin test).

### Phase 2 — populate `batter_park_archetype` for 2025 backfill

Separate follow-up PR.

- Wire `compute_batter_park_archetype` into nightly ETL
  (`etl/etl_nightly.py`) behind a `# Step N: park archetype` block,
  refreshing the table for today's batters. Production starts populating
  here.
- One-shot backfill script `etl/backfill_park_archetype.py` that walks
  2025-03-27 -> 2025-09-30 and writes one row per (batter, date). Same
  chunked, `--max-runtime` orchestrator pattern as `etl/backfill_2025.py`.
- `pick_inputs.park_archetype_centroid_json` added by an idempotent ALTER
  in `etl/db.py`.
- Backfill writes the centroid (JSON-encoded) into `pick_inputs` for
  every 2025 row.
- Smoke probe: count of `batter_park_archetype` rows for the latest date
  in `pick_inputs` is non-zero.

**Reviewer checks for Phase 2.** Backfill is honest as-of-date (spot-check:
a row dated 2025-06-01 only includes HRs before 2025-06-01); table is
densely populated for active batters; the new column on `pick_inputs` is
populated.

### Phase 3 — run the backtest, set the weight, enable the signal

Separate follow-up PR. **Requires evidence.**

- Run `diagnostics/backtest_park_archetype.py` on the full 2025 backfill.
- Compare all 6 variants on AUC / top10_lift / quint_mono / avg_rank_hr.
- Decision rule (lifted from B6 + B12 precedent): the archetype blend
  ships only if it wins on at least 2 of 4 metrics with no decisive
  loss on the others, validated on full season + ≥3 monthly slices.
- Set the sub-signal weight in `score_park` based on whichever variant
  wins. Whatever is picked goes in `WEIGHT_REFIT_LOG.md` with the
  supporting numbers.
- Flip `USE_PARK_ARCHETYPE = True`.
- Pin tests updated.

**Reviewer checks for Phase 3.** Backtest output attached to the PR.
Decision is documented in `WEIGHT_REFIT_LOG.md`. Sub-signal weight is
defensible.

### Phase 4 — monitor, then promote via A1 refit cycle

- Run for 14 days post-Phase-3 with the sub-signal on. Inspect: daily
  picks unchanged in the obvious-correct direction (pull-LHB hitters
  ranked higher at short-RF parks). No HALT regressions in smoke tests.
  AUC tracking — sub-signal contribution should be visible in
  `backtest_factors.rescore_row`.
- After 14 clean days, fold into the next A1 refit cycle. At that
  point park's top-level weight may need to lift off 0 (the sub-signal
  makes park more potent), but that's a refit decision, not an ad-hoc
  weight change. Outside this design's scope.

## What this design does NOT change

- The existing handedness-weighted park-factor logic in `score_park`
  (`hr_pf_lhb` / `hr_pf_rhb` lookup, slate-percentile rank, ±50 L/R
  adjustment) is untouched. The archetype score is **purely additive**
  when the flag is on.
- The existing weight configurations (`WEIGHT_CONFIGS`) are untouched.
- The existing batter feature pipeline (`fetch_batter_advanced_stats`,
  `fetch_batter_recent_statcast_14d`, `fetch_batter_pitch_type_splits`)
  is untouched.
- `park_factors_seed.py` and the `park_factors` table are read-only from
  this code path; we don't extend them.

The new code adds one new ETL builder, one new DB table, one new helper,
and a guarded read inside `score_park`. All flag-gated to off by default.

## File touch list (Phase 1)

| Path                                       | Change                                                      |
| ------------------------------------------ | ----------------------------------------------------------- |
| `docs/park_archetype_design.md`            | New — this file.                                            |
| `etl/db.py`                                | `batter_park_archetype` CREATE TABLE block.                 |
| `features_v2.py`                           | `PARK_FEATURE_KEYS`, `PARK_FEATURE_STATS`, `PARK_ARCHETYPE_MIN_HRS`, `build_park_feature_vector`, `compute_batter_park_archetype`. |
| `score_batters.py`                         | `USE_PARK_ARCHETYPE`, `_compute_park_archetype_match`, guarded read in `score_park`. |
| `generate_picks.py`                        | `park_archetype_centroid` key defaults to `None` on the batter dict (live + untiered + offline paths). |
| `diagnostics/backtest_park_archetype.py`   | New — harness skeleton.                                     |
| `tests/smoke.py`                           | 8 new pin tests.                                            |
| `BACKLOG.md`                               | Park-archetype entry under Active queue with current phase. |

## Reference

- `pitcher_profile.py::_build_victim_profiles_from_db` — the
  DB-backed-archetype builder pattern this design mirrors.
- `docs/pitch_type_archetype_design.md` — same Phase-1 shape (sub-signal,
  flag-gated, schema-only, backtest-skeleton-bails). This park archetype
  is the parallel build for `score_park`.
- `etl/backfill_2025.py` — the backfill-orchestrator pattern Phase 2 will follow.
- `diagnostics/backtest_form_anchors.py`,
  `diagnostics/backtest_power_inputs.py` — backtest harness patterns
  Phase 3 will follow.
- `docs/as_of_date_convention.md` — historical-reconstruction semantics.
