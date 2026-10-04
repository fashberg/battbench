@echo off
rem Start BattBench from the source tree (for development).
rem The first run creates .venv and installs requirements.txt; later runs reinstall only when it changed.
rem Arguments are passed on, e.g.  run.bat --offline  or  run.bat --lang en
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating .venv ...
    py -3 -m venv .venv 2>nul || python -m venv .venv || goto :failed
)
fc /b requirements.txt .venv\requirements.installed >nul 2>&1 || (
    echo Installing requirements ...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :failed
    copy /y requirements.txt .venv\requirements.installed >nul
)
".venv\Scripts\python.exe" -m battbench %*
exit /b %errorlevel%

:failed
echo Setting up the Python environment failed (Python 3.10 or newer needed).
exit /b 1
