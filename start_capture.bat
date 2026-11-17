@echo off
chcp 65001 >nul
title Chujian Capture Proxy
cd /d "%~dp0"
python tools\capture.py
echo.
pause
