@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Workbench - status check

echo.
echo   ==========================================================
echo     Workbench status check
echo   ==========================================================
echo.

rem ---- 1. is the workbench server listening on 8787? ----
set SRV=NO
for /f "tokens=*" %%L in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":8787"') do set SRV=YES

rem ---- 2. is the cloudflare tunnel process alive? ----
set TUN=NO
for /f "tokens=*" %%L in ('tasklist /nh /fi "imagename eq cloudflared.exe" 2^>nul ^| findstr /i "cloudflared"') do set TUN=YES

rem ---- 3. token + last known public address ----
set TOKEN=
if exist access_token.txt set /p TOKEN=<access_token.txt

set PUBURL=
if exist "public-url.txt" (
  for /f "delims=" %%u in ('findstr /r /c:"https://.*trycloudflare.com" "public-url.txt" 2^>nul') do set PUBURL=%%u
)
if "!PUBURL!"=="" if exist "%TEMP%\cf_tunnel.log" (
  for /f "tokens=3" %%u in ('findstr /r /c:"https://.*trycloudflare.com" "%TEMP%\cf_tunnel.log" 2^>nul') do set PUBURL=%%u
)

echo   workbench server (port 8787) : !SRV!
echo   cloudflare tunnel            : !TUN!
echo.

set ONLINE=NO
if "!SRV!"=="YES" if "!TUN!"=="YES" set ONLINE=YES

if "!ONLINE!"=="YES" goto :online
goto :offline

:online
echo   ----------------------------------------------------------
echo    STATUS: ONLINE - your teacher can open the site right now
echo   ----------------------------------------------------------
echo.
if not "!PUBURL!"=="" (
  echo    The link you sent should still work:
  echo.
  echo    !PUBURL!/workbench.html?token=!TOKEN!
  echo.
) else (
  echo    Look at the "cloudflared" window for the line:
  echo      Your quick Tunnel has been created!
  echo    then append:  /workbench.html?token=!TOKEN!
  echo.
)
echo    Keep BOTH windows open. Closing either one takes it offline.
echo.
goto :done

:offline
echo   ----------------------------------------------------------
echo    STATUS: OFFLINE - your teacher CANNOT open the site
echo   ----------------------------------------------------------
echo.
if "!SRV!"=="NO" echo    [x] the workbench server is not running
if "!TUN!"=="NO" echo    [x] the cloudflare tunnel is not running
echo.
echo    To go back online: double-click  start-public.bat
echo.
echo    IMPORTANT - you will get a NEW address.
echo    The old link stops working, so send the new one again.
echo.
if not "!PUBURL!"=="" (
  echo    Previous address (now dead): !PUBURL!
  echo.
)
goto :done

:done
echo   ----------------------------------------------------------
pause
endlocal
