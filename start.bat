@echo off
setlocal EnableExtensions DisableDelayedExpansion
title ThesisHelper
cd /d "%~dp0"
if errorlevel 1 goto folder_error
if not exist ".venv\Scripts\python.exe" goto not_installed
echo.
echo Starting ThesisHelper. Keep this window open while you work.
echo Press Ctrl+C in this window to stop the server.
echo.
".venv\Scripts\python.exe" "run.py" %*
set "TH_RESULT=%errorlevel%"
if "%TH_RESULT%"=="0" exit /b 0
echo.
echo ThesisHelper stopped with an error. See the message above.
pause
exit /b %TH_RESULT%

:not_installed
echo Please run install.bat first.
pause
exit /b 1

:folder_error
echo Extract the whole project to a writable local folder before starting.
pause
exit /b 1
