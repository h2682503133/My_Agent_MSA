@echo off
REM 双击运行：宿主代理（从 admin panel 取「浏览器环境」命令并执行）
REM 面板：http://127.0.0.1:5700 → 「浏览器环境」→ 启动环境
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0browser-boot.ps1"
echo.
echo [已退出] 宿主代理停止。按任意键关闭。
pause >nul
