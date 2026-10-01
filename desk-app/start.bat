@echo off
setlocal
cd /d "%~dp0"
title Workbench

echo.
echo   ==========================================
echo     Workbench - starting up
echo   ==========================================
echo.

rem ---- locate python ----
set PY=
where python >nul 2>nul && set PY=python
if "%PY%"=="" ( where py >nul 2>nul && set PY=py )
if "%PY%"=="" (
  echo   [ERROR] Python not found on PATH.
  echo   Install Python or add python.exe to PATH, then retry.
  echo.
  pause
  exit /b 1
)
echo   python : %PY%

rem ---- probe the verilog project for real data ----
echo   probing verilog project ...
"%PY%" refresh.py
if errorlevel 1 (
  echo.
  echo   [WARN] probe incomplete - starting with previous data.
  echo.
)

rem ---- rebuild both html files ----
"%PY%" build.py

rem ---- serve and open the browser ----
echo.
echo   starting local server (close this window to stop)
echo.
"%PY%" server.py 8787

rem If the server reused an already-running instance (or failed), it exits at
rem once - pause so the message stays visible instead of the window flashing shut.
if not errorlevel 1 (
  echo.
  echo   ------------------------------------------------------------
  echo    The server process has exited.
  echo    If it said "already running", that is normal - just use the
  echo    address it printed. If the page shows "connection failed",
  echo    reopen it with that exact address.
  echo   ------------------------------------------------------------
  echo.
  pause
)

endlocal
