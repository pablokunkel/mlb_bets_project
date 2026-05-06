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
    REM ---------------------------------------------------------------
    REM  Override schtasks defaults that silently skip noon runs.
    REM
    REM  schtasks /create has no flags for these three settings, so it
    REM  inherits Windows defaults that are hostile to laptops:
    REM    DisallowStartIfOnBatteries = True   (skip if unplugged at 12:00)
    REM    StopIfGoingOnBatteries     = True   (kill mid-pipeline on unplug)
    REM    StartWhenAvailable         = False  (no catch-up after a miss)
    REM
    REM  Bit us 2026-05-06: laptop happened to be on battery at noon,
    REM  task silently skipped (NumberOfMissedRuns went to 1), and with
    REM  StartWhenAvailable=False it never caught up when plugged back
    REM  in. PowerShell's Set-ScheduledTask exposes all three flags.
    REM  We patch them right after creation so a fresh-machine install
    REM  is correct from minute one.
    REM ---------------------------------------------------------------
    powershell -NoProfile -Command "$ErrorActionPreference='Stop'; try { $t=Get-ScheduledTask -TaskName 'MLB_HR_Daily_Picks'; $t.Settings.DisallowStartIfOnBatteries=$false; $t.Settings.StopIfGoingOnBatteries=$false; $t.Settings.StartWhenAvailable=$true; $t | Set-ScheduledTask | Out-Null; exit 0 } catch { Write-Host $_; exit 1 }"
    if errorlevel 1 (
        echo.
        echo  WARN: task created, but battery / catch-up settings could NOT be patched.
        echo        Open Task Scheduler GUI and adjust manually:
        echo          Conditions ^> uncheck "Start the task only if the computer is on AC power"
        echo          Conditions ^> uncheck "Stop if the computer switches to battery power"
        echo          Settings   ^> check   "Run task as soon as possible after a scheduled start is missed"
        echo.
    ) else (
        echo  Battery + catch-up settings: patched.
    )

    echo.
    echo  Task "MLB_HR_Daily_Picks" created successfully.
    echo  Runs daily at 12:00 PM (catches up if missed; ignores battery state).
    echo.
    echo  Useful commands:
    echo    Run now:    schtasks /run /tn "MLB_HR_Daily_Picks"
    echo    Check:      schtasks /query /tn "MLB_HR_Daily_Picks"
    echo    Detail:     powershell -NoProfile -Command "Get-ScheduledTask -TaskName 'MLB_HR_Daily_Picks' ^| Get-ScheduledTaskInfo"
    echo    Remove:     schtasks /delete /tn "MLB_HR_Daily_Picks" /f
    echo.
)

pause
