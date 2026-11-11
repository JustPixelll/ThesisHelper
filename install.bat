@echo off
setlocal EnableExtensions DisableDelayedExpansion
title ThesisHelper - Install
cd /d "%~dp0"
if errorlevel 1 goto folder_error

echo.
echo  ThesisHelper - Windows installation
echo  This creates a private Python environment in this folder.
echo  Internet access is needed to download the Python dependencies.
echo.

set "TH_PY="
py -3.12 -c "import sys,struct; sys.exit(0 if struct.calcsize('P') == 8 else 1)" >nul 2>&1
if not errorlevel 1 set "TH_PY=py -3.12"
if defined TH_PY goto python_found
py -3.11 -c "import sys,struct; sys.exit(0 if struct.calcsize('P') == 8 else 1)" >nul 2>&1
if not errorlevel 1 set "TH_PY=py -3.11"
if defined TH_PY goto python_found
py -3.13 -c "import sys,struct; sys.exit(0 if struct.calcsize('P') == 8 else 1)" >nul 2>&1
if not errorlevel 1 set "TH_PY=py -3.13"
if defined TH_PY goto python_found
python -c "import sys,struct; sys.exit(0 if (3,11) <= sys.version_info[:2] < (3,14) and struct.calcsize('P') == 8 else 1)" >nul 2>&1
if not errorlevel 1 set "TH_PY=python"
if not defined TH_PY goto missing_python

:python_found
if not exist "requirements.txt" goto folder_error
if exist ".venv\Scripts\python.exe" goto check_environment
echo Creating the local Python environment...
%TH_PY% -m venv ".venv"
if errorlevel 1 goto environment_error

:check_environment
".venv\Scripts\python.exe" -c "import sys,struct; sys.exit(0 if (3,11) <= sys.version_info[:2] < (3,14) and struct.calcsize('P') == 8 else 1)"
if errorlevel 1 goto environment_error
echo Installing application dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto install_error
".venv\Scripts\python.exe" -m pip install -r "requirements.txt"
if errorlevel 1 goto install_error
".venv\Scripts\python.exe" -c "import fastapi,uvicorn,docx,pypdf,numpy; print('Core dependencies are ready.')"
if errorlevel 1 goto install_error
echo.
echo Installation complete.
echo Next: double-click start.bat, then enter your OpenAI API key in Settings.
echo Put source PDFs and DOCX files in the ingest folder.
echo.
pause
exit /b 0

:missing_python
echo.
echo A supported 64-bit Python installation was not found.
echo Install Python 3.12 from https://www.python.org/downloads/windows/
echo During setup, select "Add python.exe to PATH" and the Python launcher.
echo Then run install.bat again.
echo Python 3.11 and 3.13 are also supported; 3.12 is recommended.
pause
exit /b 1

:environment_error
echo.
echo The local Python environment could not be created or is no longer valid.
echo If you moved this folder or upgraded Python, delete ONLY the .venv folder
echo and run install.bat again. Keep ingest, data, and workspace.
echo Also check that this folder is writable by your Windows user.
pause
exit /b 1

:install_error
echo.
echo Installation did not finish. The error is shown above.
echo Check your internet connection, proxy settings, and free disk space.
echo Run install.bat again to retry; your documents and source files are kept.
pause
exit /b 1

:folder_error
echo.
echo ThesisHelper cannot read its project folder.
echo Extract the whole downloaded ZIP before running install.bat.
echo Do not run it inside the ZIP preview or from a read-only folder.
pause
exit /b 1
