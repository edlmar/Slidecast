@echo off
rem slidecast installer: double-click. Needs internet the first time (~10 GB of downloads).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1"
echo.
pause
