@echo off
setlocal

REM Запускать из корня master_bot\ (рядом с main.py и run_web.py)
cd /d "%~dp0"
echo [1/4] Working dir: %cd%

REM venv на уровень выше master_bot, т.е. D:\bot\.venv
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
echo [2/4] venv found: %VENV_ACTIVATE%

call "%VENV_ACTIVATE%"
echo [3/4] venv activated

if not exist "run_web.py" (
    echo.
    echo ERROR: not found run_web.py in %cd%
    echo.
    pause
    exit /b 1
)

if not exist "web\backend\app.py" (
    echo.
    echo ERROR: not found web\backend\app.py
    echo.
    pause
    exit /b 1
)

pip show fastapi >nul 2>&1
if errorlevel 1 (
    echo Installing dashboard dependencies...
    pip install -r web\backend\requirements1.txt
)

echo [4/4] Starting scanner + dashboard in separate windows...

REM Окно 1: сканер без Telegram (тут же можно вводить "rebuild" для ручного пересчета уровней)
start "Watcherbot Scanner (run_web.py)" cmd /k "call "%VENV_ACTIVATE%" && python run_web.py"

REM Окно 2: веб-дашборд
REM ВАЖНО: без --reload. candles.db лежит в той же папке, что и app.py, и
REM обновляется постоянно (докачка истории) — с --reload uvicorn видит это
REM как "файл изменился" и рестартует сервер, обрывая докачку на середине.
REM Из-за этого некоторые монеты застревали на июле, пока другие (которым
REM повезло не попасть под рестарт) доходили до августа.
start "Watcherbot Dashboard" cmd /k "call "%VENV_ACTIVATE%" && python -m uvicorn web.backend.app:app --port 8010"

echo.
echo Готово. Открыты два окна: Scanner и Dashboard.
echo Дашборд:  http://localhost:8010
echo В окне Scanner можно ввести "rebuild" + Enter для ручного пересчета уровней.
echo.
pause