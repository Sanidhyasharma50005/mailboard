@echo off
setlocal enabledelayedexpansion

echo ========================================================
echo Building Mailboard for Windows
echo ========================================================

echo [1/3] Creating virtual environment (.venv)...
python -m venv .venv
if errorlevel 1 (
    echo Failed to create virtual environment.
    exit /b 1
)

echo [2/3] Installing requirements into .venv...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo Failed to install requirements.
    exit /b 1
)

echo [3/3] Packaging Mailboard.exe with PyInstaller...
pyinstaller --onefile --noconsole --name Mailboard --add-data "templates;templates" --hidden-import keyring.backends.Windows app.py
if errorlevel 1 (
    echo PyInstaller packaging failed.
    exit /b 1
)

if exist "dist\Mailboard.exe" (
    echo.
    echo ========================================================
    echo BUILD SUCCESSFUL!
    echo Executable is located at: %~dp0dist\Mailboard.exe
    echo ========================================================
) else (
    echo Build output dist\Mailboard.exe not found.
    exit /b 1
)
