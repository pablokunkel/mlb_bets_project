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

## Mobile UI cleanup pass (2026-05-23 session)

Annotated screenshot pass on `mobile_edits.pdf` (user-supplied 2026-05-23). Mobile-first cuts and polish across every tab plus a Hitters-tab rebuild on top of the diagnostic heatmap. Pure presentation work — no scoring, ETL, or worker changes — safe to land in parallel with the model-factor sequence above. Split into four batches by risk/scope so each can ship and roll back independently.

### M1. Batch A — pure cuts (lowest risk)

**Status.** Ready to start.

**Scope.**
- Today's Picks header: cut PICKS card and AVG COMPOSITE card. Keep BOARD SIZE + EXPECTED HRs.
- Today's Picks rows: cut tier badges (T1/T2/T3/T4 chips). Tier qualification (B5) still filters server-side; the visible badge is vestigial.
- Big Board header: cut SHOWING and SELECTED cards. Fix the center divider line. Remove gridlines from the stats block.
- Big Board advanced filters: remove the Barrel % and ISO sliders under "Underlying skill · minimum" — the score sliders already capture contact quality.
- Lab cards: remove the descriptive paragraph under each card title (Homerun Leaders, Power × Matchup, Hot Streak Watch, Park × Pitcher Exploit, Pure Longshots, Game Stacks). The card title + scoreboard tells the story.
- Game-detail modal: remove the "Composite Distribution" histogram on mobile (`@media (max-width: 768px)`). Keep on desktop.
- HR Recap subtitle copy: change `"22 batters went deep this day"` → `"22 Dingers"`.

**Files.** `mlb_hr_bet_site/index.html` only. Touch points map (from 2026-05-23 exploration):
- Today's Picks stat cards: `renderToday()` ~line 2774-2782.
- Tier badges: `tierBadge()` ~line 2551 and the row render ~line 2823.
- Big Board stats: `renderBoard()` ~line 4465-4474.
- Big Board filter sliders: HTML ~line 2156-2169.
- Lab descriptions: ~line 3110-3149 in `renderLab()`.
- Composite Distribution: `#railHistogram` block in `renderBoard()` ~line 4617-4654.
- HR Recap subtitle: `_renderRecapDay()` ~line 5348.

**Done when.** All cuts land, no console errors, visual diff on mobile + desktop in screenshots.

### M2. Batch B — mobile-aware sort + filter restructure

**Status.** Ready to start. Depends on Batch A landing first to avoid merge churn on the same regions.

**Scope.**
- Big Board: replace the "All teams" dropdown with an "All games" dropdown (e.g. `WAS @ ATL`, `LAD @ SF`). User confirmed: replace (not add). Same filter-state field, different source list and label rendering.
- Big Board: add a "Season" option to the HR window control (currently Last/Off + 7d/14d).
- Big Board: when a user sorts by a factor column on mobile, that column becomes visible in the row even though desktop shows all columns. Track which column is "currently sorted" and reveal it on the mobile expandable row.
- Today's Slate rail (`renderSlateRail`, ~line 4579): convert to a dropdown at the top of the Big Board tab on mobile. Desktop keeps the rail.
- HR Recap "HR Hitters · {date}" table: sort by `our_comp` instead of `hr_count`. Fix the `${h.hr_count}⚾` formatting — the emoji should have proper spacing/alignment (currently mashed against the number).

**Files.** `mlb_hr_bet_site/index.html`.
- Filter row: `#boardTeamFilter` ~line 2108, filter state `_boardFilters` ~line 4380-4390.
- HR window: HTML ~line 2171-2184, state field `_boardFilters.hrWindow`.
- Mobile sort column: media query at line 627, sort cycle `cycleBoardSort(field)` at line 4687.
- Slate dropdown: `renderSlateRail(cache)` line 4579.
- HR Recap table: sort + emoji format `_renderRecapDay()` line 5326, HR cell ~line 5394.

**Done when.** Big Board filter is per-game on mobile + desktop, HR window has Season, mobile sort reveals the sorted column, slate is a dropdown on mobile, HR Recap rows are sorted by comp with clean `⚾ N` formatting.

### M3. Batch C — game modal + Lab polish + History/Performance formatting

**Status.** Ready to start.

