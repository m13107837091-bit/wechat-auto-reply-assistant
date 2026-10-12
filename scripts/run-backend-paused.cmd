@echo off
rem ---------------------------------------------------------------------
rem run-backend-paused.cmd
rem Start the backend WITHOUT any window (phone-first usage).
rem Auto-reply on startup: PAUSED (connects to WeChat, does NOT reply until you start it in the panel)
rem Logs: <repo>\wechatauto_logs\app_YYYYMMDD.log
rem ---------------------------------------------------------------------
setlocal
for %%I in ("%~dp0..") do set "REPO=%%~fI"
if "%BOT_PYTHON%"=="" set "BOT_PYTHON=C:\Users\22069\anaconda3\envs\pytorch\pythonw.exe"
set "BOT_AUTOREPLY_ON_START=0"

if not exist "%BOT_PYTHON%" (
  echo [ERROR] Python not found:
  echo         %BOT_PYTHON%
  echo         Set BOT_PYTHON to your pythonw.exe path, then retry.
  pause
  exit /b 1
)
if not exist "%REPO%\src\main.py" (
  echo [ERROR] Backend entry not found:
  echo         %REPO%\src\main.py
  pause
  exit /b 1
)

rem Refuse to start a second copy. Python's HTTPServer used to set
rem SO_REUSEADDR, which on Windows let two processes bind port 8000 at once
rem and silently split the panel between them. The backend itself now also
rem refuses to double-bind; this check just gives a clearer message.
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }"
if errorlevel 1 (
  echo [SKIP] A backend is already listening on port 8000. Nothing started.
  echo        Panel: http://127.0.0.1:8000/
  echo        To switch between auto-reply ON and PAUSED, close the running
  echo        pythonw.exe process first, then run this launcher again.
  pause
  exit /b 0
)

echo Starting backend with auto-reply PAUSED ...
start "wechat-bot backend (paused)" /D "%REPO%" "%BOT_PYTHON%" "%REPO%\src\main.py"
timeout /t 3 /nobreak >nul
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) { Write-Host 'OK  - backend is listening on port 8000' -ForegroundColor Green } else { Write-Host 'WARN - port 8000 not listening yet; check wechatauto_logs' -ForegroundColor Yellow }"
echo.
echo Panel on this PC : http://127.0.0.1:8000/
echo Logs             : %REPO%\wechatauto_logs\
echo.
echo (This window can be closed - the backend keeps running in the background.)
pause
