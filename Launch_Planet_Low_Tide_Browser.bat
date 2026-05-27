@echo off
setlocal
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1

py -3.11 --version >nul 2>&1
if errorlevel 1 (
  echo Python 3.11 is required for this app.
  echo.
  echo Ask IT to install Python 3.11 side-by-side with existing Python versions,
  echo and make sure this command works:
  echo   py -3.11 --version
  echo.
  pause
  exit /b 1
)

if not exist "tide\CSIRO_tidal_const_v12.nc" (
  echo CSIRO tide model file is missing.
  echo.
  echo Required file:
  echo   tide\CSIRO_tidal_const_v12.nc
  echo.
  echo Download it from:
  echo   https://data.csiro.au/collection/csiro:45584
  echo.
  echo The .nc file is intentionally not stored in this repository.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating local Python environment in .venv ...
  py -3.11 -m venv ".venv"
  if errorlevel 1 (
    echo.
    echo Failed to create the Python virtual environment.
    pause
    exit /b 1
  )

  echo Installing app packages ...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Failed to install packages.
    echo Check that this VM can access PyPI, or ask IT to allow package installs.
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" app\web_app.py
if errorlevel 1 pause
