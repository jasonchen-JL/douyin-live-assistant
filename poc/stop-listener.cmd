@echo off
python -X utf8 "%~dp0listen.py" stop %*
if errorlevel 1 pause
