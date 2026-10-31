@echo off
setlocal
cd /d "%~dp0"
title TalkTogether

echo ========================================
echo          TalkTogether 0.6.0 Beta
echo ========================================
echo.

where py >nul 2>nul
if %errorlevel%==0 (
    set "PYTHON=py -3"
) else (
    set "PYTHON=python"
)

%PYTHON% updater.py
if errorlevel 10 exit /b 0

%PYTHON% main.py
if errorlevel 1 (
    echo.
    echo TalkTogether se je zaprl z napako.
    pause
)
