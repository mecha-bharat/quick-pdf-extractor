@echo off
title Quick PDF Extractor - Legal Acts
color 0A

echo ============================================================
echo         QUICK PDF EXTRACTOR (LEGAL ACTS)
echo ============================================================
echo.

:: 1. Check Python installation
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python install nahi hai ya PATH me add nahi hai!
    echo Kripya Python 3.10+ install kare python.org se aur
    echo install karte waqt "Add Python to PATH" checkbox par tick kare.
    echo.
    pause
    exit /b 1
)

echo [1/4] Python found!

:: 2. Create virtual environment if it doesn't exist
if not exist "venv" (
    echo [2/4] Virtual environment create ho raha hai (venv)...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo [ERROR] Virtual environment create karne me problem aayi.
        pause
        exit /b 1
    )
) else (
    echo [2/4] Virtual environment (venv) already exists.
)

:: 3. Activate venv & install/update requirements
echo [3/4] Packages install / check ho rahe hain (requirements.txt)...
call venv\Scripts\activate.bat
python -m pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
if %errorlevel% neq 0 (
    echo [WARNING] Kuch packages install karne me issue aaya, retry kar rahe hain...
    pip install -r requirements.txt
)

:: 4. Ensure storage directories exist
if not exist "extractor_json" mkdir "extractor_json"
if not exist "outputs" mkdir "outputs"

:: 5. Open browser and start server
echo.
echo ============================================================
echo [4/4] Server ready! Browser automatically khul raha hai...
echo URL: http://localhost:8000
echo Server band karne ke liye ye window close kare ya Ctrl+C dabaye.
echo ============================================================
echo.

start "" http://localhost:8000

python -m uvicorn main:app --host 127.0.0.1 --port 8000

echo.
echo Server band ho gaya hai.
pause
