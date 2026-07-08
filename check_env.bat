@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title WhitelistClick v1.0 - Check Env
set "LOG=%CD%\check_log.txt"
echo [CHECK START] %date% %time% > "%LOG%"
echo WhitelistClick v1.0 - Environment check
where python >> "%LOG%" 2>&1
where py >> "%LOG%" 2>&1
where python >nul 2>nul
if errorlevel 1 (
  where py >nul 2>nul
  if errorlevel 1 (
    echo Python not found. Please install Python 3.10+ first.
    echo Python not found. >> "%LOG%"
    pause
    exit /b 1
  )
)
python --version >> "%LOG%" 2>&1
py -3 --version >> "%LOG%" 2>&1
where python
where py
python --version 2>nul
py -3 --version 2>nul
echo Done. See check_log.txt if needed.
pause
