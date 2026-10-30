@echo off
setlocal
title PDF Cleaner
cd /d "%~dp0"

if not exist "server.py" (
  echo.
  echo   PDF Cleaner can't find its files next to start.bat.
  echo.
  echo   If you opened start.bat from inside the ZIP file, extract the ZIP first:
  echo   right-click the ZIP file, choose "Extract All", then open start.bat
  echo   from the extracted folder.
  echo.
  pause
  exit /b 1
)

set "PYCMD="
where py >nul 2>nul && py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" <nul >nul 2>nul && set "PYCMD=py -3"
if not defined PYCMD (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" <nul >nul 2>nul && set "PYCMD=python"
)

if not defined PYCMD (
  echo.
  echo   PDF Cleaner needs Python 3.9 or newer, and it wasn't found on this computer.
  echo.
  echo   1. Go to https://www.python.org/downloads/windows/ and, under the newest
  echo      "Stable Release", click "Download Windows installer (64-bit)".
  echo   2. Open the downloaded installer. On its first screen, tick
  echo      "Add python.exe to PATH", then click "Install Now".
  echo   3. When it says the setup was successful, double-click start.bat again.
  echo.
  pause
  exit /b 1
)

%PYCMD% server.py %*
if errorlevel 1 (
  echo.
  echo   PDF Cleaner stopped because of an error. The message above explains what happened.
  echo   See "Troubleshooting" in README.md for help.
  echo.
  pause
)
