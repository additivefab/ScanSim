@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe py -3 -m venv .venv
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto fail
.venv\Scripts\python.exe run_demo.py
if errorlevel 1 goto fail
start "" "results\default\viewer.html"
exit /b 0
:fail
echo Failed. Read the error above; verify Python and network access.
pause
exit /b 1
