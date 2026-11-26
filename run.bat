@echo off
setlocal enabledelayedexpansion

:: Set current directory to the folder where run.bat is located
cd /d "%~dp0"

title Quick PDF Extractor - Legal Acts
color 0A

echo ============================================================
echo         QUICK PDF EXTRACTOR - LEGAL ACTS
echo ============================================================
echo.

:: 1. Check Python installation (check 'python' first, then 'py')
set "PY_CMD="

python --version >nul 2>nul
if %errorlevel% equ 0 (
    set "PY_CMD=python"
    goto :python_found
)

py -3 --version >nul 2>nul
if %errorlevel% equ 0 (
    set "PY_CMD=py -3"
    goto :python_found
)

py --version >nul 2>nul
if %errorlevel% equ 0 (
    set "PY_CMD=py"
    goto :python_found
)

echo [ERROR] Python install nahi mila!
echo Kripya Python 3.10 ya usse naya version python.org se install karein.
echo Install karte waqt "Add Python to PATH" checkbox par tick zaroor karein!
echo.
pause
exit /b 1

:python_found
echo [1/4] Python mil gaya: !PY_CMD!
echo.

:: 2. Create virtual environment if not already created
if exist "venv\Scripts\python.exe" (
    echo [2/4] Virtual environment pehle se bana hua hai.
    goto :activate_venv
)

echo [2/4] Virtual environment create ho raha hai... Kripya thoda wait karein...
!PY_CMD! -m venv venv
if %errorlevel% neq 0 (
    echo [ERROR] Virtual environment create karne me problem aayi!
    pause
    exit /b 1
)
echo [2/4] Virtual environment successfully ban gaya!

:activate_venv
echo.
echo [3/4] Requirements check / install ho rahi hain...
echo Ye pehli baar chalane par 1-2 minute le sakta hai...
call venv\Scripts\activate.bat

python -m pip install --upgrade pip --quiet
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Requirements install karne me error aaya!
    pause
    exit /b 1
)
echo [3/4] Sabhi requirements successfully install ho chuki hain!
echo.

:: 4. Ensure storage directories exist
if not exist "extractor_json" mkdir "extractor_json"
if not exist "outputs" mkdir "outputs"

:: 5. Open browser and start server
echo ============================================================
echo [4/4] Server start ho raha hai! Browser khul raha hai...
echo URL: http://localhost:8000
echo Server band karne ke liye ye window close karein ya Ctrl+C dabayein.
echo ============================================================
echo.

start "" http://localhost:8000

python -m uvicorn main:app --host 127.0.0.1 --port 8000

echo.
echo Server band ho gaya hai.
pause
