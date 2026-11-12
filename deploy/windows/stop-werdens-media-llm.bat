@echo off
title Werdens Media LLM - stoppen
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-werdens-media-llm.ps1"
echo.
pause
