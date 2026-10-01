@echo off
setlocal
cd /d "%~dp0"
title Workbench (LAN)
set PY=
where python >nul 2>nul && set PY=python
if "%PY%"=="" ( where py >nul 2>nul && set PY=py )
if "%PY%"=="" (
  echo   [ERROR] Python not found on PATH.
  pause
  exit /b 1
)

echo.
echo   ==========================================================
echo     Workbench - LAN mode (accessible from other machines)
echo   ==========================================================
echo.
echo   This exposes the workbench on your local network with a
echo   password (token). Only people you give the full link to can
echo   open it. Anyone else gets a "need token" page.
echo.
echo   The token is stored in access_token.txt and stays the same
echo   across restarts, so you only have to send the link once.
echo.

rem ---- probe + build so the page has fresh data ----
"%PY%" refresh.py
"%PY%" build.py

echo.
echo   starting server on 0.0.0.0:8787 ...
echo   (close this window to stop)
echo.
"%PY%" server.py 8787 --host 0.0.0.0

echo.
echo   server exited.
pause
endlocal
