@echo off
chcp 65001 >nul
cd /d "C:/Users/86189/WorkBuddy/2026-11-14-01-23-55/pjsk-vision"
title PJSK AutoPlay

echo ============================================================
echo   PJSK AutoPlay  -  Yaminabe!!!! MASTER  (full)
echo ============================================================
echo.
echo Press ENTER to start (full 126s)
pause >nul

echo.
echo Preparing injector and schedule...
tools\py311\python.exe manual.py

:AFTER
echo.
set /p SHOT=g = screenshot, ENTER = exit:
if /i "%SHOT%"=="g" goto GRAB
goto END

:GRAB
tools\py311\python.exe grab_result.py
start "" "out\result.png"

:END
pause