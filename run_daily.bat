@echo off
REM ============================================================
REM  Daily HR Bet Pipeline -- runs at 12:00 PM
REM
REM   0a. Kill stale Python    -> reap zombies from a Ctrl+C'd prior run
REM   0b. Pull latest source   -> git pull --rebase --autostash origin main
REM   1.  Morning ETL          -> daily_slate, daily_lineup, weather
REM   2.  Generate picks       -> results/picks_<DATE>.json
REM   3.  Load picks to DB     -> daily_picks rows for the date
REM   4.  Refresh yesterday    -> outcomes + hr_events (self-heal)
REM   5.  Export site          -> mlb_hr_bet_site/data/*.json
REM   6.  Git push             -> Cloudflare Pages auto-deploys from main
REM
REM  Step 0 was added 2026-05-03 after a noon-run failure: a PR merged
REM  on github.com (PR #18) fixed a KeyError that crashed format_card
REM  on T4 picks, but the local main checkout was 12 commits behind
REM  origin/main and never pulled it. Today's run used stale code and
REM  crashed even though the fix had landed hours earlier. Pulling at
REM  the START of run_daily means we always run the latest merged
REM  source. --autostash protects working-tree edits (raw_data_v2.csv,
REM  workers/live-hr/package*.json bumps, etc.). Soft-fail: if the
REM  pull errors (e.g., merge conflict needing manual resolution), warn
REM  but proceed with whatever code is checked out — better to attempt
REM  picks with stale code than to skip picks entirely.
REM
REM  Step 4 is a self-healing safety net: if last night's 1 AM
REM  run_outcomes.bat failed to push (or hadn't run yet because we
REM  reboot mid-deploy etc.), the live HR worker has already advanced
REM  to today's date so the dashboard's HR Recap goes blank for
REM  yesterday. Refreshing here means hr_recap.json picks up
REM  yesterday's HRs by noon at the latest. INSERT OR REPLACE makes
REM  it a no-op when 1 AM already succeeded.
REM
REM  Migrated from Netlify to Cloudflare Pages 2026-05-01.
REM  Repo: https://github.com/pablokunkel/mlb_bets_project
REM
REM  Logs to logs/daily_YYYY-MM-DD.log. Step markers also echo to console.
REM  Live-tail in another window:
REM    powershell -Command "Get-Content logs\daily_YYYY-MM-DD.log -Wait -Tail 20"
REM ============================================================

REM Force Python to use UTF-8 for stdout/stderr regardless of Windows
REM locale. Without this, unicode chars in print() crash with
REM UnicodeEncodeError when redirected to a log file under cp1252.
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

cd /d "%~dp0"

REM Set up logging -- use Python to format date robustly across locales.
set LOGDIR=%~dp0logs
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
for /f "delims=" %%i in ('python -c "import datetime; print(datetime.date.today().isoformat())"') do set TODAY=%%i
set LOGFILE=%LOGDIR%\daily_%TODAY%.log

echo.
echo ========================================
echo  DAILY HR BET PIPELINE -- %date% %time%
echo  Log: %LOGFILE%
echo  (live tail another window: powershell Get-Content "%LOGFILE%" -Wait -Tail 20)
echo ========================================
(
    echo ========================================
    echo  DAILY HR BET PIPELINE -- %date% %time%
    echo ========================================
) >> "%LOGFILE%" 2>&1

echo.
echo  [0a/6] Killing any stale Python processes from prior runs...
echo  [0a/6] Killing stale Python processes... >> "%LOGFILE%" 2>&1
REM 2026-05-05 fix: when the user Ctrl+Cs run_daily.bat or the
REM scheduler kills it mid-run, the python.exe child processes
REM aren't always reaped. Those zombies hold pybaseball cache
REM locks + DB connections, and the next invocation hangs at
REM step [2/6] generate_picks waiting for them. Two days in a
REM row this bit us (2026-05-04 + 2026-05-05). Now we
REM defensively kill any python.exe processes from prior runs
REM at the start of every invocation. The .bat itself is cmd.exe
REM (not python.exe), so this never kills its own host. /F forces
REM the kill, /IM matches by image name, 2>nul suppresses the
REM "no matching process found" error message on a clean machine.
taskkill /F /IM python.exe >> "%LOGFILE%" 2>&1
echo       OK
echo.
echo  [0b/6] Pull latest source...                  ^(~5s, soft-fail^)
echo  [0b/6] Pull latest source... >> "%LOGFILE%" 2>&1
REM Pull merged PRs into local main BEFORE running picks. See header
REM comment for the 2026-05-03 motivation.
git pull --rebase --autostash origin main >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo       WARN -- pull failed; running with whatever code is checked out
    echo  WARN: git pull --rebase failed -- using local source as-is >> "%LOGFILE%" 2>&1
) else (
    echo       OK
)

echo.
echo  [1/6] Morning ETL...                          ^(~30s^)
echo  [1/6] Morning ETL... >> "%LOGFILE%" 2>&1
python -m etl.etl_morning >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo       ERROR -- see %LOGFILE%
    echo  ERROR: Morning ETL failed! >> "%LOGFILE%" 2>&1
    exit /b 1
)
echo       OK

echo.
echo  [2/6] Generating picks...                     ^(5-10 min on cold cache^)
echo  [2/6] Generating picks... >> "%LOGFILE%" 2>&1
python generate_picks.py >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo       ERROR -- see %LOGFILE%
    echo  ERROR: Pick generation failed! >> "%LOGFILE%" 2>&1
    exit /b 1
)
echo       OK

echo.
echo  [3/6] Loading picks to DB...                  ^(~5s^)
echo  [3/6] Loading picks to DB... >> "%LOGFILE%" 2>&1
python load_picks_to_db.py >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo       ERROR -- see %LOGFILE%
    echo  ERROR: Pick DB load failed! >> "%LOGFILE%" 2>&1
    exit /b 1
)
echo       OK

echo.
echo  [4/6] Refresh yesterday's outcomes + HR events... ^(~30s, soft-fail^)
echo  [4/6] Refresh yesterday's outcomes + HR events... >> "%LOGFILE%" 2>&1
REM Self-heal step: if run_outcomes.bat (1 AM) failed or didn't push
REM yet, the dashboard's HR Recap goes blank for yesterday at the same
REM time the live tracker advances to today (worker rolls at midnight).
REM `python -m etl.etl_outcomes` defaults to yesterday and is idempotent
REM (INSERT OR REPLACE), so re-running here is a no-op when 1 AM
REM already succeeded. Soft-fail: warn but don't block today's deploy.
python -m etl.etl_outcomes >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo       WARN -- yesterday refresh failed; see %LOGFILE%
    echo  WARN: etl_outcomes failed -- hr_recap may be stale for yesterday >> "%LOGFILE%" 2>&1
) else (
    echo       OK
)

echo.
echo  [5/6] Exporting site data...                  ^(~10s^)
echo  [5/6] Exporting site data... >> "%LOGFILE%" 2>&1
python export_site_data.py >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo       ERROR -- see %LOGFILE%
    echo  ERROR: Data export failed! >> "%LOGFILE%" 2>&1
    exit /b 1
)
echo       OK

echo.
echo  [6/6] Pushing to GitHub (Cloudflare auto-deploys)... ^(~10s^)
echo  [6/6] Pushing to GitHub... >> "%LOGFILE%" 2>&1
git add mlb_hr_bet_site/data/*.json mlb_hr_bet_site/index.html >> "%LOGFILE%" 2>&1
git commit -m "Daily update %TODAY%" --allow-empty >> "%LOGFILE%" 2>&1

REM Pull --rebase BEFORE pushing. If main moved during the noon window
REM (e.g., a PR merged on github.com), our daily-update commit would
REM otherwise be rejected non-fast-forward (this happened 2026-05-02:
REM 4 PRs merged between local's last sync and the noon run, push got
REM rejected, today's picks stayed local until manual recovery).
REM
REM --autostash handles any unrelated working-tree changes by stashing
REM them around the rebase. Daily-update touches only data/*.json which
REM rarely conflict with PR changes (PRs touch source code).
git pull --rebase --autostash origin main >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo       ERROR -- pull --rebase failed; see %LOGFILE%
    echo  ERROR: git pull --rebase failed -- conflicts need manual resolution >> "%LOGFILE%" 2>&1
    echo  Today's picks ARE committed locally but NOT pushed yet. >> "%LOGFILE%" 2>&1
    exit /b 1
)

git push origin main >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo       ERROR -- see %LOGFILE%
    echo  ERROR: Git push failed! >> "%LOGFILE%" 2>&1
    exit /b 1
)
echo       OK

echo.
echo ========================================
echo  DONE -- %time%
echo  Site updates auto-deploy via Cloudflare Pages
echo ========================================
(
    echo  DONE -- %date% %time%
    echo ========================================
) >> "%LOGFILE%" 2>&1
