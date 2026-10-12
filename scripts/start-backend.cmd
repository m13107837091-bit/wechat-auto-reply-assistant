@echo off
rem 一键启动「微信自动回复」：拉起计划任务（看门狗 -> 托盘 -> 后端）。
rem 在托盘里点过「退出」之后它不会自己回来；双击桌面上的「启动自动回复」图标即可。
chcp 65001 >nul
setlocal
set "TASK=wechat-bot-backend"

echo.
echo   正在启动「微信自动回复」...
schtasks /Run /TN "%TASK%" >nul 2>&1
if errorlevel 1 goto failed

echo.
echo   已发出启动指令，约 20 秒后可用。
echo   手机/浏览器打开： http://computer333:8000/
echo.
echo   确认它起没起来：看右下角托盘的绿色图标，或等半分钟刷新面板。
echo   这个窗口 8 秒后自己关掉，不用管它。
ping -n 9 127.0.0.1 >nul
exit /b 0

:failed
echo.
echo   [x] 启动失败：计划任务 "%TASK%" 没能运行。
echo       1^) 在 PowerShell 里看它还在不在： Get-ScheduledTask -TaskName %TASK%
echo       2) 不在就重新注册： scripts\install-autostart.ps1
echo.
pause
exit /b 1
