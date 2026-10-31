@echo off
rem Double-click to run. First run creates a private Python environment and installs the libraries.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Setting up for the first time...
    py -3.12 -m venv .venv 2>nul || py -3 -m venv .venv 2>nul || python -m venv .venv
    if not exist ".venv\Scripts\python.exe" (
        echo Could not create a Python environment. Install Python 3.12 from python.org, then run this again.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Library install failed - see the messages above.
        pause
        exit /b 1
    )
)
".venv\Scripts\python.exe" raw_review_app.py %*
if errorlevel 1 pause
