@echo off
setlocal
cd /d "%~dp0"
echo Starting VAOS and its temporary camera tunnel...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_windows.ps1"
if errorlevel 1 (
  echo.
  echo Startup failed. Review the message above.
  pause
)
