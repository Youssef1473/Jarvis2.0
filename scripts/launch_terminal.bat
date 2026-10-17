@echo off
title JARVIS 2.0 - Tony Stark AI Assistant
chcp 65001 > nul
cd /d "%~dp0\.."

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" run.py %*
) else (
    python run.py %*
)
pause
