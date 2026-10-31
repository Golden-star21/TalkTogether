@echo off
setlocal
cd /d "%~dp0"
title Namestitev TalkTogether

echo Namestitev knjiznic za TalkTogether 0.6.0 Beta
echo.
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m pip install -r requirements.txt
) else (
    python -m pip install -r requirements.txt
)
if errorlevel 1 (
    echo.
    echo Namestitev ni uspela. Preveri, ali je Python 3 namescen in ali internet deluje.
    pause
    exit /b 1
)
echo.
echo Namestitev je koncana. Zdaj lahko zazenes run.bat.
pause
