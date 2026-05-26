# Form archetype sub-signal — design

Status: **Phase 1 — schema + scaffolding (this PR)**. The sub-signal is
built behind a feature flag with weight 0 in the composite. Phases 2-4
detailed in [Rollout](#rollout-plan) below — each is a separate, reviewable PR.

## Problem statement

`score_form` (weight 0.279) today reads three recent inputs averaged in
a 0-100 mean:

- `recent_hr_10g` — HRs over the last ~10 games
- `recent_iso_30g` — ISO over the last ~30 games
- `ev_trend` — exit-velocity trend vs season (Phase 2; always `None` in
  production data today)

B11 (2026-05-26) dropped `recent_avg_30g` from this mean. The current
Form score reads the batter's _general output level_ — is he going
deep, hitting for power. It does NOT capture **whether his current
state-of-play looks like the state he was in the last few times he
went deep.**

Concrete example. A batter with a feast-or-famine power profile (think
Joey Gallo) has a "good Form" signature distinctly different from a
contact-hitter slugger (think Freddie Freeman). Both can be at the same
`score_form` value of 70 — but their _pre-HR pattern_ is different:

- Gallo's pre-HR state-of-play: high `swstr_pct`, high `barrel%`,
  moderate-to-high `xwoba`, pulled flies dominant, rest after off-days.
- Freeman's pre-HR state-of-play: lower `swstr_pct`, controlled barrel%
  with elite `xwoba`, balanced spray.

A score that captures "today, does this batter LOOK like the batter
who's about to deep" is orthogonal to "is this batter hitting well"
— and the two together should improve Form's HR-prediction lift.

## Signal definition

Each batter has a learned **state-of-play centroid** in a
contact-quality-and-discipline feature space. The centroid is computed
from the batter's own HR history: for every HR they hit in the prior
two seasons, snapshot their state-of-play in the 7-day window ending
the day before the HR. Average those snapshots into a centroid vector.

At scoring time, build today's state-of-play vector for the batter
(same features, same 7d window ending the day before today). Score the
**L2 distance** between today's vector and the centroid:

```
similarity = 1 / (1 + L2(today, centroid))     # in (0, 1]
archetype_match_score = min_max_scale(similarity, 0.20, 0.80) → 0-100
```

Anchors picked so the typical observed similarity range (.20-.80) maps
to the typical observed score range (0-100). Re-tuned in Phase 3 once
the backtest has the real similarity distribution.

A higher score means "today's state-of-play matches your past pre-HR
state-of-play closely" — interpreted as "the form you carry into deep
days." This is added as a **sub-signal of `score_form`**, not a
top-level factor — the existing 6-factor weight calibration
(power 0.250, matchup 0.264, **form 0.279**, weather 0.057, lineup 0.150)
does not change.

## Pre-HR state-of-play vector

**Critical constraint: features must NOT overlap with `score_form`'s
existing inputs.** Otherwise the centroid measures the same thing the
base Form term already scores — double-counting. Current Form's three
inputs are `recent_hr_10g`, `recent_iso_30g`, and `ev_trend`. The
archetype vector deliberately picks a different angle: **contact
quality + plate discipline + rest pattern.**

| # | Feature | Window | Rationale | In `score_form` today? |
|---|---|---|---|---|
| 1 | `recent_xwoba_14d` | 14d prior | Contact-quality reading. Different from form's `iso_30g` — xwoba includes walks and weights by expected wOBA per batted ball, capturing approach quality independent of HRs themselves. | NO |
| 2 | `recent_barrel_pct_14d` | 14d prior | Quality-contact frequency. Barrel% is the Statcast canonical "exact" measure. Independent of HRs the batter has actually hit. | NO |
| 3 | `recent_swstr_pct_7d` | 7d prior | Plate discipline / contact rate signal. Whiff-rate trending up vs down — never measured anywhere else in the model. | NO |
| 4 | `recent_pull_pct_14d` | 14d prior | Spray-direction signal. Pulled contact is the HR-relevant approach. Captures "is the batter pulling more lately" without literally counting pulled flies. | NO |
| 5 | `days_since_last_hr` | n/a | Rest pattern signal — homers cluster, and "how recently did I last go deep" is itself a state-of-play marker. | NO |
| 6 | `days_since_off` | n/a | Rest from baseball entirely. Two days off + travel produces a different state-of-play than 12 games in 12 days. | NO |
| 7 | `recent_avg_30g` | 30g prior | **Yes, this is the column B11 dropped from Form.** Including it here is deliberate: as a _state-of-play descriptor_, AVG is informative (a hot-streak hitter at .310 vs a cold-streak hitter at .200 ARE in different states); as a _direct HR predictor_, AVG was net-noise (B11's verdict). Different jobs — Form scores production, archetype reads state. | NO (dropped by B11) |

All seven features are computed in a **7-day window ending the day
before** the snapshot date (HR date for centroid construction; today
for scoring). The window choice is deliberate (next section).

### Why a 7-day window for the snapshot

- Short enough to capture _momentum_ — a 7d window samples 4-7 games
  for an everyday player. State-of-play turns over fast in baseball;
  what you were doing 21 days ago is essentially a different player.
- Long enough to be a _stable sample_ — barrel%, xwoba, swstr%, and
  pull% all have enough events in 4-7 games to land at a defensible
  number. A 3-day window would be too noisy; 14 days would smooth out
  the state-of-play signal we want.
- `days_since_last_hr` and `days_since_off` are calendar-anchored,
  not window-dependent.
- `recent_avg_30g` is unique — already a 30g window everywhere else
  in the pipeline. Keeping it at 30g here preserves direct compat
  with `pick_inputs.recent_avg_30g` and the existing Form fetch.

The 7d default is what Phase 1 ships. Phase 3 backtests sweep 7d /
14d / 21d windows to find the best lookback empirically.

### Multi-window harness

Phase 3's `backtest_form_archetype.py` sweeps **3 windows × 3 sample
thresholds** = 9 variants, plus a `default` (current Form only). The
threshold dimension is the minimum-career-HR sample-size policy
(next section). The Phase-1 default is **window=7d, min_hrs=10** —
chosen because:

- A typical position player has 15-25 HRs across two seasons. A
  10-HR floor keeps ~70% of regulars in the population while
  rejecting the noisy tail (e.g., 3-HR contact catchers whose
  centroid would be one weird HR's worth of state).
- 7d is the most state-of-play-like window per the rationale above.

## Small-sample policy — None+skip, NOT league-avg fallback

If a batter has fewer than `MIN_HRS` HRs across the lookback window
(default 10; sweep 5/10/20), `_compute_form_archetype_match` returns
`None`. The caller (`score_form`) checks for `None` and **skips the
sub-signal term entirely** — Form is scored on its base mean alone,
exactly as today.

This matches every other "skip-on-missing" convention in the codebase
(see `score_power`'s `is not None and > 0` gates, the
`compute_slate_context` <2-signal pitcher skip, the audit-MED
fix-on-league-mean across `compute_slate_context` + `score_matchup`).
**There is no `LEAGUE_AVG_ARCHETYPE` constant.** A batter with
insufficient HR history is honestly missing this signal; pretending he
has a league-average pre-HR archetype is the same provenance bug
the audit fixed.

The 10-HR sweep point lands at the natural noise floor for a 7-feature
centroid:

| min_hrs | what it filters | what survives |
|---|---|---|
| 5  | almost everyone w/ any 2-season HR history | noisier centroid, more batters scored |
| 10 | regulars + most platoon bats | balanced — Phase 1 default |
| 20 | only true everyday power threats | tight centroid, far fewer batters scored |

## As-of-date snapshotting

Same convention as every other historical input
([`docs/as_of_date_convention.md`](as_of_date_convention.md)).

```sql
CREATE TABLE batter_form_archetype (
    player_id            INTEGER NOT NULL,
    date_through         TEXT NOT NULL,        -- 'YYYY-MM-DD'; centroid computed
                                               -- from HRs strictly before this date
    window_days          INTEGER NOT NULL,     -- 7 | 14 | 21 (sweep dimension)
    feature_centroid_json TEXT NOT NULL,       -- JSON-serialized 7-element vector
    n_hrs_used           INTEGER NOT NULL,     -- count of HRs that fed the centroid
    fetched_at           TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (player_id, date_through, window_days)
);
```

- One row per `(batter, as-of date, window-days)`.
- `feature_centroid_json` stores the 7-element mean vector as JSON so
  the schema stays stable as we sweep features in Phase 3.
- `n_hrs_used` lets downstream queries filter on the sample-size
  threshold without recomputing.
- Refreshed nightly with the rest of Statcast (Phase 2) — but
  centroids move slowly (one new HR per ~5 days for a regular hitter
  in season), so a weekly refresh is also viable. Decided in Phase 2.

Reads in `score_form` look up the row where `date_through =
as_of_date - 1 day`. At noon on `D`, the most recent snapshot is
`D - 1 day` — today's games haven't happened yet, so games ON `D`
are correctly excluded.

## Risk callout — feature non-overlap with `score_form`

The archetype's value depends on it measuring something **orthogonal**
to base Form. If the centroid features overlap with the base Form
inputs, then `score_form` would effectively double-count: both the
base mean and the sub-signal would lift the same hitter for the same
underlying signal. That's silent reuse, not additional signal — the
backtest would show a deceptive lift that's really just a coefficient
shift on the existing terms.

The vector above was chosen with that constraint as the load-bearing
design check. Per-feature mapping:

| Archetype feature | Form input it might overlap with | Verdict |
|---|---|---|
| `recent_xwoba_14d` | `recent_iso_30g` (form), `xwoba_contact` (power) | Different — xwoba (over all PA) ≠ iso (extra bases on hit) ≠ xwoba on contact only (power). |
| `recent_barrel_pct_14d` | `recent_iso_30g` (form proxy), `barrel_pct` (power) | Different — recent barrel% over 14d ≠ ISO ≠ season barrel%. |
| `recent_swstr_pct_7d` | none | Plate-discipline signal, not in form OR any other factor. |
| `recent_pull_pct_14d` | `pull_fb_pct` (power) | Different — pull% (all batted balls) ≠ pull-FB% (pulled flies only). |
| `days_since_last_hr` | `recent_hr_10g` (form) | Different — count over 10g ≠ days since the most recent one. |
| `days_since_off` | `recent_window_days` (form, dampener input) | Different — rest measures elapsed days since last off-day ≠ window span. |
| `recent_avg_30g` | (none — B11 dropped it) | Free to reuse here; was never load-bearing in Form post-B11. |

**Guardrail.** A smoke test pins the non-overlap: with all
archetype-only inputs set on a batter dict and `score_form` reading
no other batter dict keys, the flag-OFF score is byte-identical to a
dict without those keys. (See `tests/smoke.py` Phase 1 pins below.)

## Score mapping

Raw similarity is L2-based: `similarity = 1 / (1 + L2(today, centroid))`,
in `(0, 1]`. Higher = closer match. Mapped to 0-100 via:

```
archetype_match_score = min_max_scale(similarity, 0.20, 0.80)
```

Anchors picked from a rough expected distribution: typical batters
should land in the 0.30-0.65 range; truly "today looks just like a
HR day" hits 0.70+. Anchors will be re-tuned in Phase 3 after the
backtest has the real distribution.

L2 is **feature-scale-sensitive**, so each feature is **z-scored**
against the league population (or the batter's own historical
range — TBD in Phase 2) before distance is computed. Without that,
`days_since_last_hr` (0-30) would dwarf `recent_swstr_pct_7d` (0-0.20)
in the L2 norm.

## Wiring into `score_form` — sub-signal vs. new factor

| Option | Pros | Cons |
|---|---|---|
| **A. Sub-signal of `score_form`** (chosen) | Refit-safe — existing 6-factor weight calibration unchanged. The archetype term slots into the mean alongside `hr10`, `iso30`, `ev_trend`. | Less granular dial. Sub-weight defined in helper, not in `WEIGHT_CONFIGS`. |
| B. New top-level factor `archetype_score` | Independent dial. | Needs an A1 refit on all 7 top-level factors. Without that, the +1 factor inflates the form family's effective weight — the un-calibrated change pattern WEIGHT_REFIT_LOG warns against. |

**Decision: sub-signal of `score_form` (Option A).** Strictly additive,
reversible — if the backtest shows the signal is noise, we set the
sub-signal weight to 0 and the composite output is byte-identical to
today.

## Implementation in this PR (Phase 1)

Strictly additive scaffolding. No production behavior changes.

### 1. DB migration — `etl/db.py`

The `batter_form_archetype` CREATE TABLE block above, plus an
idempotent ALTER pattern for existing DBs (mirrors the migration
blocks already in `create_tables`).

### 2. ETL builder — `features_v2.py`

```python
def compute_batter_form_archetype(
    player_ids: list[int],
    as_of_date: str | None = None,
    window_days: int = 7,
) -> dict[int, dict | None]:
    """
    Build per-batter pre-HR state-of-play centroid.

    Returns {player_id: {feature_centroid, n_hrs_used} | None}.
    None means "fewer than MIN_HRS HRs in lookback window" → caller
    skips the sub-signal cleanly via None propagation.
    """
```

Implementation lives in `features_v2.py`; data sources are the
existing `batter_hr_events` table (HRs over the lookback window)
plus a bulk Statcast pull for the per-HR pre-state windows.

Constants exposed:

```python
FORM_ARCHETYPE_FEATURES = [
    "recent_xwoba_14d", "recent_barrel_pct_14d", "recent_swstr_pct_7d",
    "recent_pull_pct_14d", "days_since_last_hr", "days_since_off",
    "recent_avg_30g",
]
FORM_ARCHETYPE_MIN_HRS = 10        # min HRs in lookback for centroid
FORM_ARCHETYPE_LOOKBACK_SEASONS = 2 # how many seasons of HRs to use
FORM_ARCHETYPE_DEFAULT_WINDOW = 7  # 7-day pre-HR snapshot
```

### 3. Scoring hook — `score_batters.py`

```python
USE_FORM_ARCHETYPE = False  # Phase 1 default. Flip in Phase 3 after backtest.
FORM_ARCHETYPE_SUBSIGNAL_WEIGHT = 1.0  # equal-mean with the 3 base terms

def _compute_form_archetype_match(
    today_state_vector: list[float] | None,
    batter_archetype_vector: list[float] | None,
) -> float | None:
    """Return 0-100 match score, or None if either input is missing."""
```

`score_form` reads the helper output behind the `USE_FORM_ARCHETYPE`
guard. With the flag off (default), `score_form` is byte-identical to
today's behavior — verified by a pin test.

### 4. Backtest harness skeleton — `diagnostics/backtest_form_archetype.py`

Modeled on `backtest_form_anchors.py`. Variants:

- `default` — current `score_form` only (no archetype term)
- `archetype_7d_5hr` / `archetype_7d_10hr` / `archetype_7d_20hr`
- `archetype_14d_5hr` / `archetype_14d_10hr` / `archetype_14d_20hr`
- `archetype_21d_5hr` / `archetype_21d_10hr` / `archetype_21d_20hr`

Same `auc / top10_lift / quint_mono / avg_rank_hr` metrics. Same
"common subset" filter. **Not actually runnable yet** — the SQL fetch
references `batter_form_archetype` rows that won't exist in the DB
until Phase 2. There's a guard at the top of `main()` that bails with
"Phase 1 — run after batter_form_archetype is populated."

### 5. Smoke tests

Pinned in `tests/smoke.py`:

- `pin_batter_form_archetype_table_exists` — the new table is created
  by `create_tables`.
- `pin_use_form_archetype_default_off` — `USE_FORM_ARCHETYPE`
  defaults to `False`.
- `pin_score_form_archetype_flag_off_no_op` — for a fixed batter dict
  with archetype keys set, `score_form` with flag OFF returns the
  same value as a dict without them (no-op verified).
- `pin_compute_form_archetype_match_returns_none_on_missing` —
  helper returns `None` when either input is `None` (skip semantics).
- `pin_compute_form_archetype_match_basic` — helper returns a 0-100
  score when both inputs are present.
- `pin_form_archetype_constants_present` — the new constants
  (`FORM_ARCHETYPE_FEATURES`, `FORM_ARCHETYPE_MIN_HRS`, etc.) exist
  with the documented values.
- `pin_form_archetype_no_overlap_with_form_inputs` — the feature list
  has zero intersection with `score_form`'s base inputs
  (`recent_hr_10g`, `recent_iso_30g`, `ev_trend`).

## Rollout plan

Phased intentionally. Each phase ships as its own PR. The user reviews
each before approving the next.

### Phase 1 — schema + scaffolding (this PR)

- `batter_form_archetype` table created.
- `compute_batter_form_archetype` builder implemented (callable,
  not wired to nightly ETL).
- `USE_FORM_ARCHETYPE = False`, sub-signal guarded.
- Backtest harness skeleton.
- Smoke tests pin the foundation.

**No production behavior changes.** Builder is callable but not
called by anything — no nightly ETL wiring, no automatic backfill.

**Reviewer checks for Phase 1.** Schema looks right; flag defaults
to off; smoke tests pass; no live calls; design doc covers the math
clearly; non-overlap with Form inputs documented; production scoring
is byte-identical with flag off.

### Phase 2 — populate `batter_form_archetype` for 2025 backfill

Separate follow-up PR.

- One-shot backfill script `etl/backfill_form_archetype.py` that
  walks 2025-03-27 → 2025-09-30 and writes one row per
  `(batter, date, window_days)` — for each of the 3 window-days
  sweep values (7/14/21). Same chunked, `--max-runtime` orchestrator
  pattern as `etl/backfill_2025.py`.
- Wire into nightly ETL (`etl/etl_nightly.py`) behind a `# Step N:
  form archetype` block, refreshing the table for today's batters
  at the default window (7d).
- Smoke probe: count of `batter_form_archetype` rows for the latest
  date in `pick_inputs` is non-zero.

**Reviewer checks for Phase 2.** Backfill is honest as-of-date
(spot-check: a row dated 2025-06-01 only uses HRs before that date);
table is densely populated for active batters.

### Phase 3 — run the backtest, set the weight, enable the signal

Separate follow-up PR. **Requires evidence.**

- Run `diagnostics/backtest_form_archetype.py` on the full 2025
  backfill (3×3 sweep).
- Compare `default` vs all 9 archetype variants on AUC /
  top10_lift / quint_mono / avg_rank_hr.
- Decision rule (lifted from B6 + B11 + B12 precedent): an archetype
  variant ships only if it wins on at least 2 of 4 metrics with no
  decisive loss on the others, validated on full season + ≥3 monthly
  slices.
- Set `FORM_ARCHETYPE_DEFAULT_WINDOW` and `FORM_ARCHETYPE_MIN_HRS`
  to the winning combo. Decision documented in `WEIGHT_REFIT_LOG.md`.
- Flip `USE_FORM_ARCHETYPE = True`.

**Reviewer checks for Phase 3.** Backtest output attached to the PR.
Decision is documented in `WEIGHT_REFIT_LOG.md`. Sub-signal weight
is defensible.

### Phase 4 — monitor, then promote via A1 refit cycle

- Run for 14 days post-Phase-3 with the sub-signal on. Inspect:
  daily picks unchanged in the obvious-correct direction (Gallo-type
  feast-or-famine power threats ranked higher when their pre-HR
  state-of-play matches today). No HALT regressions in smoke tests.
- After 14 clean days, fold into the next A1 refit cycle. At that
  point the form family's relative weight may need to come down a
  hair (sub-signal makes form more potent), but that's a refit
  decision, not an ad-hoc weight change. Outside this design's scope.

## What this design does NOT change

- The existing base Form path (`recent_hr_10g + recent_iso_30g +
  ev_trend` mean, post-B11) is untouched. Archetype is **additive**.
- The existing `_layoff_dampener` (kicks in at >55d window) is
  untouched.
- The existing weight configurations (`WEIGHT_CONFIGS`) are untouched.
- The existing batter feature pipeline (`fetch_batter_advanced_stats`,
  `fetch_batter_recent_statcast_14d`) is untouched.

The new code adds one new ETL builder, one new DB table, one new
helper, and a guarded read inside `score_form`. All flag-gated to
off by default.

## File touch list (Phase 1)

| Path | Change |
|---|---|
| `docs/form_archetype_design.md` | New — this file. |
| `etl/db.py` | `batter_form_archetype` CREATE TABLE block. |
| `features_v2.py` | `FORM_ARCHETYPE_*` constants, `compute_batter_form_archetype` builder. |
| `score_batters.py` | `USE_FORM_ARCHETYPE`, `_compute_form_archetype_match`, guarded read in `score_form`. |
| `diagnostics/backtest_form_archetype.py` | New — harness skeleton. |
| `tests/smoke.py` | 7 new pin tests. |

## Reference

- `pitcher_profile.py::_build_victim_profiles_from_db` — the
  DB-backed archetype-aggregation pattern this builder mirrors.
- `features_v2.py::fetch_batter_recent_statcast_14d` — the bulk-pull
  pattern the per-HR snapshot uses.
- `score_batters.py::score_form` (post-B11) — the function the
  sub-signal blends into.
- `diagnostics/backtest_form_anchors.py` — backtest harness pattern
  Phase 3 builds on.
- `docs/as_of_date_convention.md` — historical-reconstruction
  semantics.
- B11 (`claude/b11-drop-recent-avg`) — the Form base post-B11, which
  this branch is forked off.
- PR #80 (`claude/pitch-type-archetype-foundation`) — the Phase 1
  shape template (sub-signal, flag-off, design + scaffolding + harness
  + pins).
