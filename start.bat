@echo off
rem start.bat — 双击启动（选择量化模型）; 也可: start.bat Q5_K_S
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
pause
