@echo off
REM PBS Job Monitor v2.0 - FastAPI + React Dashboard Launcher
REM Place this file in the root of the installation folder (alongside backend\ and frontend\).
REM Uses %~dp0 (self-relative path) so no hardcoded paths need changing.

REM -----------------------------------------------------------------------
REM Check Python is available
REM -----------------------------------------------------------------------
where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: python not found on PATH.
    echo Please install Python and ensure it is added to your PATH.
    pause
    exit /b 1
)

REM -----------------------------------------------------------------------
REM Start FastAPI backend in a new window
REM -----------------------------------------------------------------------
echo Starting PBS Job Monitor on port 8000...
start "PBS Dashboard - FastAPI" /D "%~dp0backend" python -m uvicorn main:app --host 0.0.0.0 --port 8000

REM Wait for server to be ready
timeout /t 3 /nobreak >nul

REM -----------------------------------------------------------------------
REM Open browser
REM -----------------------------------------------------------------------
start http://localhost:8000

echo.
echo Dashboard : http://localhost:8000
echo API docs  : http://localhost:8000/docs
echo.
echo Close the "PBS Dashboard - FastAPI" window to stop the server.
