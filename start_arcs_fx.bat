@echo off
title ARCS-FX Trading Bot

:: Change to project directory (update this path if you move the project)
cd /d "C:\Users\fadia\OneDrive\Bureau\ARCS-FX"

echo.
echo  ====================================================
echo   ARCS-FX  --  Starting bot + dashboard
echo  ====================================================
echo.
echo  Dashboard will open at http://localhost:5000
echo  Press Ctrl+C to stop the bot
echo.

:: Run the bot with dashboard
py -3.11 main.py --dashboard

:: If bot exits with an error, pause so you can read it
if %ERRORLEVEL% neq 0 (
    echo.
    echo  ====================================================
    echo   ERROR: Bot stopped with exit code %ERRORLEVEL%
    echo  ====================================================
    echo.
    pause
)
