@echo off
setlocal
cd /d "%~dp0"

if not exist ".conda\python.exe" (
  echo Creating local conda environment in .conda ...
  conda --no-plugins env create --prefix "%CD%\.conda" --file environment.yml
  if errorlevel 1 (
    echo.
    echo Failed to create the conda environment.
    echo Check that conda is installed and that this VM can reach conda-forge and PyPI.
    pause
    exit /b 1
  )
)

".conda\python.exe" app\web_app.py
if errorlevel 1 pause
