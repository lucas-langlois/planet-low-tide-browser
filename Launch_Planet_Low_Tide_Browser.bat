@echo off
setlocal
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1
set "PYTHON_CMD="
set "APP_PYTHON="
set "SHARED_CONDA_ENV=L:\RES_Library\Conda_env\myenv"
set "CONDA_ENV_DIR=.conda"
set "VENV_DIR=.venv"
set "SETUP_MARKER=%VENV_DIR%\.planet_low_tide_setup_complete"
set "LOG_DIR=logs"
set "PIP_LOG=%LOG_DIR%\pip_install.log"

echo Planet Low Tide Browser launcher
echo.
echo App folder:
echo   %CD%
echo.
echo [1/5] Checking Python ...

py -3.11 --version >nul 2>&1
if not errorlevel 1 set "PYTHON_CMD=py -3.11"

if not defined PYTHON_CMD (
  py -3.10 --version >nul 2>&1
  if not errorlevel 1 set "PYTHON_CMD=py -3.10"
)

if not defined PYTHON_CMD (
  python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
  if not errorlevel 1 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
  echo Python 3.10 or newer is required for this app.
  echo.
  echo Install Python 3.10 or 3.11, then make sure one of these commands works:
  echo   py -3.11 --version
  echo   py -3.10 --version
  echo   python --version
  echo.
  pause
  exit /b 1
)

echo Using Python command:
echo   %PYTHON_CMD%
%PYTHON_CMD% --version
echo.

echo [2/5] Checking CSIRO tide model ...
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
echo Found tide\CSIRO_tidal_const_v12.nc
echo.

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

if exist "%SHARED_CONDA_ENV%\python.exe" (
  set "APP_PYTHON=%SHARED_CONDA_ENV%\python.exe"
  echo [3/5] Existing conda environment found:
  echo   %SHARED_CONDA_ENV%
  echo.
) else if exist "%CONDA_ENV_DIR%\python.exe" (
  set "APP_PYTHON=%CONDA_ENV_DIR%\python.exe"
  echo [3/5] Existing project conda environment found:
  echo   %CONDA_ENV_DIR%
  echo.
)

if defined APP_PYTHON (
  "%APP_PYTHON%" -c "import flask, pandas, numpy, scipy, xarray, netCDF4, rasterio, requests, PIL, pytz, shapefile, pyproj, shapely, timezonefinder, planet, utide" >nul 2>&1
  if errorlevel 1 (
    echo [4/5] Conda environment is missing required pip packages.
    echo Installing app packages into:
    echo   %APP_PYTHON%
    echo Pip output is shown below and saved to:
    echo   %PIP_LOG%
    echo.
    echo Planet Low Tide Browser pip install log > "%PIP_LOG%"
    "%APP_PYTHON%" -m pip install -r requirements.txt --log "%PIP_LOG%"
    if errorlevel 1 (
      echo.
      echo Failed to install packages.
      echo Check that this VM can access PyPI, or ask IT to allow package installs.
      echo See:
      echo   %PIP_LOG%
      pause
      exit /b 1
    )
    "%APP_PYTHON%" -c "import flask, pandas, numpy, scipy, xarray, netCDF4, rasterio, requests, PIL, pytz, shapefile, pyproj, shapely, timezonefinder, planet, utide" >nul 2>&1
    if errorlevel 1 (
      echo.
      echo Package install finished, but required imports still failed.
      echo See:
      echo   %PIP_LOG%
      pause
      exit /b 1
    )
  ) else (
    echo [4/5] Conda package check passed.
    echo.
  )
) else (
  set "APP_PYTHON=%VENV_DIR%\Scripts\python.exe"
  if not exist "%APP_PYTHON%" (
    echo [3/5] Creating local Python environment in:
    echo   %VENV_DIR%
    echo.
    echo This can take a few minutes on a shared or network drive.
    echo If Python prints "Actual environment location may have moved", wait for
    echo the next launcher message: "Installing app packages".
    echo.
    %PYTHON_CMD% -m venv --without-pip "%VENV_DIR%"
    if errorlevel 1 (
      echo.
      echo Failed to create the Python virtual environment.
      pause
      exit /b 1
    )
  ) else if not exist "%SETUP_MARKER%" (
    echo [3/5] Existing local Python environment found, but package setup is incomplete:
    echo   %VENV_DIR%
    echo.
  )

  if exist "%SETUP_MARKER%" (
    "%APP_PYTHON%" -c "import flask, pandas, numpy, scipy, xarray, netCDF4, rasterio, requests, PIL, pytz, shapefile, pyproj, shapely, timezonefinder, planet, utide" >nul 2>&1
    if errorlevel 1 (
      echo [3/5] Existing local Python environment is missing required packages.
      echo Package setup will run again.
      echo.
      del "%SETUP_MARKER%" >nul 2>&1
    )
  )

  if not exist "%SETUP_MARKER%" (
    echo.
    echo [4/5] Installing app packages ...
    echo This is usually the slowest step on first run.
    echo Pip output is shown below and saved to:
    echo   %PIP_LOG%
    echo.
    echo Planet Low Tide Browser pip install log > "%PIP_LOG%"
    "%APP_PYTHON%" -m ensurepip --upgrade
    if errorlevel 1 (
      echo.
      echo Failed to install pip into the virtual environment. See:
      echo   %PIP_LOG%
      pause
      exit /b 1
    )
    "%APP_PYTHON%" -m pip install --upgrade pip --log "%PIP_LOG%"
    if errorlevel 1 (
      echo.
      echo Failed to upgrade pip. See:
      echo   %PIP_LOG%
      pause
      exit /b 1
    )
    "%APP_PYTHON%" -m pip install -r requirements.txt --log "%PIP_LOG%"
    if errorlevel 1 (
      echo.
      echo Failed to install packages.
      echo Check that this VM can access PyPI, or ask IT to allow package installs.
      echo See:
      echo   %PIP_LOG%
      pause
      exit /b 1
    )
    "%APP_PYTHON%" -c "import flask, pandas, numpy, scipy, xarray, netCDF4, rasterio, requests, PIL, pytz, shapefile, pyproj, shapely, timezonefinder, planet, utide" >nul 2>&1
    if errorlevel 1 (
      echo.
      echo Package install finished, but required imports still failed.
      echo See:
      echo   %PIP_LOG%
      pause
      exit /b 1
    )
    echo setup complete > "%SETUP_MARKER%"
  ) else (
    echo [3/5] Existing local Python environment found:
    echo   %VENV_DIR%
    echo.
    echo [4/5] Package install skipped. Delete .venv to rebuild it.
    echo.
  )
)

echo [5/5] Starting app ...
echo Open http://127.0.0.1:5050 if the browser does not open automatically.
echo.
"%APP_PYTHON%" app\web_app.py
if errorlevel 1 pause
