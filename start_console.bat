@echo off
chcp 65001 >nul
title Chujian Console
cd /d "%~dp0"
python tools\console.py
echo.
pause
