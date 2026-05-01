@echo off
REM ============================================================
REM  Nightly ETL -- runs at 2:00 AM
REM
REM  Two phases:
REM   1. Standard nightly ETL (Statcast HR events, season stats, park factors)
REM   2. Pre-warm archetype + bulk Savant caches so noon's daily run is fast
REM
REM  No Netlify deploy here; the noon run picks up the warm cache, scores,
REM  and deploys.
REM
REM  Logs to logs/nightly_YYYY-MM-DD.log. Step markers also echo to console.
REM ============================================================

REM Force UTF-8 on stdout/stderr (matches run_daily.bat fix).
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

cd /d "%~dp0"

REM Locale-proof date parsing.
set LOGDIR=%~dp0logs
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
for /f "delims=" %%i in ('python -c "import datetime; print(datetime.date.today().isoformat())"') do set TODAY=%%i
set LOGFILE=%LOGDIR%\nightly_%TODAY%.log

echo.
echo ========================================
echo  NIGHTLY ETL -- %date% %time%
echo  Log: %LOGFILE%
echo ========================================
(
    echo ========================================
    echo  NIGHTLY ETL -- %date% %time%
    echo ========================================
) >> "%LOGFILE%" 2>&1

echo.
echo  [1/2] Standard nightly ETL...                ^(~5-15 min^)
echo  [1/2] Standard nightly ETL... >> "%LOGFILE%" 2>&1
python -m etl.etl_nightly >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo       ERROR -- see %LOGFILE%
    echo  ERROR: Nightly ETL failed! >> "%LOGFILE%" 2>&1
    exit /b 1
)
echo       OK

echo.
echo  [2/2] Pre-warm archetype + bulk caches...    ^(~10-20 min on cold start^)
echo  [2/2] Pre-warm archetype + bulk caches... >> "%LOGFILE%" 2>&1
python prewarm_cache.py >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo       WARN -- prewarm failed, see %LOGFILE%. Daily run will fall back.
    echo  WARN: prewarm failed >> "%LOGFILE%" 2>&1
    REM Don't abort -- partial warmth is still useful, daily run can still proceed.
)
echo       OK

echo.
echo ========================================
echo  DONE -- %time%
echo ========================================
(
    echo  DONE -- %date% %time%
    echo ========================================
) >> "%LOGFILE%" 2>&1
