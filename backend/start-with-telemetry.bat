@echo off
REM 启动 backend（带 telemetry hook）
REM 用法：双击或在 PowerShell 里 .\start-with-telemetry.bat

echo ============================================
echo  Starting backend with telemetry hook
echo ============================================

python start-with-telemetry.py

echo.
echo Backend exited with code %errorlevel%