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

echo [4/4] Starting scanner + dashboard (single process)...

REM Раньше тут были ДВА отдельных окна: сканер (run_web.py) и дашборд
REM (python -m uvicorn web.backend.app:app --port 8010) — два процесса,
REM у каждого своя, независимая копия состояния вотчеров в памяти, из-за
REM чего дашборд то удалял, то "воскрешал" одних и тех же вотчеров сам
REM по себе, списки расходились и т.д. После слияния run_web.py сам
REM поднимает дашборд (uvicorn) внутри себя — один процесс, одна копия
REM состояния, окно теперь одно.
start "Watcherbot (scanner + dashboard)" cmd /k "call "%VENV_ACTIVATE%" && python run_web.py"

echo.
echo Готово. Открыто одно окно: Watcherbot (scanner + dashboard).
echo Дашборд:  http://localhost:8010
echo В этом же окне можно ввести "rebuild" + Enter для ручного пересчета уровней.
echo.
pause