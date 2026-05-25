@echo off
REM ============================================================
REM  run_backfill_2025.bat -- Local 2025-season backfill with R2 bookends
REM
REM  Thin shim over run_backfill_local.py. Same Windows convention as
REM  run_daily.bat / run_outcomes.bat / run_nightly.bat (the user-facing
REM  entry is a .bat; the work lives in Python).
REM
REM  NOT the same as run_backfill.bat (that one is the initial 3-season
REM  DB seeding via etl_nightly --backfill, run ONCE on a fresh DB).
REM  This one walks 2025 daily and reconstructs pick_inputs rows for the
REM  A1 weight refit -- 6-12 hours of work, R2-safe.
REM
REM  Pipeline (handled by the Python wrapper):
REM    [1/3] python infra/r2_sync.py pull
REM    [2/3] python -m etl.backfill_2025 %*
REM    [3/3] python infra/r2_sync.py push   (always, even on Ctrl+C)
REM
REM  Usage:
REM    run_backfill_2025.bat                   ::  full 2025 season
REM    run_backfill_2025.bat --max-runtime 4h
REM    run_backfill_2025.bat --start 2025-04-01 --end 2025-04-30 --max-dates 30
REM    run_backfill_2025.bat --outcomes-only
REM
REM  R2 credentials are loaded from .env at the repo root. If they're
REM  missing the Python wrapper exits before doing any work and tells you
REM  what to add.
REM
REM  Ctrl+C behavior:
REM    Cmd.exe may show "Terminate batch job? (Y/N)" -- press N to keep
REM    the batch running. The Python wrapper has already caught SIGINT
REM    by then; pressing N lets it finish the R2 push step. Pressing Y
REM    skips the push -- you can recover by running:
REM        python infra\r2_sync.py push
REM ============================================================

setlocal

REM Move to the repo root (the dir this .bat lives in).
pushd "%~dp0"

python run_backfill_local.py %*
set "EXITCODE=%errorlevel%"

popd
endlocal & exit /b %EXITCODE%
