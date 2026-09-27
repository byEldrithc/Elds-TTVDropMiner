@echo off
rem Runs Eld's TTVDropMiner from source in its own window (tray icon included).
rem The first run creates .venv and installs the requirements.
rem Browser mode instead:  .venv\Scripts\python.exe main.py
chcp 65001 >nul
cd /d "%~dp0"
title Eld's TTVDropMiner
if not exist ".venv\Scripts\pythonw.exe" (
    echo First run: setting up...
    python -m venv .venv || (echo Python 3.10+ not found. Install it from https://www.python.org & pause & exit /b 1)
    ".venv\Scripts\python.exe" -m pip install -q --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || (pause & exit /b 1)
)
start "" ".venv\Scripts\pythonw.exe" app.py %*
