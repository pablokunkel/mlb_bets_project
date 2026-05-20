# Backlog

Project queue for MLB HR Bets. Each item is scoped enough that a future session (or future-you on a cold context) can pick it up without re-reading prior conversations. Last updated 2026-05-20.

> **For the model behavior, see `How_The_HR_Model_Works.md`. For the deploy / release process, see `DEPLOY.md`. For component map and DB tables, see `ARCHITECTURE.md`. For monthly weight-refit decisions, see `WEIGHT_REFIT_LOG.md`.**

## How to use this file

- **Active queue** is roughly priority-ordered. Pull from the top. If you're picking up a non-top item, jot a quick "why I jumped order" in the PR.
- **Parked** items are real but not actionable yet (waiting on data, waiting on design, blocked on something external).
- **Open action items** are smaller carry-forwards from prior sessions that didn't fit into a PR at the time.
- **Recently shipped** is a short rolling log so you can tell at a glance what's already done. Trim the tail when it gets past ~6 weeks.

When an item ships, move it from "Active queue" to "Recently shipped" with the PR number. When an item gets parked or unparked, move it across sections.

---

## Active queue

### 1. Today's Picks scoreboard overhaul (Wrigley green-board, Option A)

**Status:** queued. Design reference confirmed via screenshot mock 2026-05-06 (Wrigley scoreboard photo with a green board overlay).

**Why it matters.** The current Today's Picks card is a horizontal row layout that crams composite, factor pills, and metadata into a tight strip. It's compact but visually generic. The Wrigley overlay treatment makes the picks feel like a manually-cranked scoreboard — distinctive, on-brand for a baseball product, and gives each factor visible weight via column real estate. The user has explicitly chosen Option A over Option B (compact-with-amped-aesthetic) — they want the full board treatment.

**Spec.** Single rectangular green "board" overlaid on a Wrigley scoreboard photo. Each pick is one row across the board. Columns and sub-info per the user's mock:

```
RANK | BATTER | COMP | POWER | MATCHP        | FORM | WEATHR    | PARK
 1   | A.JUDGE| 99.9 | 100.0 | 67.3          | 99.9 | 67.3      | 99
                                (L. SEVERINO)         (60 SUNNY) (YANKEE STADIUM)
```

Each numeric cell is the factor score (0-100). Sub-info under MATCHP is the opposing pitcher name; under WEATHR is the temperature + condition; under PARK is the venue name. The whole thing sits inside a scoreboard-style green panel with white slot-style typography (cream / off-white, NOT bright white — match the Wrigley palette).

Existing Today's Picks card has the data already; this is a presentation-layer rebuild only. The data is in `picks_latest.json` → each pick already has `composite`, `power`, `matchup`, `form`, `weather`, `park`, `pitcher_name`, `weather_summary`, `venue`. Nothing new to compute.

