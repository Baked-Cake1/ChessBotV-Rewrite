@echo off
title ChessBotV Launcher

REM Change to script directory
cd /d "%~dp0"

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo Error: Python is not installed or not in PATH
    echo Please install Python from https://python.org - preferrably, Python 3.14 or later.
    pause
    exit /b 1
)

REM Run main Python script with any command line arguments passed through
:launch
echo Initalizing launcher...
echo Launching ChessBotV Rewrite...
echo.
python new.py %*

set PYTHON_EXIT_CODE=%errorlevel%

REM Restart handling (code 42 from in-app Restart button)
if %PYTHON_EXIT_CODE%==42 (
    echo Restarting ChessBotV Rewrite...
    timeout /t 1 >nul
    goto launch
)

REM Automatically close when UI closes normally; keep open on error so crash details remain visible
if %PYTHON_EXIT_CODE% equ 0 (
    exit /b 0
) else (
    echo.
    echo ChessBotV Rewrite closed with error code: %PYTHON_EXIT_CODE%!
    echo.
    echo If you see this message, ChessBotV Rewrite may have crashed.
    echo Check the error messages above ^ for details.
    echo.
    echo Press any key to exit the ChessBotV launcher...
    pause >nul
    exit /b %PYTHON_EXIT_CODE%
)
