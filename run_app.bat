@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if exist "dist\WhitelistClick\WhitelistClick.exe" (
  start "" "dist\WhitelistClick\WhitelistClick.exe"
  exit /b 0
)
if exist "WhitelistClick.exe" (
  start "" "WhitelistClick.exe"
  exit /b 0
)
echo WhitelistClick.exe not found. Run build_portable.bat first.
pause
