@echo off
setlocal EnableExtensions
cd /d "%~dp0"
echo Clear runtime data...
if exist profiles rmdir /s /q profiles
if exist output\debug rmdir /s /q output\debug
if exist output\task_progress.json del /f /q output\task_progress.json
if exist dist\WhitelistClick\profiles rmdir /s /q dist\WhitelistClick\profiles
if exist dist\WhitelistClick\output\debug rmdir /s /q dist\WhitelistClick\output\debug
if exist dist\WhitelistClick\output\task_progress.json del /f /q dist\WhitelistClick\output\task_progress.json
echo Done.
pause
