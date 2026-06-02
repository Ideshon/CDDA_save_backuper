@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 cdda_save_backuper.py
) else (
    python cdda_save_backuper.py
)
if errorlevel 1 (
    echo.
    echo Error: Python 3 not found or the program crashed.
    echo Install Python 3 or run from terminal to see details.
    pause
)
