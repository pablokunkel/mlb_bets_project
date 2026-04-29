@echo off
REM ============================================================
REM  Daily HR Bet Pipeline
REM  1. Generate picks (live data from MLB/weather APIs)
REM  2. Export data to JSON for the dashboard
REM  3. Git push to trigger Netlify deploy
REM
REM  Runs unattended via scheduled task or manually.
REM  Logs output to logs/daily_YYYY-MM-DD.log
REM ============================================================

REM Always run from the script's own directory
cd /d "%~dp0"

REM Set up logging
set LOGDIR=%~dp0logs
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
for /f "tokens=1-3 delims=/" %%a in ('echo %date%') do set TODAY=%%c-%%a-%%b
set LOGFILE=%LOGDIR%\daily_%TODAY%.log

echo ======================================== >> "%LOGFILE%" 2>&1
echo  DAILY HR BET PIPELINE — %date% %time%   >> "%LOGFILE%" 2>&1
echo ======================================== >> "%LOGFILE%" 2>&1

REM Step 1: Generate picks
echo  [1/3] Generating picks... >> "%LOGFILE%" 2>&1
python generate_picks.py >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo  ERROR: Pick generation failed! >> "%LOGFILE%" 2>&1
    exit /b 1
)

REM Step 2: Export site data
echo  [2/3] Exporting site data... >> "%LOGFILE%" 2>&1
python export_site_data.py >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo  ERROR: Data export failed! >> "%LOGFILE%" 2>&1
    exit /b 1
)

REM Step 3: Git push
echo  [3/3] Pushing to GitHub... >> "%LOGFILE%" 2>&1
git add mlb_hr_bet_site/data/ >> "%LOGFILE%" 2>&1
git commit -m "Daily picks update %TODAY%" >> "%LOGFILE%" 2>&1
git push >> "%LOGFILE%" 2>&1

echo  DONE — %date% %time% >> "%LOGFILE%" 2>&1
echo ======================================== >> "%LOGFILE%" 2>&1
