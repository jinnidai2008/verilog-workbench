@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Workbench - publishing to the internet

if not exist "cloudflared.exe" (
  echo.
  echo   [ERROR] cloudflared.exe is missing from this folder.
  echo   Get it from:
  echo     https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe
  echo   save it here as cloudflared.exe, then run this again.
  echo.
  pause
  exit /b 1
)

set PY=
where python >nul 2>nul && set PY=python
if "%PY%"=="" ( where py >nul 2>nul && set PY=py )
if "%PY%"=="" ( echo   [ERROR] Python not found on PATH. & pause & exit /b 1 )

for /f "delims=" %%t in ('"%PY%" server.py --show-token') do set TOKEN=%%t

echo.
echo   ==========================================================
echo     Workbench - publishing to the internet
echo   ==========================================================
echo.
echo   This gives you a temporary public https address.
echo   Send the link below to whoever needs to look at it.
echo   Nobody else can open it - they get a "token required" page.
echo.
echo   TWO minimised windows will open. KEEP BOTH OPEN -
echo   closing either one takes the site offline.
echo.

rem ================================================================ 1. server
rem IMPORTANT: server.py runs forever (it IS the web server), so it MUST be
rem put into its own window. If we call it directly here, this script never
rem gets past this line and the tunnel is never started. That was a real bug
rem found by actually clicking the shortcut and watching nothing happen.
echo   [1/3] starting the workbench server on port 8787 ...
start "workbench-server" /min "%PY%" server.py 8787 --host 0.0.0.0 --no-browser

rem wait until it is really listening (up to ~40 s)
set SRVOK=NO
for /l %%i in (1,1,20) do (
  if "!SRVOK!"=="NO" (
    ping -n 3 127.0.0.1 >nul
    for /f "tokens=*" %%L in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":8787"') do set SRVOK=YES
  )
)
if "!SRVOK!"=="NO" (
  echo.
  echo   [ERROR] the workbench server did not come up on port 8787.
  echo   Look at the minimised "workbench-server" window for the reason.
  echo   Most likely port 8787 is already used by something else.
  echo.
  pause
  exit /b 1
)
echo         ok, listening on 8787.

rem ================================================================ 2. tunnel
rem Traps found the hard way, all by actually clicking the shortcut:
rem   * Do NOT wrap this in a quoted "cmd /c ..." - the redirect inside
rem     nested quotes silently fails and the tunnel never starts.
rem   * cloudflared logs ONLY to stderr, never stdout.
rem   * Its lines look like
rem       2026-09-30T17:01:44Z INF |  https://xxx.trycloudflare.com  |
rem     so a fixed "tokens=3" grabs the wrong word. Parsing this reliably in
rem     batch is painful - so we let cloudflared write its own --logfile and
rem     pull the address out with PowerShell regex instead.
echo   [2/3] opening the cloudflare tunnel ...
del "cf_tunnel.log" >nul 2>nul

rem Write the log next to this script, NOT into %TEMP%.
rem Why: %TEMP% expands to an 8.3 short name (C:\Users\JINNID~1\...), and
rem cloudflared's --logfile then fails with "Access is denied" because it
rem cannot create a file through that alias. Learned by testing, not guessing.
set LOGFILE=%~dp0cf_tunnel.log
start "cloudflare-tunnel" /min cmd /c "cloudflared.exe tunnel --url http://127.0.0.1:8787 --no-autoupdate --logfile ""%LOGFILE%"" 2>>""%LOGFILE%"""

call :graburl
if "!PUBURL!"=="" (
  echo         still starting, waiting a bit more ...
  for /l %%i in (1,1,25) do (
    if "!PUBURL!"=="" (
      ping -n 3 127.0.0.1 >nul
      call :graburl
    )
  )
)

if not "!PUBURL!"=="" echo !PUBURL!> public-url.txt

rem ---- also rebuild the lightweight demo page (no 7 MB video) ----
rem The normal page is 10 MB because the background video is inlined as base64,
rem which is painfully slow over a tunnel (phones show text but no video).
rem demo.html carries the same recorded results with a gradient instead of the
rem video: about 230 KB, loads instantly, and needs no token.
"%PY%" build_demo.py
goto :result

rem ---------------------------------------------------------------- helper
rem The parsing lives in get-public-url.ps1 on purpose.
rem Doing it inline here meant fighting batch escaping (^|, nested quotes,
rem % expansion) and it kept corrupting PowerShell's syntax. A separate .ps1
rem has none of those problems.
:graburl
set PUBURL=
for /f "delims=" %%u in ('powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0get-public-url.ps1" -LogFile "%LOGFILE%" 2^>nul') do set PUBURL=%%u
exit /b 0

:result

rem ================================================================ 3. result
echo   [3/3] done.
echo.
if "!PUBURL!"=="" (
  echo   ------------------------------------------------------------
  echo    Could not read the public address automatically.
  echo.
  echo    Open the minimised "cloudflare-tunnel" window and look for
  echo    the line:   Your quick Tunnel has been created!
  echo    then build the link yourself:
  echo.
  echo      ^<that address^>/workbench.html?token=!TOKEN!
  echo.
  echo    If that window shows an error or vanished, check the log:
  echo      %TEMP%\cf_tunnel.log
  echo   ------------------------------------------------------------
) else (
  echo   ==========================================================
  echo     SEND THIS LINK   ^(recommended - fast, works on phones^)
  echo   ==========================================================
  echo.
  echo   !PUBURL!/demo.html
  echo.
  echo   ==========================================================
  echo.
  echo   That one is about 230 KB and loads instantly. It shows the
  echo   recorded probe results; nothing to install, no password.
  echo.
  echo   If you want them to actually RUN a probe live, send this one
  echo   instead ^(slower: 10 MB, and needs the password^):
  echo.
  echo   !PUBURL!/workbench.html?token=!TOKEN!
  echo.
  echo   ==========================================================
  echo.
  echo   Notes:
  echo     - The address changes every time you run this script,
  echo       so send the new link again each time.
  echo     - First load can take a few seconds: the page is about
  echo       10 MB, plus another 7 MB for the background video.
  echo     - To stop publishing: close this window AND both
  echo       minimised windows.
  echo.
  echo   Press any key to open the link here, so you can check it...
  pause >nul
  start "" "!PUBURL!/workbench.html?token=!TOKEN!"
)

echo.
echo   This window is only a log now.
echo   Run "check-status.bat" any time to see whether the site is up.
echo.
pause
endlocal
