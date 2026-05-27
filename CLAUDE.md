# CLAUDE.md — MLB HR Bets

> **If you are here to plan, scope, design, or prioritize new work — STOP.**
> Task definition lives in a dedicated PM chat. Route the user there.
> This file only governs sessions that are SHIPPING a task already briefed
> by the PM chat. If the user opens you to "figure out what to do next" or
> "audit the codebase" without a brief in hand, ask them to bring back the
> PM chat's brief first.

## Project orientation

MLB HR Bets is a daily home-run prediction model. The pipeline scores every
MLB batter on six factors (power, matchup, form, park, weather, lineup),
combines them into a 0-100 composite, picks the top 8 (max 2 per game,
confirmed starters only, non-postponed games), and publishes to
`dingersonly.cc`.

**The model is the product.** UI is a thin renderer over JSON exports.

## Stack and environment (hard constraints)

- **Python 3.14** on **Windows** in **PowerShell**. Test on this env or you
  haven't tested. Two bugs (argparse `%`, NA on parquet round-trip) slipped
  through agents testing on different Python versions on 2026-05-26.
- **SQLite WAL** local DB at `C:/dev/Claude/Projects/data/hr_bets.db`
  (sibling of repo, NOT inside it — `etl/db.py` uses
  `Path(__file__).parent.parent.parent / "data"`).
- **R2 is the source of truth.** Pull before you work:
  `python infra/r2_sync.py pull`
- **GitHub Actions** runs the daily pipeline. Cloudflare Workers Builds
  auto-deploys `dingersonly.cc` on push to `main`.
- **Daily flow:** 2am nightly ETL → noon pipeline (lineups + weather +
  score + export + commit + push) → 1am next-day outcomes.

## Cold-start read order

Before doing anything else in a fresh session, read top-to-bottom:

1. `docs/handoff_2026-05-26.md` — what's actually working / broken right now
2. `BACKLOG.md` — ordered queue + recently shipped log
3. `How_The_HR_Model_Works.md` — model behavior
4. `ARCHITECTURE.md` — component + DB map
5. `WEIGHT_REFIT_LOG.md` — weight-decision history

If your brief references a specific section of any of these, jump there first.

## Hard rules for shipping a briefed task

1. **One PR per atomic change.** No bundling. The user prefers many small
   independent PRs off `main` over one stack.
2. **Base off `main`. Always.** If your task depends on another open PR's
   branch, STOP and tell the user. Two PRs hit silent-merge-to-dead-branch
   bugs on 2026-05-26 from this exact pattern.
3. **One agent at a time within a lane.** No parallel fan-out within the
   same area. Cross-lane parallelism is fine if the user explicitly says so.
4. **Don't bundle follow-up commits into a PR under review.** Open a new
   PR. (The argparse fix landed in the wrong branch on 2026-05-26 because
   of this.)
5. **Verify on Python 3.14 + PowerShell.** `python -m tests.smoke` if smoke
   exists for the area; sample-data spot-check otherwise. Verifying in a
   Linux sandbox does not count.
6. **Do not assume.** If a design call is not pre-resolved in your brief,
   STOP and ask the user. Do not invent. Do not infer "what they probably
   want." If a path is ambiguous, ask.
7. **Trust tool output over your own prose.** If you write "this took 70
   min" and the timestamp says 13 min, the timestamp wins. Same for runtime
   estimates, file sizes, finding counts.
8. **Don't skip hooks** (`--no-verify`, `--no-gpg-sign`, etc.) without
   explicit user approval. If a hook fails, fix the underlying issue.

## False alarms — DO NOT try to "fix" these

These look broken but are intentional state. The handoff doc explains each.
If you "discover" one of these in passing, leave it alone:

- `pick_inputs.ev_trend` 100% NULL — by design until A2 (real Statcast EV)
- `daily_lineup.batting_order > 9` (428 rows) — residue from old roster
  fallback; `score_lineup_position` returns 35.0 for these via the default
  branch, intentional
- `backtest_factors.rescore_row` missing 21d/28d / archetype columns —
  added AFTER A1 refit on purpose; will get reads when flags flip on
- Weather API failures since 2026-05-12 — known (B14 in BACKLOG), GH
  Actions runner IP issue
- `hr_fb_pct` anchor `(8, 20)` — known anchor mis-cal, filed
- `pitcher_fb_pct_allowed > 100` (23 rows) — known Savant parse bug, filed
- `season_batting.team = '???'` for ~20 Athletics — known (C2)
- T4-untiered NULL `barrel_pct_source` — known (B13)
- `score_lineup_position` table anti-correlated with HR — known (B15)

## Standard agent-brief shape

Every brief from the PM chat follows this format. If yours doesn't, ask the
PM chat to re-issue:

```
# TASK: <short name>

## Goal
<2-3 sentences: the change and the outcome>

## Spec
<changes with file:line refs>

## Done when
<verifiable criteria — usually a command + expected output>

## PR description must include
- Headline change with the command that produced it
- What you verified (smoke, run output, sample row)
- Files NOT touched (scope statement, helps the audit)
```

Briefs do not repeat the env constraints, false-alarms list, or hard rules
— those live here and are loaded automatically.

## Closing the loop (when your PR is open)

1. Tell the PM chat: "PR #N is up for `<task name>`"
2. PM chat returns `/ultrareview <N>` + a focused review checklist tailored
   to the task's risk profile
3. You (or a fresh session) run the audit and paste findings back to PM
4. PM chat decides ship / iterate. On ship, PM updates `BACKLOG.md` and
   `WEIGHT_REFIT_LOG.md` via the next session.

## What the PM chat owns (do not modify from a shipping session)

- `BACKLOG.md` — task queue + status
- `WEIGHT_REFIT_LOG.md` — weight-decision history
- `docs/handoff_*.md` — session boundary docs

Touch these from a shipping session only if the brief explicitly says so
(e.g., a B-series task that strikes itself through and adds a "Recently
shipped" entry as part of the same PR).
