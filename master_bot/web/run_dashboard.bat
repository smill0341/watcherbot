@echo off
setlocal

REM go to master_bot folder (one level above web\)
cd /d "%~dp0.."
echo [1/5] Working dir: %cd%

REM venv is one level above master_bot, i.e. D:\bot\.venv
set VENV_ACTIVATE=..\.venv\Scripts\activate.bat

if not exist "%VENV_ACTIVATE%" (
    echo.
    echo ERROR: not found %VENV_ACTIVATE%
    echo Expected venv at D:\bot\.venv
    echo If your venv is elsewhere, edit VENV_ACTIVATE in this file.
    echo.
    pause
    exit /b 1
)
echo [2/5] venv found: %VENV_ACTIVATE%

call "%VENV_ACTIVATE%"
echo [3/5] venv activated

if not exist "web\backend\requirements1.txt" (
    echo.
    echo ERROR: not found web\backend\requirements1.txt
    echo Check the web folder structure.
    echo.
    pause
    exit /b 1
)

pip show fastapi >nul 2>&1
if errorlevel 1 (
    echo [4/5] Installing dashboard dependencies...
    pip install -r web\backend\requirements1.txt
) else (
    echo [4/5] Dependencies already installed
)

if not exist "web\backend\app.py" (
    echo.
    echo ERROR: not found web\backend\app.py
    echo.
    pause
    exit /b 1
)

echo [5/5] Starting server at http://localhost:8010
python -m uvicorn web.backend.app:app --reload --port 8010

pause