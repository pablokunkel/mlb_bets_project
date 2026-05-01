@echo off
REM ============================================================
REM  Daily HR Bet Pipeline -- runs at 12:00 PM
REM
REM   1. Morning ETL    -> daily_slate, daily_lineup, weather
REM   2. Generate picks -> results/picks_<DATE>.json
REM   3. Load to DB     -> daily_picks rows for the date
REM   4. Export site    -> mlb_hr_bet_site/data/*.json
REM   5. Git push       -> Cloudflare Pages auto-deploys from main
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
echo  [1/5] Morning ETL...                          ^(~30s^)
echo  [1/5] Morning ETL... >> "%LOGFILE%" 2>&1
python -m etl.etl_morning >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo       ERROR -- see %LOGFILE%
    echo  ERROR: Morning ETL failed! >> "%LOGFILE%" 2>&1
    exit /b 1
)
echo       OK

echo.
echo  [2/5] Generating picks...                     ^(5-10 min on cold cache^)
echo  [2/5] Generating picks... >> "%LOGFILE%" 2>&1
python generate_picks.py >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo       ERROR -- see %LOGFILE%
    echo  ERROR: Pick generation failed! >> "%LOGFILE%" 2>&1
    exit /b 1
)
echo       OK

echo.
echo  [3/5] Loading picks to DB...                  ^(~5s^)
echo  [3/5] Loading picks to DB... >> "%LOGFILE%" 2>&1
python load_picks_to_db.py >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo       ERROR -- see %LOGFILE%
    echo  ERROR: Pick DB load failed! >> "%LOGFILE%" 2>&1
    exit /b 1
)
echo       OK

echo.
echo  [4/5] Exporting site data...                  ^(~10s^)
echo  [4/5] Exporting site data... >> "%LOGFILE%" 2>&1
python export_site_data.py >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo       ERROR -- see %LOGFILE%
    echo  ERROR: Data export failed! >> "%LOGFILE%" 2>&1
    exit /b 1
)
echo       OK

echo.
echo  [5/5] Pushing to GitHub (Cloudflare auto-deploys)... ^(~10s^)
echo  [5/5] Pushing to GitHub... >> "%LOGFILE%" 2>&1
git add mlb_hr_bet_site/data/*.json mlb_hr_bet_site/index.html >> "%LOGFILE%" 2>&1
git commit -m "Daily update %TODAY%" --allow-empty >> "%LOGFILE%" 2>&1
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