**Scope.**
- Game-detail modal (`openGameModal` line 5767):
  - Investigate why `VEGAS TEAM TOTAL` is always N/A in the modal. Trace from `export_site_data.py` through the per-game JSON. Either fix data wiring or remove the field if it's not coming.
  - Add an "Avg matchup score" (or similar) line to the Pitching Matchup section. Computed as the mean `matchup` factor across the visible batters in the game (already in the modal's data).
  - Cap "TOP N BATTERS IN THIS GAME" at 5 instead of 25. Keep the `OUR RANK` column so users still see global rank.
- Lab tab:
  - "Game Stacks" stack-score formatting: round to one decimal max, add a colored stat-pill treatment so it doesn't read as a free-floating number. Render at `${s.score.toFixed(1)}` ~line 3072.
  - Hot Streak Watch (`renderLab`-driven view): single-digit scores look misleading. User said: **replace the score column with the composite score** for now; defer the underlying calc question to another session. Keep the rank.
- History tab Pick-Rank Heat Map (`renderHistory` line 3170, heat-map render line 3223-3247): cap the visible date columns at **Last 10** instead of the current full window. Clean up the baseball-emoji `⚾` formatting inside cells (line 3239) — proper spacing and consistent rendering with text.
- Performance tab backtest tables (`renderPerformance` line 3438, factor tables ~line 3390): same formatting cleanup as History — cap dates at Last 10, fix `⚾` formatting.

**Files.** `mlb_hr_bet_site/index.html`, possibly `export_site_data.py` for the vegas-team-total wiring.

**Done when.** Vegas team total either populated or removed; pitching matchup carries an avg score; game modal lists top 5; stack-score is a clean stat; Hot Streak shows composite; History + Performance show last 10 dates with proper emoji rendering.

### M4. Batch D — Hitters tab → diagnostic heatmap (restyled)

**Status.** Ready to start. This is the implementing form of existing item **C1** ("Heatmap as a dashboard tab — replace the Hitters tab"); on landing, mark C1 shipped.

**Scope.** Replace the entire Hitters tab body with the diagnostic heatmap from `diagnostics/batter_ab_heatmap.html`, restyled to match the site's clay/cream/Inter aesthetic. All filters and functionality of the standalone heatmap are kept; only the visual treatment changes.

**Data path.** User confirmed: regenerate `heatmap.json` daily. Add a `build_heatmap_payload()` function reusing `diagnostics/batter_ab_heatmap.py::build_dataset()` query logic, called from `export_site_data.py` so the noon pipeline drops a fresh `heatmap.json` next to `picks_latest.json` and friends. The dashboard fetches it on tab open.

**Restyle checklist.**
- Drop the dark `--bg:#0e1116` palette; map to the site's `--bg / --paper / --surface / --border / --ink / --signal` variables.
- `.card` → reuse site's stat-card treatment (cream surface, statbook border).
- `.ctl` (filter rail) → match `.board-controls` styling — sticky top, light surface, same border-radius and padding.
- `.cell` glyphs: replace `#dfe6f0 / #ffd23f / #5c6675` with `--ink / --signal / --ink-muted`.
- Heatmap ramp: keep the 6-stop blue→amber→red gradient (it's information-bearing), but warm the cool end toward the site's blues.
- `.modal` → reuse the existing `#gameModal` chrome instead of the heatmap's own modal styling. Single shared modal pattern across the dashboard.
- Sticky table headers: keep, but recolor with `--paper-2 / --border`.
- Fonts: replace the system stack with Inter throughout; use JetBrains Mono only for the per-cell numeric glyphs.

**Files.**
- `mlb_hr_bet_site/index.html` — port the heatmap HTML + CSS + JS into `#panel-hitters` (line 2250). Delete the existing `renderHitters()` Hot Sheet table.
- `export_site_data.py` — new `heatmap.json` export step, reusing `diagnostics/batter_ab_heatmap.py::build_dataset()`.
- `diagnostics/batter_ab_heatmap.py` — refactor `build_dataset()` to be importable (currently a script) without breaking the standalone tool.
- `run_daily.bat` / pipeline orchestration — confirm the new export runs in the daily flow.

**Risks.**
- Standalone HTML is ~5 MB because it inlines the dataset. The JSON-fetch version will still be large — verify the payload is acceptable on mobile (consider trimming per-cell `in` / `hrs` arrays, or lazy-loading the per-cell modal data).
- Sticky-header z-index interactions with the site's tab nav need a real browser check.

**Done when.** The Hitters tab on the live site shows the restyled heatmap, refreshed by the noon pipeline, with all standalone filters working. C1 in the backlog gets marked shipped.

### M5. HR Recap header reshape (cuts + hit-rate trio)

**Status.** Ready to start. Independent of A-D but ships easiest with Batch A.

**Scope.** Replace the four-card HR Recap header (DAYS TRACKED / HR HITTERS / WE PICKED / CAPTURE RATE) with a three-card hit-rate trio. User decision 2026-05-23: keep hit rate, drop the rest, show it three ways:
- **Season hit rate** — % of season HR events caught by our daily top-N.
- **14d hit rate** — same metric on the last 14 days.
- **Top-50 hit rate** — % of HR events whose hitter was in our top 50 by composite that day (a wider net signal).

**Files.** `mlb_hr_bet_site/index.html` (`renderHRRecap` line 5285, stat cards line 5303-5308). `export_site_data.py` if the 14d / top-50 cuts aren't already in `hr_leaderboard.json` — compute them in the export step alongside the existing capture rate so the dashboard stays a thin renderer.

**Done when.** HR Recap header shows three hit-rate cards, all computed from existing outcomes data, refreshed by the pipeline.

### M6. HR Recap "Recent picked days" → batter Last X Games

**Status.** Ready to start. Pairs naturally with M5 since both touch `renderHRRecap`.

**Scope.** The current per-batter card on HR Recap shows the last 11 dates on which we picked that hitter (DATE / VS / COMP / RESULT / LINE columns). User wants instead: the batter's **last 7-14 actual games**, regardless of whether we picked them, so a user can see recent hitting form / HR cadence at a glance. Reuses `outcomes` data already loaded by the pipeline.

**Spec.**
- Window: prefer 14 if the outcomes-cumulative join makes it cheap, else 7. Confirm during build.
- Columns: DATE / VS (opponent abbrev) / AB / H / HR. Maybe an "OUR PICK" star/badge column to retain the prior signal (we picked him that day) without spending a whole column on it.
- Sort: most recent at top.
- Render: replace the existing recent-picked-days table in `_renderRecapDay()` at line 5326 with the new last-games table.

**Files.** `mlb_hr_bet_site/index.html` (`_renderRecapDay` line 5326). `export_site_data.py` if the per-batter game log isn't already in `hr_leaderboard.json` — emit a `last_games` array per hitter.

**Done when.** Each HR-Recap day's batter card shows the hitter's last 7-14 games with hit / HR signal, sortable by date, with our-pick callouts preserved.

---

## Model factor review & heatmap (2026-05-19/20 sessions)

A factor-by-factor audit of the 6-factor composite, plus tooling/data carry-forwards. **Form** and **Matchup** are done — PRs **#56** (`form-factor-rebuild`) and **#57** (`matchup-vulnerability-fix`). The 2026-05-20 scoring audit (`docs/scoring_audit_2026-05-20.md`) added B8/B9/B10 — see those entries for the audit findings they wrap.

**Sequencing (post-2026-05-20):**
- **Phase 1 (must land first):** B8 (outcomes-cumulative season_hr + pick_inputs column).
- **Phase 2 (independent, parallel-shippable):** B5, B7, B9, B10.
- **Phase 3 (depends on B8):** B6 (Power rebuild), B4 (pitcher recency tighten — parallel-ok).
- **Phase 4 (depends on all above):** A1 (weight refit).
- **A1–A4 are gated** as documented per-entry; A1 specifically requires B6 + B8 + B9 (not just #56/#57 anymore).
- **B1–B3 and C1–C3 are independent** — self-contained branches; can run in parallel in separate chats.

Background context: `CLAUDE.md` ("Current work" section), the #56/#57 PR descriptions, `docs/scoring_audit_2026-05-20.md`, and the diagnostic tool `diagnostics/batter_ab_heatmap.py`. The earlier "recent singles/doubles" idea is already folded into the rebuilt `score_form` (the 30-game AVG term) — no separate item.

> Method note for the factor reviews (B1–B3): same approach that worked for Form and Matchup — decompose every input the factor uses, trace where each value actually comes from, check for proxies / hard caps / mislabels / paths that disagree, verify it recalculates and matches the DB, then write findings + a fix PR.

### A1. Refit composite weights after the Form + Matchup changes

**Status.** Gated — blocked until **B6 + B8 + B9 land** AND ~1–2 weeks of pipeline runs accrue on the new code. Previously gated on #56 + #57 only; widened 2026-05-20 after the scoring audit (`docs/scoring_audit_2026-05-20.md`) revealed that `backtest_factors.rescore_row` has never been able to apply the season-HR floor (no `hr` column in `pick_inputs` — finding #3). That means every refit since 2026-05-03 (when the floor went on) was calibrated against backtest data that scored *without* the floor while production *has* it. Refitting now would inherit the same divergence.

**Why it matters.** `WEIGHT_CONFIGS["default"]` (power 0.250, matchup 0.264, park 0.000, form 0.279, weather 0.057, lineup 0.150) was logistic-regression-fit on the *old* Form and Matchup inputs **and** on backtest data that under-applied Power. #56 replaced Form's inputs wholesale (recent HR/ISO/AVG on new game-count windows, vs the old capped-barrel + SLG-delta proxies); #57 changed Matchup's vulnerability input set (added FB%) and redistributed the rookie bonus; B6 will rebuild Power (recent quality-contact + smooth floor curve). The 0.279 / 0.264 / 0.250 coefficients now sit on inputs that are about to change again. The composite is mis-weighted until refit.

**Working assumption (2026-05-20).** Do **not** assume the current weights are "approximately right." Backtest-vs-production divergence on the floor means we cannot estimate the bias without first removing that divergence (B8 finding 2). Treat the refit as a from-scratch calibration on clean data.

**Spec.** Refit via `refit_weights.py`. Prerequisites in this file's Open Action Items: (#1/#5) `raw_data.csv` does not auto-extend — wire the nightly CSV append, or refit directly off `pick_inputs` in the DB; (#2) `refit_weights.py`'s hardcoded baseline is stale. Resolve those first or as part of this. The refit needs post-B6/B8/B9 `pick_inputs` rows with the new columns populated — hence the data-accrual gate.

**Files.** `refit_weights.py`, `score_batters.py` (WEIGHT_CONFIGS), `WEIGHT_REFIT_LOG.md`, possibly `run_outcomes.bat`.

**Done when.** New weights fit on post-change data, logged in `WEIGHT_REFIT_LOG.md`, `WEIGHT_CONFIGS["default"]` updated.

### A2. Phase 2 — real exit-velocity trend (nightly Statcast ETL)

**Status.** Gated on #56 (needs the `ev_trend` column it adds).

**Why it matters.** #56's `score_form` has a 4th input slot — `ev_trend`, a real exit-velocity trend — wired with skip-on-missing and currently always None. Real EV is contact-quality signal that recent ISO/AVG don't fully capture. The model once had a real EV path (`try_fetch_statcast_recent` in `generate_picks.py`) but it is dead code: per-player `statcast_batter` calls hung the noon run (the 2026-04-29 incident). The current game-log feed is box-score only — no EV.

**Spec.** Compute rolling EV in the **nightly** ETL (it already runs 15–25 min of Statcast, off the noon critical path) — not at noon. Per batter: pull recent batted-ball Statcast (`launch_speed`), average it over a window (~last 10–15 games), store it; the trend = recent EV − season EV. Populate `pick_inputs.ev_trend`; `score_form` activates the term automatically once it is non-NULL. Revisit the `min_max_scale(ev_trend, -3, 3)` anchor in `score_form` against real data.

**Files.** `etl/etl_nightly.py`, `etl/db.py` (rolling-EV store), `generate_picks.py` (enrich `ev_trend` onto the batter dict), `score_batters.py` (anchor).

**Done when.** `ev_trend` is non-NULL on new `pick_inputs` rows and reflects recent-vs-season EV; the 4-input `score_form` is live.

### A3. Form-rename consumer cleanup

**Status.** Gated on #56.

**Why it matters.** #56 added honest new `pick_inputs` columns and stopped writing the old `recent_*_14d` proxies. Two consumers still read the old columns and go NULL/stale for new rows: `export_site_data.py` (the Form factor-decomposition input list + the column→factor map, plus a Big Board `recent_hr_14d` field feeding a dashboard filter) and `factor_diagnostics.py` (which *re-implements* the old proxy — `min(25, recent_iso_est*100)` — so it needs a logic rework, not just a rename).

**Spec.** `export_site_data.py`: swap `recent_hr_14d / recent_barrel_pct_14d / ev_trend_14d` → `recent_hr_10g / recent_iso_30g / recent_avg_30g` in `factor_inputs["form"]` (~line 672) and the column→factor map (~line 889); for the Big Board field (~line 178) decide whether to keep the JSON key (renaming ripples to `index.html`'s filter). `factor_diagnostics.py`: rework its form section to the new windows, or import `score_form` directly.

**Files.** `export_site_data.py`, `diagnostics/factor_diagnostics.py`, possibly `mlb_hr_bet_site/index.html`.

**Done when.** Dashboard Form decomposition reads the new columns; `factor_diagnostics` reflects the rebuilt form.

### A4. Matchup v1 consolidation

**Status.** Gated on #57.

**Why it matters.** Two loose ends from the Matchup decomposition not fixed in #57. (1) v1 `score_matchup` keeps its own inline vulnerability fallback — only hr9 + hh, a 2-input third path beyond the slate-percentile path and the now-5-input `score_pitcher_vulnerability`. (2) v1 adds a flat +10 platoon bonus for opposite handedness; v2 adds 0 (intentional — handedness is inside archetype similarity), so v1- and v2-scored batters aren't on the same scale.

**Spec.** Make v1 `score_matchup` call `score_pitcher_vulnerability` instead of its inline 2-input block — one vulnerability function across both versions. For platoon: v1's `woba_vs_hand` already carries handedness; decide if the +10 is double-counting and should be removed to match v2 (likely yes).

**Files.** `score_batters.py` (`score_matchup`), possibly `pitcher_profile.py`.

**Done when.** One shared vulnerability function for v1/v2; consistent platoon handling.

### B1. Power factor review

**Status.** Ready to start — independent.

**Why it matters.** Next factor in the sweep (weight 0.250). Strong prior suspicion: `barrel_pct` / `exit_velo` from `season_batting` are *synthetic estimates* (`barrel ≈ hr_per_pa×200`, `ev ≈ 82 + slg×15`) — `barrel_pct_source` is only ever `synthetic_hr_per_pa` / `season_batting_fallback` / None, never `statcast`. So Power may run on synthetic contact-quality data exactly as Form did. `pull_fb_pct` is known-NULL on the daily path. The season-HR floor (5→50, 8→60, 12→70, 18→78, 25→85) keys off `season_batting.hr`, which the heatmap showed undercounts vs `outcomes` (Buxton 11 stored vs 13 actual) → mis-tiers hitters.

**Spec.** Decompose every Power input — barrel%, exit velo, HR/FB%, ISO, xwOBA-on-contact, pull-FB% — plus the season-HR floor: trace each value's source, real vs synthetic, caps/proxies, whether it recalculates daily. Verify against the DB. Findings + a fix PR.

**Files (review).** `score_batters.py` (`score_power`, `compute_season_hr_floor`), `etl/etl_nightly.py` (`sync_season_batting`), `generate_picks.py` (`enrich_with_season_batting`), `fetch_daily_data.py`, `features_v2.py`.

**Done when.** Every Power input graded real/synthetic/bug; fixes specced or PR'd.

### B2. Weather factor review + empirical correlation decomposition

**Status.** Ready to start — independent.

**Why it matters.** Weather (weight 0.057) — flagged (with Park) as a poor predictor. The explicit question: does any weather input we pull (temperature, wind, humidity, dome) actually correlate with HR, or are we pulling stats that don't carry HR signal? Two hypotheses — bad ingestion (believed working) vs the stats genuinely not mattering as wired.

**Spec.** (1) Decompose `score_weather` — the temperature piecewise curve, the handedness-aware wind logic (cosine of wind-to-CF angle × speed), humidity, dome handling. (2) Empirical decomposition: bucket every game-cell by temperature band / wind band / humidity band / dome, compute the actual HR rate per bucket — from `pick_inputs` weather columns ⨝ `outcomes`, and/or the `historical_calibration` table (2024–25 weather backfill, bigger sample). Grade each input: does HR rate move across its buckets? A flat input is dead weight.

**Files (review).** `score_batters.py` (`score_weather`, `score_temperature`, wind logic), `etl/wind_utils.py`, `etl/etl_morning.py`, `etl/historical_calibration.py`. The dashboard's existing Temp×Humidity / Wind diagnostics and the heatmap tool are useful references.

**Done when.** Each weather input graded against empirical HR rate; a keep / re-source / drop recommendation per input.

### B3. Park + Lineup factor review

**Status.** Ready to start — independent.

**Why it matters.** The two remaining small factors. Park (0.000 in the weighted average + a 0.05 additive bonus) — flagged weak; the `park_factors` table is a hardcoded seed, never refreshed live. Lineup (0.150) — the batting-order→score curve (1→85 … 9→38) and how often `batting_order` is NULL / `roster_fallback`.

**Spec.** Decompose `score_park` + `score_lineup_position`. Park: is the seed park-factor data accurate / worth refreshing from Savant? Does the additive +0.05 bonus behave as intended? Lineup: are the AB-per-position assumptions current; how often does `batting_order` fall back. Findings + any fix.

**Files (review).** `score_batters.py` (`score_park`, `score_lineup_position`), `etl/park_factors_seed.py`, `etl/etl_nightly.py` (`sync_park_factors`).

**Done when.** Both decomposed, data verified, fixes specced.

### B4. Tighten pitcher-recency window in Matchup

**Status.** Ready to start — independent (depends on `pitcher_recent` ETL output already wired into Phase 1 Matchup).

**Why it matters.** Surfaced during the 2026-05-20 low-score diagnosis. Burger faced Kyle Freeland on 5/20 with `pitcher_hr_per_9 = 1.9` (season) but `recent_hr9_21d = 3.46` (recent — 83% spike). The current Phase 1 implementation blends `RECENT_HR9_BLEND_WEIGHT = 0.60` against the season number, giving an effective HR/9 of ~2.8 — directional but smoothed. Matchup score still only 67.9; the hitter went yard. The 21-day window pulls in starts that are already stale once a pitcher has clearly turned. User's framing: **"recency bias for matchups"** — go further than the current blend.

**Spec.** Replace the fixed 21-day pitcher window with a **last-N-starts** window (N=5 candidate; test N=3, 7). Re-weight the blend (current 60/40 → candidate 70/30 or pure-recent when N starts present). Look at whether to apply the same logic to `pitcher_era_recent` and `pitcher_k9_recent`, not just HR/9. Backtest deltas before shipping. Probably feature-flagged.

**Files.** `etl/pitcher_recent.py` (window definition), `pitcher_profile.py` (`score_pitcher_vulnerability` + `compute_slate_context`), `score_batters.py::RECENT_HR9_BLEND_WEIGHT`.

**Done when.** Last-N-starts window implemented behind a flag, backtest run on the last 30 days vs current 21d/60-40 blend, decision documented in `WEIGHT_REFIT_LOG.md`.

### B5. Tier qualification filter — `2026 games > 0 OR in today's lineup`

**Status.** Ready to start — independent. Small PR.

**Why it matters.** Surfaced 2026-05-20: Blaine Crim ranked #6 by Power with bo=bench despite having zero 2026 games (released by his team). `build_live_tiers` in `fetch_daily_data.py:659` falls back to a 2025 backfill window and qualifies any player with `games ≥ 5 AND hr ≥ 1` regardless of season. Crim qualified on his 2025 line (20 g / 5 HR) and got pulled into today's slate scoring.

A naive fix — "require 2026 games > 0" — would lock out true rookies and IL-returnees on their first day back. The user's framing: **combined filter.** Keep a player in the qualification pool if EITHER condition holds:
- has `season_batting season=2026 games > 0`, OR
- appears in today's `daily_lineup` (any tier — posted, recent, or roster fallback)

**Spec.** Modify `build_live_tiers` to apply the combined filter when selecting which players enter the tier ranking. Players who pass on the "in_lineup" branch but have no 2026 history will likely score low (no inputs) — that's fine and intended; the IL/scratch filter (B7) catches the inverse case where they have history but aren't playing.

**Files.** `fetch_daily_data.py::build_live_tiers`, possibly `generate_picks.py::main` if the lineup lookup needs to happen before tier build.

**Done when.** Crim and analogous prior-season-only players disappear from 2026 slates; a confirmed rookie or recently-activated player still appears the moment their name posts in `daily_lineup`.

### B6. Power Phase 1 rebuild — recent quality-contact blend + smooth HR-floor curve

**Status.** Gated on B8 — the floor curve must read `season_hr` from outcomes (not from the lagging MLB API). Mirrors the Form rebuild shape (PR #56).

**Note (2026-05-20).** B6c (the original "off-by-one" sub-finding) is resolved by B8: the Burger 8-HR-floor bug was not a tier-loop off-by-one but the MLB API HR lag flowing through `b["hr"]`. Once B8 wires `season_hr` from `outcomes` into the batter dict, the existing `compute_season_hr_floor` works correctly; B6 just replaces the cliff with a smooth curve.

**Why it matters.** Surfaced 2026-05-20 low-score diagnosis. Two problems hit at once:

1. **`score_power` has no recent quality-contact input.** It reads season-aggregate `barrel_pct`, `exit_velo`, `hr_fb_pct`, `iso`, `xwoba_contact`, `pull_fb_pct` from `season_batting`. A slow-starter-now-hot hitter (Alec Bohm: 4 HR season, but hot last 2 weeks) gets dragged by his stale season aggregate. Bohm scored Power=4.0 on 5/20 with real recent production the model is blind to.
2. **The Season-HR floor is a 5-step cliff with a calibration off-by-one.** `SEASON_HR_FLOOR_TIERS` in `score_batters.py:505` defines floors at 5/8/12/18/25 HR. Burger (8 HR) showed Power=50.0 on 5/20 — should be 60 (the 8-HR floor). Either `season_hr` isn't reaching `score_power` correctly for the live tier path, or there's an off-by-one in the tier selection. The cliff structure also means a 7-HR hitter gets 50 and an 8-HR hitter would get 60 (10pt jump for one extra HR) — discrete, not smooth.

**Spec (two sub-changes, ship together behind a feature flag, backtest before flipping on):**

**B6a — Recent quality-contact blend.** Add `recent_barrel_pct_14d`, `recent_xwoba_contact_14d`, `recent_iso_14d` to the inputs available to `score_power`. Same skip-on-missing pattern as the Form `ev_trend` slot — if absent, the season inputs carry the score; if present, they participate in the mean. Wire a real (cached, batched) Statcast pull through `etl/etl_nightly.py` or `etl/etl_morning.py` to populate the new columns on `pick_inputs`. **Do not** revive the per-player `statcast_batter()` calls that hung the noon pipeline on 2026-04-29; use a single bulk Savant fetch per slate, same pattern as `fetch_daily_data._fetch_season_batting_splits`.

**B6b — Smooth HR-floor curve.** Replace the 5-step `SEASON_HR_FLOOR_TIERS` with a continuous curve. **Default candidate: log-based, no cap** — `floor = c * ln(season_hr + 1)`, with `c` calibrated so 18 HR → 78 (matches current Schwarber outcome). At c=26.5 this gives ~43 at 4 HR (Bohm), ~58 at 8 HR (Burger fix), ~78 at 18 HR, ~91 at 30 HR. Backtest a `sqrt`-based variant as comparator. Whichever wins on 30-day rank-correlation-with-HR backtest data ships; document the call in `WEIGHT_REFIT_LOG.md`.

**B6c — Investigate the 8-HR floor mismatch.** While the floor logic is being rewritten, trace why Burger's 8 HR didn't trigger the 60-tier floor on 2026-05-20 — is `season_hr` being read from `batter.get("season_hr")` or `batter.get("hr")`, and does the live-tier path populate either? This may or may not survive the B6b rewrite, but document the root cause.

**Files.** `score_batters.py` (`score_power`, `SEASON_HR_FLOOR_TIERS`, `compute_season_hr_floor`), `etl/etl_nightly.py` or `etl/etl_morning.py` (recent Statcast pull), `etl/db.py` (new `pick_inputs` columns), `load_picks_to_db.py`, `generate_picks.py` (assemble recent stats into the batter dict in all 3 paths: tiered live, untiered, offline sim — mirror the PR #56 fix that missed `fetch_form_data_batch`).

**Done when.** Bohm-class slow-starter-now-hot hitters get Power > 30 when their recent Statcast supports it. Burger's 8 HR floors cleanly to ~58 (log) without a cliff. Schwarber sits near current 78 (log calibration preserved). Backtest deltas documented; flag flipped on after WEIGHT_REFIT_LOG decision.

### B7. IL / scratch filter — replace lineup-fallback rows with roster-status data

**Status.** Ready to start — scoped in detail during 2026-05-20 session. 2–3 hour build estimate.

**Why it matters.** Surfaced 2026-05-20: Ryan Jeffers ranked #2 by Form on 5/20 despite being placed on the IL before 5/19's game. Daily lineup fell back to "recent:2026-05-19" (when he was active) and the model had no way to know he wouldn't play. The posted-lineup feed catches every absence once posted, but ~60% of the 5/20 slate (229 of 392 batters) was on fallback lineups before posting — the residual window where IL'd / suspended players slip through.

**Spec (V1, expected to ship as one PR):**

1. **DB migration** (`etl/db.py`):
   - Add `lineup_source TEXT` column to `daily_lineup` (already computed in `fetch_daily_data.py:283` but never persisted — see `etl/etl_morning.py:291` INSERT).
   - New table `daily_player_status (date, player_id, status_code, status_description, is_likely_out INTEGER, source, fetched_at)`.

2. **New fetcher** in `fetch_daily_data.py`: `fetch_team_roster_status(team_id, date_str)` — calls `/teams/{team_id}/roster?rosterType=fullRoster&date={d}`, returns `{player_id: {status_code, status_description}}`. ~30 calls per slate (one per team). Use the `home_team_id`/`away_team_id` already returned by the lineup hydrate (currently thrown away after the fallback decision; persist into `daily_slate` and we're set).

3. **New ETL step** (`etl/etl_morning.py` Step 2.5, after lineups): walk `daily_lineup`, for each Tier 2/Tier 3 fallback-sourced row look up the player's status, write to `daily_player_status` with `is_likely_out = (status_code != 'A')`. Posted-lineup rows (Tier 1) are not overridden — the team posted them, trust it.

4. **Filter in `generate_picks.py`**: when assembling `eligible_batters` (~line 1114), keep `is_likely_out=1` rows in the scored output but set `selected=0`. Preserves the diagnostic record (Jeffers stays on the big board with his Form=82.1 + an `IL` badge); top-8 promotes ranks 9+. **Do not zero out composite** — that would break the "composite ≈ HR probability" invariant.

5. **Site changes**:
   - Big board: add a small status badge column reading `status_description` ("10-day IL", "Bereavement", etc.) wired from `daily_player_status`.
   - Top-8 card: filters `is_likely_out=1` rows out of the eligible pool before the rank-8 cut.
   - Heatmap (`diagnostics/batter_ab_heatmap.py` and the future tab in C1): same badge, no behavior change to the cells themselves.

**Decisions baked into V1 (all confirmed):**
- Filter scope: **Tier 2/3 fallback rows only.** Don't override a posted lineup.
- Status threshold: **`status_code != 'A'`.** Covers IL + Paternity + Bereavement + Suspended + Restricted with one rule.
- Top-8 handling: **omit (`selected=0`), don't zero composite.** Diagnostic record preserved.
- Late same-day scratches (1pm injury news after a 9am picks publish): **out of scope for V1.** Manual-scratch dashboard button + late-afternoon rerun parked for V2.

**Optional enhancement to consider during the build.** When the filter promotes a rank-9+ player into the top-8, log it (e.g., `daily_picks.promoted_due_to='il_filter'`). This is *not* to validate the filter itself (filtering an IL'd player is strictly ≥ keeping them — they have 0 ABs). It's a **calibration audit** lever: did the promoted player hit at a comparable rate to original top-8 picks, or does the model's rank 8↔9 boundary capture less signal than we think? Useful retrospectively, doesn't change V1 behavior either way.

**Files.** `etl/db.py`, `fetch_daily_data.py` (new `fetch_team_roster_status`), `etl/etl_morning.py` (new Step 2.5), `generate_picks.py` (eligibility filter), `load_picks_to_db.py`, `export_site_data.py`, `mlb_hr_bet_site/index.html` (badge column), `diagnostics/batter_ab_heatmap.py` (badge).

**Done when.** Jeffers and analogous IL'd players appear on the big board with an `IL` badge but don't make the top-8 card. Bench/IL detection works on the day-of for any player whose status the MLB roster API reflects by morning.

### B8. Pre-B6 prereqs — outcomes-cumulative `season_hr` + `pick_inputs.season_hr` column

**Status.** Ready to start — must land before B6. From the 2026-05-20 scoring audit (`docs/scoring_audit_2026-05-20.md`, findings #1 + #3).

**Why it matters.** Two convergent problems both fix here:

1. **MLB API HR-aggregate lag.** `fetch_daily_data._fetch_season_batting_splits` calls `/api/v1/stats?stats=byDateRange` (line 537-548). The endpoint **lags HR totals by ~3 days** while updating the games count immediately. Direct API replay on 2026-05-20 confirmed: `endDate=2026-05-17/18/19` all returned Burger HR=7, even though he hit his 8th on 5/17 and `outcomes` recorded the event. `endDate=2026-05-20` finally returned HR=8. That lagged HR count flows `_splits_to_batters.b["hr"]` → batter dict → `score_power`'s `season_hr` lookup → `compute_season_hr_floor` lands one tier too low. On 2026-05-20: Burger (8 HR) scored Power=50 instead of 60. Aranda, Dingler same. Jacob Young (5 HR) scored 4.0 — floor didn't fire at all. Two 12-HR batters scored 60 (should be ≥70).

2. **`backtest_factors.rescore_row` can never apply the floor** because `pick_inputs` has no `hr` or `season_hr` column (verified via `PRAGMA table_info(pick_inputs)`). Every backtest re-score returns `base_score`, while production has the floor applied. This is a silent backtest-vs-live divergence affecting **every weight refit since 2026-05-03** when `USE_SEASON_HR_FLOOR` flipped on. A1 cannot refit cleanly until this is fixed.

**Spec.**

1. **DB migration** (`etl/db.py`): add `season_hr INTEGER` column to `pick_inputs`. Idempotent ALTER TABLE in the migration block (same pattern PR #56 used for the new Form columns).

2. **Cumulative-HR helper.** In `generate_picks.py` (or a small utility module), add a function that takes a list of `batter_id`s + a date string and returns `{batter_id: int_hr_total}` via a single batched query:
   ```sql
   SELECT batter_id, SUM(hr_count) AS season_hr
   FROM outcomes
   WHERE date >= ? AND date < ?
   GROUP BY batter_id
   ```
   Where the lower bound is the season opener (`2026-03-27` or detect-from-data) and the upper bound is the scoring date (strict-less-than: cumulative through yesterday). Reuse the pattern from `compute_lab_accuracy.py:120`. Batters not in the result default to 0.

3. **Wire into all three batter-dict assembly paths in `generate_picks.py`**:
   - Live tiered (~line 1192-1208): set `entry["season_hr"] = season_hr_lookup.get(player_id, 0)`.
   - T4 untiered (~line 1324-1346): set `stub["season_hr"] = season_hr_lookup.get(player_id, 0)`.
   - Offline sim (~line 1479-1493): set `entry["season_hr"] = season_hr_lookup.get(player_id, 0)`.

4. **Make `score_power` prefer `season_hr` over `hr`.** Currently (`score_batters.py:603-605`):
   ```python
   season_hr = batter.get("season_hr")
   if season_hr is None:
       season_hr = batter.get("hr")
   ```
   This already falls back correctly. **Verify** that once `season_hr` is set on the dict, the fallback path is no longer exercised. Optionally remove the `hr` fallback after a week of confidence.

5. **Persist `season_hr` to `pick_inputs`.** `load_picks_to_db.py` INSERT needs to include the new column. Source: the same `season_hr` value already on the batter dict (don't re-compute).

6. **Update `backtest_factors.rescore_row`** to read `season_hr` from the `pick_inputs` row and set it on the rebuilt batter dict. Once this lands, set `USE_SEASON_HR_FLOOR=True` is consistent across backtest and live.

**Files.** `etl/db.py`, `generate_picks.py` (helper + 3 assembly paths + load_picks call site), `load_picks_to_db.py`, `backtest_factors.py`, `score_batters.py` (verify only, optional cleanup).

**Done when.**
- `pick_inputs.season_hr` exists and is populated nightly.
- Burger / Aranda / Dingler score Power=60 instead of 50 on a fresh run (assuming season_hr ≥ 8).
- `backtest_factors.rescore_row` produces the same Power score as the live `score_power` for the same `pick_inputs` row.
- The MLB-API-lag dependency is severed from the floor logic.

### B9. T4 untiered stub enrichment — `hr`, `bats`, real `games`

**Status.** Ready to start — independent of B8 mechanically but conceptually paired (both about making the batter dict complete before scoring). From the 2026-05-20 audit (findings #2, #4, #5).

**Why it matters.** `score_untiered_starters` (`generate_picks.py:1324-1346`) builds T4 stubs with only `name, team, player_id, _lineup_source`. `enrich_with_season_batting` writes Statcast proxies but not `hr`, not `bats`, and only writes `games` when the `season_batting` row has it. Three downstream bugs:

1. **`hr` never set** → `score_power` floor never fires for T4. Evidence on 2026-05-20: Kurtz (8 HR, T4) scored Power=33.5; Rooker (7 HR, T4) scored 26.3; Greene (4 HR, T4) scored 21.9. None lifted to their qualifying floor.
2. **`bats` never set** → `compute_composite` (`score_batters.py:1076`) defaults to `"R"` → every LHB or switch T4 batter gets wrong park handedness skew and wrong platoon bonus.
3. **`games=None` when no season_batting row** (true rookies, just-recalled minors) → `_platoon_dampener(games, None)` returns 1.0 → true rookies get the **full daily-starter multiplier** while real platoon hitters with 30 games get dampened. Inverse of intent.

**Spec.**
- Extend `load_season_batting_lookup` in `generate_picks.py:164-172` to SELECT `hr`, `bats`, `games` in addition to current columns.
- In `score_untiered_starters` stub assembly (~line 1324-1346), copy these into the stub: `stub["hr"]`, `stub["bats"]`, `stub["games"]` (None if absent).
- After B8 lands: also set `stub["season_hr"]` via the same outcomes-cumulative lookup B8 introduces — this is the correct fix for T4 floor application, regardless of `season_batting.hr`.
- Confirm `_platoon_dampener` treats `games=None` consistently — if it should no-op (current behavior), that's fine; if it should treat None as "untiered, assume sub-platoon," adjust.

**Files.** `generate_picks.py::load_season_batting_lookup`, `generate_picks.py::score_untiered_starters`, possibly `score_batters.py::_platoon_dampener` for None handling.

**Done when.** T4 batters with ≥5 season HR floor cleanly. Every T4 LHB / switch hitter has `bats` from `season_batting` (or carries `None` rather than silently-defaulted `"R"`). `games` is populated wherever possible and explicit-None where not.

### B10. Audit cleanups — TBD pitcher, weather fallback, smaller items

**Status.** Ready to start — independent. Bundle of low-effort fixes from the 2026-05-20 audit (findings #6, #10, plus the deferred small items).

**Why it matters.** Each item alone is small; bundled they're a clean PR. None blocks B6 but all are real bugs.

**Sub-items.**

1. **"TBD" pitcher silently becomes `LEAGUE_AVG_PITCHER`.** `generate_picks.py:1165-1167` — if `slate["pitchers"].get(team)` is the literal string `"TBD"` (or missing), the matchup gets scored against a league-average synthetic pitcher with no provenance flag. Batters facing an unannounced starter get league-average credit that may be far from reality. **Fix:** detect `"TBD"` (and missing) → mark `selected=0` and stamp `_pitcher_source="tbd"`; don't score the batter against the league-avg synthetic.

2. **Two weather scoring paths can disagree** on partial weather dicts. `compute_slate_context:217-220` requires `temp + wind + humidity` all non-None to enter the slate `weather_pct`. But `score_weather`'s fallback path (`score_batters.py:991-998`) imputes missing fields (`weather.get("temperature_f", 68)`, etc.) and produces a score regardless. A game with only `temp+wind` (humidity NULL) skips the slate percentile but gets fallback-scored on imputed humidity. **Fix:** make `score_weather` skip-on-missing — if any of `temp/wind/humidity` is None, return 50 with `_weather_source="partial"` rather than imputing.

3. **`score_matchup` v1 `min(100, base + bonuses)` truncates the rookie bonus** when base is already 90+. `score_batters.py:699` and `pitcher_profile.py:761`. Rookie bonus (+15) and platoon (+10) become non-additive at the ceiling. **Fix:** apply bonuses BEFORE the final scale, not as additive at the end. Or accept and document — the comment trail is missing either way.

4. **`LEAGUE_AVG_PITCHER.hr_per_9 = 1.2` vs comment "real 2026 ~1.27"** (`score_batters.py:455-464`). One-line update + comment refresh.

5. **`PARK_CF_BEARING.get(venue, 0)` defaults to 0° for non-mapped venues** (`score_batters.py:59-91`). Add a defensive log/raise if a new venue surfaces; don't silently score wind against centerfield-bearing-of-due-north.

6. **`score_lineup_position` None=35 vs "bench"=15** — 20pt gap on the absence-vs-bench boundary (`score_batters.py:840, 843`). Low production impact (None is rare on the live path) but consistency improvement: make None=15 or both=25 — pick one.

**Files.** Per sub-item above; mostly `score_batters.py` + `generate_picks.py`.

**Done when.** TBD pitcher games visibly flagged and not scored against synthetic. Weather scoring skips on partial data. Smaller items resolved and documented.

### C1. Heatmap as a dashboard tab — replace the Hitters tab

**Status.** Ready to start — independent. **Implementing form lives in M4** (Mobile UI cleanup pass) which adds the brand restyle scope on top of this. Land via M4; mark this shipped on M4's PR.

**Why it matters.** The batter × game heatmap built this session (`diagnostics/batter_ab_heatmap.py`) is a strict superset of what the dashboard's **Hitters tab** already does — the Hitters tab carries a basic 14-day HR-hitter × day scoreboard (`.hitters-innings-table`, fed by `hr_leaderboard.json`). The heatmap adds every batter (not just recent HR hitters), the full season, heat-shading by composite / board rank / any factor / rank-within-game, result glyphs, click-through to full per-game model detail (every input), row grouping, a date filter, and headline calibration cards. Putting it on the public dashboard gives it the same diagnostic lens used all session.

**Spec.** Replace the Hitters tab's body with the heatmap (keep the tab; consider renaming it "Heatmap"). The standalone tool embeds its whole dataset inline in one generated HTML — for the site it must be **pipeline-fed**: (a) add a `heatmap.json` export to `export_site_data.py`, reusing the query logic in `batter_ab_heatmap.py:build_dataset()` (batters / dates / cells); (b) port the heatmap's CSS + JS into `index.html` as the Hitters-tab panel, reading `heatmap.json`; (c) delete the old Hitters scoreboard; (d) watch payload size — the standalone HTML is ~4.8 MB; trim the per-cell `in` / `hrs` detail or lazy-load the modal data if the JSON is too heavy for a web tab. `diagnostics/batter_ab_heatmap.py` stays as the standalone diagnostic / reference implementation.

**Files.** `mlb_hr_bet_site/index.html` (new tab panel + CSS + JS), `export_site_data.py` (`heatmap.json` export), the daily export step.

**Done when.** The dashboard's Hitters tab shows the live heatmap, refreshed daily by the pipeline.

**Note.** `diagnostics/batter_ab_heatmap.py` + `.html` are currently uncommitted (in the worktree and the main checkout). Decide whether to also commit / PR the standalone tool — it's a useful diagnostic regardless of the tab work.

### C2. Data hygiene — '???' teams and duplicate `daily_picks` identities

**Status.** Ready to start — independent.

**Why it matters.** Surfaced while building the heatmap. (1) `season_batting.team = '???'` for ~20 Athletics players (Langeliers, Kurtz, Rooker, …) — the team didn't resolve in `sync_season_batting`. `daily_picks` has the correct `OAK`; the live dashboard isn't affected (export reads team from `daily_picks`); but the source data is wrong. (2) `daily_picks` has stray rows where a player appears under a `"Lastname, F"` name with a full-name team (e.g. `"Butler, L"` / `"Athletics"` next to `"Lawrence Butler"` / `"OAK"`) — a fallback ingestion path creating duplicate identities. Neither breaks scoring (team is not a scoring input) but both pollute joins and diagnostics.

**Spec.** Trace `sync_season_batting`'s team resolution — likely an abbreviation-map miss for the Athletics' current code; fix the map. Trace the `"Lastname, F"` rows to the ingestion path that emits them (a roster fallback) and dedupe / normalize names there.

**Files.** `etl/etl_nightly.py` (`sync_season_batting`), `normalize_team_names.py`, `fetch_daily_data.py` (lineup/roster fallback); possibly a one-off cleanup of existing rows.

**Done when.** No `'???'` teams in `season_batting`; no duplicate player identities in `daily_picks`.

### C3. Investigate the Apr 17–26 model scoring blackout

**Status.** Ready to start — independent.

**Why it matters.** The heatmap shows a ~10-day band (2026-04-17 → 2026-04-26) with **no model scores at all** — `daily_picks` has zero rows for those dates, though games were played and HRs were hit. ~485 of the analysis window's HRs landed on un-scored days, a large share of them in this blackout. A pipeline that can silently emit zero output for 10 days is a real reliability gap.

**Spec.** Determine what happened 4/17–4/26 — pipeline not running (this pre-dates the GitHub Actions migration?), a crash, a data-source outage. Check `etl_log`, the `.github/workflows` git history, any run logs. Then add a guard: the pipeline should fail loudly — not silently no-op — when it produces zero picks on a day that has games.

**Files.** Investigation: `etl_log`, `.github/workflows/`, `generate_picks.py`. Fix: a zero-picks guard in `generate_picks.py` or a workflow check.

**Done when.** Root cause documented; a loud-failure guard exists.

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
