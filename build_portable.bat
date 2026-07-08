@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title WhitelistClick v1.0 - Build
set "LOG=%CD%\build_log.txt"
echo =============================================
echo WhitelistClick v1.0 build
echo Current folder: %CD%
echo =============================================
echo This build does NOT download Chromium. It uses local Edge/Chrome first.
echo [BUILD START] %date% %time% > "%LOG%"
set "PY=python"
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
where python >nul 2>nul
if errorlevel 1 (
  where py >nul 2>nul
  if errorlevel 1 goto NO_PYTHON
)
%PY% --version >> "%LOG%" 2>&1
if errorlevel 1 goto NO_PYTHON
echo Step 1/5: Install dependencies...
%PY% -m pip install --upgrade pip >> "%LOG%" 2>&1
if errorlevel 1 goto BUILD_FAIL
%PY% -m pip install -r requirements.txt >> "%LOG%" 2>&1
if errorlevel 1 goto BUILD_FAIL
%PY% -m pip install pyinstaller >> "%LOG%" 2>&1
if errorlevel 1 goto BUILD_FAIL
echo Step 2/5: Clean old build...
if exist build rmdir /s /q build >> "%LOG%" 2>&1
if exist dist rmdir /s /q dist >> "%LOG%" 2>&1
echo Step 3/5: Build EXE...
%PY% -m PyInstaller --noconfirm --clean --windowed --name WhitelistClick --collect-all playwright --collect-all openpyxl --hidden-import=xlrd --add-data "keywords.csv;." --add-data "whitelist.csv;." --add-data "blacklist.csv;." --add-data "proxies.txt;." --add-data "config.example.json;." --add-data "update.json;." app.py >> "%LOG%" 2>&1
if errorlevel 1 goto BUILD_FAIL
if not exist "dist\WhitelistClick\WhitelistClick.exe" goto BUILD_FAIL
echo Step 4/5: Copy runtime files...
copy /y "README.txt" "dist\WhitelistClick\README.txt" >> "%LOG%" 2>&1
copy /y "USER_GUIDE.txt" "dist\WhitelistClick\USER_GUIDE.txt" >> "%LOG%" 2>&1
copy /y "update.json" "dist\WhitelistClick\update.json" >> "%LOG%" 2>&1
copy /y "run_app.bat" "dist\WhitelistClick\run_app.bat" >> "%LOG%" 2>&1
copy /y "check_update.bat" "dist\WhitelistClick\check_update.bat" >> "%LOG%" 2>&1
copy /y "updater.py" "dist\WhitelistClick\updater.py" >> "%LOG%" 2>&1
copy /y "update_config.json" "dist\WhitelistClick\update_config.json" >> "%LOG%" 2>&1
echo Step 5/5: Done.
echo.
echo Build success: %CD%\dist\WhitelistClick\WhitelistClick.exe
echo Send the whole dist\WhitelistClick folder to other users.
explorer "%CD%\dist\WhitelistClick"
pause
exit /b 0
:NO_PYTHON
echo ERROR: Python not found. Please install Python 3.10+ first. >> "%LOG%"
echo Python not found. Please install Python 3.10+ first.
pause
exit /b 1
:BUILD_FAIL
echo.
echo Build failed. Please send build_log.txt.
echo.
powershell -NoProfile -Command "if (Test-Path '%LOG%') { Get-Content '%LOG%' -Tail 80 }"
pause
exit /b 1
