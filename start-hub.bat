@echo off
rem Starts the PlaStim hub (http://127.0.0.1:8320). Close this window, or press Ctrl+C, to quit:
rem the output is brought to zero and the box stopped first.
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
    echo The app is not installed yet: see INSTALL.md, step 4.
    pause
    exit /b 1
)
"venv\Scripts\python.exe" -m stimengine.app
pause
