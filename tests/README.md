# Tests

Smoke tests + DB sanity probes for the MLB HR Bets pipeline. Standard
library only — no pytest dependency, no fixtures.

## What's here

- **`smoke.py`** — runnable as `python -m tests.smoke`. Two layers:
  1. **Pin tests** for scoring functions (no DB; locks expected outputs
     for known inputs so weight refits or curve tweaks can't silently
     change the math).
  2. **DB sanity probes** (skipped when DB is absent). Catch the bug
     classes flagged by the 2026-05-02 audit before they poison
     `pick_inputs` or `daily_picks`.

## Severity tiers

The runner reports each check as one of:

- **HALT** — pipeline-blocking. Failure means `run_daily.bat` should NOT
  ship picks for today. Today's stale picks stay on the dashboard.
- **WARN** — anomaly worth flagging but not blocking.
- **INFO** — diagnostic; logged for the daily pulse.
- **PASS** — check is green.

Exit codes:

| Code | Meaning |
|---|---|
| `0` | All checks PASS (or only INFO entries) |
| `1` | One or more WARN, no HALT (only when `--strict`) |
| `2` | One or more HALT failed |

## Usage

```cmd
python -m tests.smoke                    # run all (pin + DB)
python -m tests.smoke --pin-only         # skip DB checks (CI/fresh checkout)
python -m tests.smoke --db-only          # skip pin tests
python -m tests.smoke --strict           # WARNs exit non-zero too
```

## Wiring into `run_daily.bat` (suggested, not yet active)

A future commit can add a smoke-gate step between `generate_picks.py`
and `load_picks_to_db.py`:

```bat
python -m tests.smoke --pin-only
if errorlevel 2 goto :smoke_failed

python -m tests.smoke --db-only --strict
if errorlevel 2 goto :smoke_failed
if errorlevel 1 echo [WARN] DB anomalies — picks shipping but flagged

python load_picks_to_db.py
...

:smoke_failed
echo Smoke tests halted the pipeline. Yesterday's picks stay on the site.
exit /b 1
```

## Adding a new check

1. Add a function to `smoke.py` that returns a `Result`. Naming:
   - Pin tests: `pin_<scoring_function>_<scenario>`
   - DB probes: `db_<table>_<assertion>`
2. Append it to `PIN_TESTS` or `DB_PROBES`.
3. Pick the right severity:
   - **HALT** if a failed assertion would mean the picks are wrong
   - **WARN** if it's a smell but not a bug
   - **INFO** for diagnostics that don't gate

## Why standard-library only

The Python pipeline runs on Pablo's local Windows + a fresh-checkout
environment (CI when we add it). Adding `pytest` as a dependency is
fine but unnecessary for this scope — the runner is ~50 lines of plain
Python and the diagnostic value comes from the assertions, not the
framework. Switch to pytest later if the test count grows past ~30.
