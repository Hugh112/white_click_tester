@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title WhitelistClick v1.0 - Source Debug
set "PY=python"
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
%PY% -m pip install -r requirements.txt
%PY% app.py
pause
