@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM 国服客户端以管理员权限运行时，命令行检测需要管理员权限；lockfile 检测不需要。
where py >nul 2>nul && (py -3 cn_bridge.py %*) || (python cn_bridge.py %*)
pause

