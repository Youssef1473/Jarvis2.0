@echo off
title JARVIS 2.0 - OmniRoute AI Gateway
echo =======================================================
echo   JARVIS 2.0 - OmniRoute AI Gateway
echo   OpenAI Endpoint: http://localhost:20128/v1
echo   Web Dashboard:   http://localhost:20128/dashboard
echo =======================================================
echo Starting server in background daemon mode...
omniroute serve --daemon --no-open --no-tray
echo OmniRoute is running.
pause
