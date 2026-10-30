@echo off
python -X utf8 "%~dp0listen.py" start %*
if errorlevel 1 pause
