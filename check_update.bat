@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
%PY% updater.py
pause
