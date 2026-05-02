@echo off
REM ============================================================
REM  Outcomes ETL — runs at 1:00 AM
REM
REM   1. Pull yesterday's box scores -> outcomes table
REM   2. Re-score history with current model -> factor_accuracy.json
REM   3. Re-export site data with fresh hit/miss results
REM   4. Push refreshed JSON to GitHub (Cloudflare Pages auto-deploys)
REM
REM  Logs to logs/outcomes_YYYY-MM-DD.log. Independent of the
REM  noon picks run — failure here doesn't block tomorrow's picks.
REM ============================================================

cd /d "%~dp0"

set LOGDIR=%~dp0logs
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
REM Locale-proof date parsing (matches run_nightly.bat). Earlier brittle
REM tokens=1-3 delims=/ parser was inserting day-of-week into the path,
REM so the log file ended up at e.g. "outcomes_2026-Sat 05-02.log".
for /f "delims=" %%i in ('python -c "import datetime; print(datetime.date.today().isoformat())"') do set TODAY=%%i
set LOGFILE=%LOGDIR%\outcomes_%TODAY%.log

echo ======================================== >> "%LOGFILE%" 2>&1
echo  OUTCOMES ETL — %date% %time%            >> "%LOGFILE%" 2>&1
echo ======================================== >> "%LOGFILE%" 2>&1

REM Step 1: Yesterday's outcomes
echo  [1/4] Fetching yesterday's outcomes... >> "%LOGFILE%" 2>&1
python -m etl.etl_outcomes >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo  ERROR: Outcomes ETL failed! >> "%LOGFILE%" 2>&1
    exit /b 1
)

REM Step 2: Re-score history with current model and write factor_accuracy.json.
REM Soft-failure: if backtest crashes, dashboard keeps showing the previous
REM accuracy snapshot rather than blocking the deploy.
echo  [2/4] Backtesting current model on history... >> "%LOGFILE%" 2>&1
python backtest_factors.py >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo  WARN: backtest_factors failed -- skipping accuracy refresh >> "%LOGFILE%" 2>&1
)

REM Step 3: Re-export site data so hit rates / streak update
echo  [3/4] Re-exporting site data... >> "%LOGFILE%" 2>&1
python export_site_data.py >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo  ERROR: Data export failed! >> "%LOGFILE%" 2>&1
    exit /b 1
)

REM Step 4: Push refreshed JSON to GitHub (Cloudflare auto-deploys)
echo  [4/4] Pushing to GitHub... >> "%LOGFILE%" 2>&1
git add mlb_hr_bet_site/data/*.json >> "%LOGFILE%" 2>&1
git commit -m "Outcomes + accuracy refresh" --allow-empty >> "%LOGFILE%" 2>&1

REM Pull --rebase before pushing. Mirrors the run_daily.bat fix: avoids
REM non-fast-forward rejection when main has moved since this machine's
REM last sync. --autostash protects unrelated working-tree changes.
git pull --rebase --autostash origin main >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo  ERROR: git pull --rebase failed -- conflicts need manual resolution >> "%LOGFILE%" 2>&1
    echo  Outcomes ARE committed locally but NOT pushed yet. >> "%LOGFILE%" 2>&1
    exit /b 1
)

git push origin main >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo  ERROR: Git push failed! >> "%LOGFILE%" 2>&1
    exit /b 1
)

echo  DONE — %date% %time% >> "%LOGFILE%" 2>&1
echo ======================================== >> "%LOGFILE%" 2>&1
