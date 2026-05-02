# Diagnostic scripts

One-off and reusable investigation tools for the MLB HR Bets pipeline.
**Not part of the daily/nightly pipeline** — none of these are referenced
by `run_daily.bat` / `run_outcomes.bat` / `run_nightly.bat`.

Run from the project root:
```cmd
python diagnostics/<script>.py [args]
```

## Contents

| Script | Purpose | Reusable? |
|---|---|---|
| `autopsy_game.py` | Per-game decomposition: every batter's full `pick_inputs` + scores + outcome. Use this to triage a busted slate. Run as `python diagnostics/autopsy_game.py YYYY-MM-DD TEAM1 [TEAM2]`. | **Yes** — the model retro tool we'll keep using. |
| `factor_diagnostics.py` | Standalone factor-level diagnostics. Originally built to support the dashboard's per-factor analysis. Verify against current output before relying on it. | Verify post-move |
| `lineup_diagnostic.py` | Lineup-source debugging — relevant to the open lineup-data integrity work. | **Yes** |
| `simulate_power_anchors.py` | A/B simulator for tightening `score_power` anchors. Read-only against `pick_inputs`. | **Yes** — reusable for any anchor-tightening A/B test. |
| `debug_buxton.py` | One-off Buxton triage from 2026-05-01 (when missing-barrel% bug was hammering elite hitters). Useful as a template for future single-batter investigations. | Template |
| `check_woba_today.py` | One-off wOBA bin distribution check from 2026-05-01. | One-off |

## Why these aren't at the project root

Pre-cleanup, all of these lived at the project root alongside production
scripts (`generate_picks.py`, `score_batters.py`, etc.). That made the
root noisy and made it hard to tell at a glance what's "the daily
pipeline" vs "tools I built once during an investigation."

The split was applied 2026-05-02 in cleanup C4. None of these scripts
are imported by any production module — verified via `git grep` before
the move.

## Adding a new diagnostic

When you write a new investigation script:

1. Drop it in `diagnostics/`, not at the root.
2. Add a row to the table above with a one-line purpose.
3. If it's a parameterized tool (good!), include a usage example.
4. If it's a one-off for a specific date or player, mark it as such — future-you will want to know it's snapshot-of-an-investigation, not a reusable tool.
