@echo off
chcp 65001 >nul
setlocal
rem Resolve this file's folder, strip trailing backslash (avoids "path\" escaping the quote)
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"
cd /d "%HERE%"
title Attention Lifecycle Dashboard

echo ============================================
echo    Attention Lifecycle Dashboard
echo ============================================
echo.
echo [1/2] Updating data from ApeWisdom...
echo.
python fetch.py
if errorlevel 1 echo [!] Update failed (no internet, or Python not on PATH). Will try existing data.

echo.
echo [2/2] Opening dashboard...
echo.

rem If a server is already listening on 8137, just open the browser
netstat -ano | findstr ":8137" | findstr "LISTENING" >nul
if %errorlevel%==0 (
  echo Server already running - opening browser...
  start "" "http://127.0.0.1:8137/index.html"
  ping -n 4 127.0.0.1 >nul
  goto :end
)

echo     URL:  http://127.0.0.1:8137/index.html
echo     Stop: close this window, or press Ctrl + C
echo.
start "" "http://127.0.0.1:8137/index.html"
python -m http.server 8137 --bind 127.0.0.1

echo.
echo [!] Server stopped, or failed to start.
echo     If it closed right away: port 8137 is in use, or Python is not installed / not on PATH.
pause

:end
endlocal
