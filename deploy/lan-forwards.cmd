@echo off
REM 双击运行：把 gateway 的 5210 / 3080 / 5700 以 0.0.0.0 转发到本机，
REM 局域网其它设备即可访问；掉线后自动重连（3 秒）。
REM 关闭这个窗口 = 停止转发。
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0lan-forwards.ps1"
echo.
echo [已退出] 转发停止。按任意键关闭。
pause >nul
