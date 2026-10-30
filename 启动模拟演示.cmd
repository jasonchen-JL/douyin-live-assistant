@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-services.ps1" -Mode Demo -OpenBrowser
if errorlevel 1 pause
