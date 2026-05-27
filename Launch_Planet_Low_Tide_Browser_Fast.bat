@echo off
setlocal
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1

set "SHARED_CONDA_ENV=L:\RES_Library\Conda_env\myenv"
set "APP_PYTHON=%SHARED_CONDA_ENV%\python.exe"
set "LOG_DIR=logs"
set "FAST_MARKER=%LOG_DIR%\shared_conda_env_ok.marker"

echo Planet Low Tide Browser fast launcher
echo.
echo App folder:
echo   %CD%
echo.

if not exist "tide\CSIRO_tidal_const_v12.nc" (
  echo CSIRO tide model file is missing.
  echo.
  echo Required file:
  echo   tide\CSIRO_tidal_const_v12.nc
  echo.
  echo Download it from:
  echo   https://data.csiro.au/collection/csiro:45584
  echo.
  pause
  exit /b 1
)

if not exist "%APP_PYTHON%" (
  echo Shared conda environment was not found:
  echo   %SHARED_CONDA_ENV%
  echo.
  echo Use Launch_Planet_Low_Tide_Browser.bat for setup and diagnostics.
  pause
  exit /b 1
)

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

if not exist "%FAST_MARKER%" (
  echo First fast launch: checking shared conda packages once ...
  "%APP_PYTHON%" -c "import flask, pandas, numpy, scipy, xarray, netCDF4, rasterio, requests, PIL, pytz, shapefile, pyproj, shapely, timezonefinder, planet, utide" >nul 2>&1
  if errorlevel 1 (
    echo.
    echo The shared conda environment is missing required packages.
    echo Run Launch_Planet_Low_Tide_Browser.bat to install or diagnose packages.
    pause
    exit /b 1
  )
  echo ok > "%FAST_MARKER%"
)

echo Starting app with:
echo   %APP_PYTHON%
echo.
echo Open http://127.0.0.1:5050 if the browser does not open automatically.
echo.
"%APP_PYTHON%" app\web_app.py
if errorlevel 1 pause