**Open design questions (defer to next session, don't pre-resolve).**
- Background image: ship the Wrigley photo as an asset, or render the scoreboard frame purely in CSS? The mock used a real photo — cleaner visual but a ~50-100KB asset. Probably ship the photo.
- How do we handle 8 picks vs. fewer? Mock shows 5 empty rows below the populated 1. Static height (8 fixed slots, blank-tinted when picks < 8) is more "scoreboard." Dynamic height collapses the empty rows. Static feels right.
- Mobile: the 8-column layout will be cramped on phones. Considerations: collapse to a 2-column layout (rank+batter+comp on the left, factor scores stacked on the right), OR horizontal scroll within the board. Static-board-with-scroll is probably best since it preserves the metaphor.
- Cashed flag treatment: how does the green background tint (PR #42) interact with the green Wrigley board? Probably needs a different cashed treatment for this view — maybe a yellow flag tag in the RANK column, or a "💰" overlay at the side.

**Files likely touched.**
- `mlb_hr_bet_site/index.html` — most of the work. New CSS for the scoreboard frame + JS to render the rows.
- Maybe `mlb_hr_bet_site/data/wrigley.jpg` (or similar) for the background photo.
- Possibly `picks_latest.json` if we decide the dashboard needs additional fields (it shouldn't — everything is there).

**Risks.**
- Iterative CSS work has a way of eating context. Time-box it: if the first cut is ugly, ship it and refine via follow-up PRs.
- The existing horizontal-row layout has accreted decorations over time (cashed flag, stars, expansion arrows for click-to-expand). Make sure the new layout still surfaces all of those signals, or explicitly drop the ones we don't need for Option A.

**Source.** User-driven design. Confirmed via screenshot mock attached 2026-05-06. Original framing in the audit/handoff conversation 2026-05-05 ("scoreboard overhaul (#44 next)").

### 2. ~~Big Board column expansion + click-to-sort headers~~ — SHIPPED PR #62 (2026-05-20)

Moved to "Recently shipped" section. Picked up ahead of #1 because #1's design reference (the screenshot mock) wasn't accessible in the implementing session.

### 3. ~~Slate-driven worker rollover~~ — SHIPPED PR #46 (2026-05-06)

Moved to "Recently shipped" section.

### 4. Split slate AM/PM

**Status:** queued. Needs a small design decision before building.

**Why it matters.** When MLB has a doubleheader-heavy day (e.g., a Saturday with 8 day games + 8 night games), the model treats it as one 16-game slate. Weather, lineups, and pitchers can differ meaningfully between the day card and the night card. Picks for the night slate are diluted by morning data and vice versa.

**Open design decision (the user has the call here).**
- **Option A.** One picks card with two visible sections (AM / PM). Selection rule still applies across both (top 8 with no more than 2 per game). Same `run_daily.bat`, same model run.
- **Option B.** Two separate runs of `run_daily.bat` per day — one at ~10am ET to score the day card, one at ~3pm ET to score the night card. Two independent picks files. Dashboard becomes session-aware.
- **Hybrid.** One run, one picks card, but split the top-8 quota into top-4-AM + top-4-PM so the user gets balanced exposure across both windows.

Option A is the smallest change. Option B is the most accurate but doubles the pipeline cost (~10 min × 2). Hybrid is in between but creates an artificial constraint that probably hurts pick quality.

**Spec is deliberately incomplete** — pick a design first, then spec.

**Files likely touched.** Depends on the design; could be small (Option A: just a UI section break) or large (Option B: a second pipeline run + scheduler config + JSON schema split).

**Risks.** Doubleheader days are rare enough (~1-2% of slates) that this is a polish item, not a critical fix. Could stay parked.

**Source.** User mention 2026-05-05 handoff.

### 5. Implied probability + book odds calibration

**Status:** parked until ~2026-05-19 (need 14 clean post-lineup-fix days for calibration).

**Why it matters.** The composite score (0-100) is a model-internal ordinal. It's not directly comparable to a betting probability. To answer the question "what HR probability does our composite=70 imply?" we need to calibrate composite-to-HR-rate against live outcomes. Once calibrated, we can compare to DraftKings/FanDuel HR prop odds and surface "+EV" picks where the model says the implied probability is >X% but the book is offering Y% (lower). That's the actual betting-decision shape of the product.

**Why parked.** The lineup bug fix landed 2026-05-04 (PR #32 + #33). Pre-fix data is contaminated (~17% live hit rate during the bug window). We need 14 days of clean post-fix data to calibrate against. 2026-05-04 + 14 = 2026-05-18, so unparking on or after 2026-05-19 is safe. Earlier than that and the calibration table is dominated by pre-fix noise.

**Spec.**
- Pull HR prop odds from the-odds-api.com (already used for game totals).
- Per-batter "book implied prob" = American odds → implied probability (`100 / (odds + 100)` for positive, `|odds| / (|odds| + 100)` for negative).
- "Model implied prob" computed from a calibration curve fit on (composite, hit_hr) pairs from the last 14 days.
- Surface picks where `model_prob > book_prob + threshold` (e.g., 2 percentage points) as "+EV picks."
- Dashboard column or tab.

**Files likely touched.**
- New `etl/etl_odds.py` to pull HR prop odds.
- `score_batters.py` to compute model_prob via the calibration curve.
- `export_site_data.py` to surface in JSON.
- Dashboard JS to render.

**Risks.**
- Sample size on individual-batter HR props is THIN. Calibration may be noisy at the per-pick level even with 14 clean days. Plan to start with population-level calibration (all picks, not per-batter) and refine if the data supports it.
- the-odds-api.com pricing tier may not include HR props (verify before scoping).
- Anti-correlation with the-odds-api.com cache: HR props move during the day. Refresh cadence matters.

**Source.** User mention in audit/handoff 2026-05-05; appeared on the queue with a blocking date.

---

## Parked

### Platoon detection (vs the soft dampener already shipped)

**Status:** parked. Hard to do well without a real "expected lineup" forecast model.

**What we have today.** PR #28 shipped a soft platoon dampener — a multiplicative `[0.90, 1.0]` haircut keyed off `games / slate_max_games` (play-rate proxy). It catches the broad case of "this batter is part-time" but doesn't know WHY (true platoon split, defensive backup, manager hunch).

**What this would be.** A real platoon detection layer that:
- Identifies batters who are statistically platooned (e.g., R/L splits with >100 PA per side, OR start rate vs. opposing handedness).
- Predicts whether they'll be in tonight's lineup BEFORE the lineup is posted (so the model isn't blind during the morning ETL).
- Adjusts pick selection to skip a likely-out-of-the-lineup batter even when their composite is high.

**Why parked.** Without a real "expected starter probability" forecast model trained on lineup history, this is hand-wavy. The soft dampener already addresses the worst case (pure platoon hitter scoring as if daily-starter). Going further requires data infra we don't have (lineup-history time series per player + opposing pitcher handedness) and a small model on top.

**Source.** User mention 2026-05-05; flagged as "worth thinking about; hard to do without a real expected-lineup forecast model."

### Live tracker session window — late-game edge case

**Status:** parked. Mostly resolved by the slate-driven worker rollover (PR #46).

**What's left.** PR #46 fixes the midnight rollover problem by anchoring the live feed to the published slate date. This solves both the cashed-flag persistence issue and the late-game attribution issue described in the ARCHITECTURE.md known-debt section. There's one remaining edge case: a late-night game on Sunday-night-baseball that goes 14 innings finishing at 2am ET. With the slate-driven rollover, that HR is correctly logged against Sunday's slate. The only failure mode left is the cosmetic "Live (5/4 slate)" tag during the Monday morning before noon — which is intentional and tells the user accurately what they're looking at.

**If we want to fully close it,** the spec would be: rather than rolling at the next `run_daily.bat`, roll at "last-out-of-the-latest-game-on-slate D plus a buffer of N hours." Slightly more accurate but adds complexity and the current behavior is already correct. Leaving parked unless a real bug emerges.

**Source.** Audit 2026-05-05; PR #46 description.

---

## Open action items (carry-forwards from prior sessions)

### From `WEIGHT_REFIT_LOG.md` (2026-05-01 + 2026-05-03 entries)

1. **Wire a job that appends each completed day's `daily_picks ⨝ outcomes` rows into `raw_data.csv`.** Until this lands, monthly refits are no-ops because the training data window doesn't extend past 2026-04-15. Two options: append nightly via `run_outcomes.bat` after `etl_outcomes` succeeds, OR refit `refit_weights.py` directly off the SQLite DB instead of CSV. The DB-direct path is cleaner architecturally but requires touching the refit script. The CSV-append path is mechanical (one new step in `run_outcomes.bat`).

2. **Update `refit_weights.py`'s hardcoded `current_default` baseline (lines 161-167) to mirror the actual shipped `WEIGHT_CONFIGS["default"]`.** Currently it uses v1_learned weights, so the reported `+1.25 pp lift_vs_current` is really lift-vs-v1, not lift-vs-shipped. Future refit comparisons aren't apples-to-apples until this lands.

### From `DEPLOY.md` Followup section

3. **Delete `NETLIFY_AUTH_TOKEN` and `NETLIFY_SITE_ID` from GitHub repo Settings → Secrets and variables → Actions.** These are dead secrets from the pre-Cloudflare deploy path. Status unverified — needs dashboard access.

4. **Configure CF Workers Builds to NOT trigger on every branch push.** Currently every PR branch push runs a build (harmless, just noisy). Restrict to `main` only via the CF dashboard. Status unverified — needs CF dashboard access.

(The `cloudflare/workers-autoconfig` branch deletion item from DEPLOY.md is already resolved — verified via `git ls-remote origin` 2026-05-06.)

### From `ARCHITECTURE.md` Known architectural debt

5. **`raw_data.csv` auto-extension** — same item as #1 above. Listed twice because both docs flag it.

6. **Live tier estimates feeding `season_batting`.** `etl_nightly.sync_season_batting` populates `season_batting` from synthetic Statcast estimates (`barrel_pct ≈ hr_per_pa × 200`, etc.), so `enrich_with_season_batting`'s "fallback" is sometimes one synthetic value replacing another. Real Statcast values from FanGraphs / Savant would need a separate ETL pipeline. Tracked but no scoping yet.

---

## Recently shipped

(Newest first. Trim entries past ~6 weeks.)

### 2026-05-20

- **PR #62** — Big Board: expand to 8 sortable factor columns (RANK · BATTER · COMP · POWER · MATCHP · FORM · WEATHR · PARK). Each numeric header is click-to-sort with a 3-state desc → asc → reset cycle and ▲/▼ indicator. Reuses the existing filter-drawer sort state so headers and drawer stay in lockstep.

### 2026-05-06

- **PR #46** — Slate-driven worker rollover: live feed anchored to last published slate. Fixes cashed-flag persistence past midnight + late-game attribution. Replaces calendar-midnight rollover with `slate_date.json`-driven rollover.
- **PR #45** — Docs: refresh infra (run_daily steps, live-hr deploy, lineup pipeline). Updated ARCHITECTURE.md + DEPLOY.md to current state.
- **PR #44** — Docs: refresh model behavior (anchors, floor, park bonus, lineup endpoint). Updated How_The_HR_Model_Works.md + WEIGHT_REFIT_LOG.md to current state.
- **PR #43** — Hot Streak rework: top 10 by 7d HR sorted by matchup×park×weather.
- **PR #42** — Cashed-row contrast v3: solid green tint + bright text accents.

### 2026-05-05

- **PR #41** — Live Today: count unique HR hitters, not total HR events. Fixed dedup on Cashed count too.
- **PR #40** — Postponed/cancelled/suspended game filter in generate_picks (originating case: 2026-05-05 NYM/COL rainout).
- **PR #39** — Profile cache TTL bumped 24h → 3d (saves 2-3 min per noon run).
- **PR #38** — `run_daily.bat` zombie-Python reaper at startup.
- **PR #37** — Live HR worker: skip STATE writes when cursor/doneFinal unchanged.

### 2026-05-04

- **PR #34** — Lineup source visibility: persist + display where batting_order came from.
- **PR #33** — Lineup recent-lineup fallback (prior 14d) before alphabetical-roster fallback.
- **PR #32** — CRITICAL: lineups were alphabetical-roster, not batting order. Switched primary endpoint to `statsapi schedule?hydrate=lineups`. Live hit rate went from ~17% during the bug window back to ~37% post-fix.
- **PR #31** — CI: auto-deploy `dingersonly-live-hr` worker on push to main.
- **PR #30** — T4 names: pull player_name + games in load_season_batting_lookup.

### 2026-05-03

- **PR #28** — Platoon dampener: soft `[0.90, 1.0]` composite haircut for non-daily starters.
- **PR #27** — Lab tab: 'Homerun Leaders' view (top 10 HR → top 3 by non-power composite).
- **PR #26** — Rookie pitcher matchup bonus: +15 for batters facing < 300 career pitches.
- **PR #25** — Scoring tweaks: power re-anchor + park additive bonus + season-HR floor default-on.
- **PR #24** — Fix T4-Untiered display: use fullName + team abbreviation.
- **PR #23** — `run_daily.bat`: pull origin/main BEFORE running picks (not after).
- **PR #22** — Live HR worker: skip no-op KV writes via content fingerprint.
- **PR #21** — Rename pick_inputs.vegas_implied_total → vegas_team_total_pct + add raw column.
- **PR #20** — Schema cleanup: mode + handedness + provenance columns.
- **PR #29** — Lab tab: 'Pure Longshots' view.
- **PR #18** — Fix picks blockers + self-heal yesterday's HR recap by noon.
- **PR #17** — Backtest harness: compare USE_CAREER_PRIOR × USE_SEASON_HR_FLOOR combos.

### 2026-05-02

- **PR #15** — Lab tab: 💰 cash-bag highlight on alternate views.
- **PR #14** — Lab tab: 4 alternate views to shrink the picks denominator.
- **PR #13** — Picks tab: cash-bag highlight.
- **PR #12** — Live HR worker CPU fix.

(Earlier PRs: see `git log --oneline` and the closed PRs on GitHub.)
