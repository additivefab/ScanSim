@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (echo Install Python 3.13 or another supported Python 3.11+ first. & exit /b 2)
if not exist .venv\Scripts\python.exe py -3 -m venv .venv
if errorlevel 1 exit /b 2
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 exit /b 2
.venv\Scripts\python.exe -m unittest discover -s tests -v
if errorlevel 1 exit /b 2
.venv\Scripts\python.exe run_sim.py --config configs\ad9268_50m_5x.json --output examples\ad9268_50m_5x
if errorlevel 1 exit /b 2
start "" "examples\ad9268_50m_5x\viewer.html"
