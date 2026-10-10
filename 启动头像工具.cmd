@echo off
cd /d "%~dp0"
python portrait_server.py
if errorlevel 1 pause
