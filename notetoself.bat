@echo off
rem Note to Self launcher for Windows. Double-click it, or run: notetoself.bat [--dir FOLDER] [--port N]
setlocal
set "DIR=%~dp0"
if exist "%DIR%.venv\Scripts\python.exe" (
  "%DIR%.venv\Scripts\python.exe" "%DIR%notetoself.py" %*
  goto :end
)
where py >nul 2>nul
if not errorlevel 1 (
  py -3 "%DIR%notetoself.py" %*
  goto :end
)
where python >nul 2>nul
if not errorlevel 1 (
  python "%DIR%notetoself.py" %*
  goto :end
)
echo Note to Self needs Python 3.8 or newer: https://www.python.org/downloads/
pause
exit /b 1
:end
if errorlevel 1 pause
