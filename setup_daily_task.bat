@echo off
REM ============================================================
REM  Creates a Windows Scheduled Task to run daily picks at noon
REM  Run this ONCE as Administrator to register the task.
REM  After that it runs automatically every day at 12:00 PM.
REM
REM  To remove:  schtasks /delete /tn "MLB_HR_Daily_Picks" /f
REM  To run now: schtasks /run /tn "MLB_HR_Daily_Picks"
REM ============================================================

echo.
echo  ========================================
echo   Setting up Daily HR Picks Task
echo  ========================================
echo.

schtasks /create ^
  /tn "MLB_HR_Daily_Picks" ^
  /tr "\"%~dp0run_daily.bat\"" ^
  /sc daily ^
  /st 12:00 ^
  /ri 0 ^
  /rl HIGHEST ^
  /f

if errorlevel 1 (
    echo.
    echo  ERROR: Failed to create task.
    echo  Try running this script as Administrator:
    echo    Right-click setup_daily_task.bat -^> Run as administrator
    echo.
) else (
    echo.
    echo  Task "MLB_HR_Daily_Picks" created successfully.
    echo  Runs daily at 12:00 PM.
    echo.
    echo  Useful commands:
    echo    Run now:    schtasks /run /tn "MLB_HR_Daily_Picks"
    echo    Check:      schtasks /query /tn "MLB_HR_Daily_Picks"
    echo    Remove:     schtasks /delete /tn "MLB_HR_Daily_Picks" /f
    echo.
)

pause
