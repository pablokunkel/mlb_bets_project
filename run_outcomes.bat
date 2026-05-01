@echo off
REM ============================================================
REM  Outcomes ETL — runs at 1:00 AM
REM
REM   1. Pull yesterday's box scores -> outcomes table
REM   2. Re-export site data with fresh hit/miss results
REM   3. Re-deploy to Netlify so the dashboard reflects last night
REM
REM  Logs to logs/outcomes_YYYY-MM-DD.log. Independent of the
REM  noon picks run — failure here doesn't block tomorrow's picks.
REM ============================================================

cd /d "%~dp0"

set LOGDIR=%~dp0logs
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
for /f "tokens=1-3 delims=/" %%a in ('echo %date%') do set TODAY=%%c-%%a-%%b
set LOGFILE=%LOGDIR%\outcomes_%TODAY%.log

echo ======================================== >> "%LOGFILE%" 2>&1
echo  OUTCOMES ETL — %date% %time%            >> "%LOGFILE%" 2>&1
echo ======================================== >> "%LOGFILE%" 2>&1

REM Step 1: Yesterday's outcomes
echo  [1/3] Fetching yesterday's outcomes... >> "%LOGFILE%" 2>&1
python -m etl.etl_outcomes >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo  ERROR: Outcomes ETL failed! >> "%LOGFILE%" 2>&1
    exit /b 1
)

REM Step 2: Re-export site data so hit rates / streak update
echo  [2/3] Re-exporting site data... >> "%LOGFILE%" 2>&1
python export_site_data.py >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo  ERROR: Data export failed! >> "%LOGFILE%" 2>&1
    exit /b 1
)

REM Step 3: Deploy refreshed JSON
echo  [3/3] Deploying to Netlify... >> "%LOGFILE%" 2>&1
netlify deploy --prod --dir=mlb_hr_bet_site --site=0fade6bd-ae06-43a8-aaef-22ee692ecbba >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo  ERROR: Netlify deploy failed! >> "%LOGFILE%" 2>&1
    exit /b 1
)

echo  DONE — %date% %time% >> "%LOGFILE%" 2>&1
echo ======================================== >> "%LOGFILE%" 2>&1
