@echo off
REM ============================================================
REM  Initial DB Backfill — run this ONCE to seed 3 seasons
REM  Takes 60-90 minutes (Statcast rate limits)
REM  Creates data/hr_bets.db with 2024-2026 data
REM ============================================================

echo.
echo  ========================================
echo   MLB HR BETS — 3-SEASON BACKFILL
echo   Estimated time: 60-90 minutes
echo  ========================================
echo.

REM Step 1: Create DB and backfill
echo  [1/2] Running nightly ETL in backfill mode...
echo         This fetches Statcast HR events, pitcher arsenals,
echo         victim profiles, batting/pitching stats, and park factors
echo         for 2024, 2025, and 2026.
echo.
python -m etl.etl_nightly --backfill
if errorlevel 1 (
    echo  ERROR: Backfill failed! Check output above.
    pause
    exit /b 1
)

REM Step 2: Show DB stats
echo.
echo  [2/2] Database summary:
python -m etl.db --stats

echo.
echo  ========================================
echo   BACKFILL COMPLETE
echo   DB location: data\hr_bets.db
echo  ========================================
echo.
echo  Next steps:
echo    1. Run generate_picks.py to generate today's picks
echo    2. Run export_site_data.py to build dashboard data
echo    3. Commit + push to main (Cloudflare Worker dingersonlybot auto-deploys)
echo.
pause
