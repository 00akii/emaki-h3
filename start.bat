@echo off
rem ---------------------------------------------------------------
rem  絵巻H3 / Emaki H3 — ダブルクリックで起動する
rem
rem  コマンドプロンプトを開かなくても使えるようにするためのもの。
rem  Python を順に探し、見つからなければ「どうすればいいか」を出して止まる。
rem  黙って閉じないこと（エラーを読む前に窓が消えるのが一番困る）。
rem ---------------------------------------------------------------
chcp 65001 >nul
cd /d "%~dp0"

rem --- Python を探す。EMAKI_PYTHON で明示指定もできる ---
set "PY="
if defined EMAKI_PYTHON if exist "%EMAKI_PYTHON%" set "PY=%EMAKI_PYTHON%"
if not defined PY where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
rem ComfyUI のポータブル版の隣に置いた場合。埋め込み Python は PATH に居ない
if not defined PY if exist "..\python_embeded\python.exe" set "PY=..\python_embeded\python.exe"
if not defined PY if exist "..\..\python_embeded\python.exe" set "PY=..\..\python_embeded\python.exe"

if not defined PY (
  echo.
  echo   Python が見つかりませんでした。
  echo.
  echo   ComfyUI のポータブル版をお使いなら、その中の Python を指定できます:
  echo       set EMAKI_PYTHON=C:\...\ComfyUI_windows_portable\python_embeded\python.exe
  echo       start.bat
  echo.
  echo   入っていない場合は https://www.python.org/downloads/ から 3.10 以降を入れてください。
  echo   インストール時に「Add Python to PATH」にチェックを入れると、この探索で見つかります。
  echo.
  pause
  exit /b 1
)

rem --- 必要なパッケージが入っているか ---
%PY% -c "import fastapi, uvicorn" >nul 2>&1
if errorlevel 1 (
  echo.
  echo   必要なパッケージが入っていません。次を実行してください:
  echo.
  echo       %PY% -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

echo 絵巻H3 を起動します（%PY%）。ブラウザで http://127.0.0.1:8765 を開いてください。
echo この窓を閉じるとアプリも止まります。
echo.
%PY% server.py %*

rem 落ちたときに理由を読めるよう、閉じずに待つ
echo.
echo アプリが終了しました。
pause
