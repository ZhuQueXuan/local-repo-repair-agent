@echo off
chcp 65001 >nul
setlocal
pushd "%~dp0"
".venv\Scripts\python.exe" -B "scripts\run_regression.py" %*
set "regression_exit_code=%errorlevel%"
echo.
if "%regression_exit_code%"=="0" (echo Regression finished. See the Summary path above.) else (echo Regression found failures or environment restrictions. See the logs above.)
popd
pause
exit /b %regression_exit_code%
