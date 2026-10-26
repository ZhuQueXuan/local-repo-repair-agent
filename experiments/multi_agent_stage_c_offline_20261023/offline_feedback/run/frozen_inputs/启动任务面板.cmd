@echo off
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
  echo Project Python environment not found. See README.md.
  pause
  exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" -B "%~dp0scripts\launch_task_panel.pyw"
