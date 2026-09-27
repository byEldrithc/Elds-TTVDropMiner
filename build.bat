@echo off
rem Builds the .exe with PyInstaller and the installer (Setup.exe) with Inno Setup.
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv || (echo Python not found. Install it from https://www.python.org & pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt -r requirements-build.txt || (pause & exit /b 1)
".venv\Scripts\python.exe" tools\check_locales.py || (echo Fix the locale files first. & pause & exit /b 1)
".venv\Scripts\python.exe" tools\make_icon.py || (pause & exit /b 1)
rem "call" keeps cmd from stripping the quotes of a quoted command inside for /f
for /f "delims=" %%v in ('call ".venv\Scripts\python.exe" -c "from version import __version__; print(__version__)"') do set APPVER=%%v
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --windowed --name EldsTTVDropMiner ^
    --icon assets\icon.ico --add-data "web;web" --add-data "assets;assets" --add-data "locales;locales" ^
    app.py || (pause & exit /b 1)
set ISCC="%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist %ISCC% set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist %ISCC% (
    echo Inno Setup not found, only dist\EldsTTVDropMiner was built.
    echo To build the installer: winget install JRSoftware.InnoSetup
    pause & exit /b 0
)
%ISCC% /DAppVersion=%APPVER% installer.iss || (pause & exit /b 1)
echo Installer ready: dist\EldsTTVDropMiner-v%APPVER%-Setup.exe
pause
