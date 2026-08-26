@echo off
rem ---------------------------------------------------------------
rem  Emaki H3 - double-click to start.
rem
rem  ASCII ONLY. Do not put Japanese text in this file.
rem  cmd reads .bat in the system codepage (CP932 on Japanese Windows),
rem  where some characters have 0x5C or 0x7C as their second byte and
rem  break the line. See README for the Japanese instructions.
rem ---------------------------------------------------------------
cd /d "%~dp0"

rem --- find a Python. EMAKI_PYTHON overrides the search ---
set "PY="
if defined EMAKI_PYTHON if exist "%EMAKI_PYTHON%" set "PY=%EMAKI_PYTHON%"
if not defined PY where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
rem ComfyUI portable ships its own Python, which is not on PATH
if not defined PY if exist "..\python_embeded\python.exe" set "PY=..\python_embeded\python.exe"
if not defined PY if exist "..\..\python_embeded\python.exe" set "PY=..\..\python_embeded\python.exe"

if not defined PY (
  echo.
  echo   Python was not found.
  echo.
  echo   Using the ComfyUI portable build? Point this script at its Python:
  echo       set EMAKI_PYTHON=C:\...\ComfyUI_windows_portable\python_embeded\python.exe
  echo       start.bat
  echo.
  echo   Otherwise install Python 3.10 or later from
  echo       https://www.python.org/downloads/
  echo   and tick "Add Python to PATH" during setup.
  echo.
  echo   README.md has the same steps in Japanese.
  echo.
  pause
  exit /b 1
)

rem --- are the packages installed? ---
%PY% -c "import fastapi, uvicorn" >nul 2>&1
if errorlevel 1 (
  echo.
  echo   Required packages are missing. Run:
  echo.
  echo       %PY% -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

echo Starting Emaki H3 with %PY%
echo Open http://127.0.0.1:8765 in your browser.
echo Closing this window stops the app.
echo.
%PY% server.py %*

rem keep the window so the reason stays readable if it crashes
echo.
echo Emaki H3 has stopped.
pause
