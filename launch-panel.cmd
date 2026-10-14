@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m trd_bot dashboard --open-browser
) else (
  python -m trd_bot dashboard --open-browser
)
pause
