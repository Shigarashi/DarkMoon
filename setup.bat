@echo off
setlocal
title Darkmoon setup
cd /d "%~dp0"

echo.
echo  === Darkmoon setup ===
echo.

REM Prefer the Python launcher with 3.12, then any 3.11+.
set "PY="
py -3.12 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=py -3.12"
if not defined PY (
  py -3 -c "import sys; assert sys.version_info >= (3,11)" >nul 2>nul
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
  python -c "import sys; assert sys.version_info >= (3,11)" >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo  [!] Python 3.11+ not found. Install Python 3.12 from https://www.python.org/downloads/
  echo      and tick "Add python.exe to PATH". Then run setup.bat again.
  pause
  exit /b 1
)
echo  Using: %PY%

if not exist .venv (
  echo  Creating virtual environment...
  %PY% -m venv .venv || (echo  [!] venv failed & pause & exit /b 1)
)
echo  Installing packages (first run takes a minute)...
.venv\Scripts\python -m pip install --upgrade pip -q
.venv\Scripts\python -m pip install -r requirements.txt -q || (echo  [!] pip install failed & pause & exit /b 1)

echo  Creating shortcuts...
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\make_shortcuts.ps1"

echo.
where ollama >nul 2>nul
if errorlevel 1 (
  echo  [ ] Ollama not found - install from https://ollama.com/download, then run setup.bat again
) else (
  echo  [ok] Ollama found
  echo  Building your local "darkmoon" model ^(pulls qwen3.5:9b the first time, ~6.6 GB^)...
  ollama pull qwen3.5:9b
  ollama create darkmoon -f models\darkmoon.Modelfile && echo  [ok] darkmoon model ready - try: ollama run darkmoon
)
where grok >nul 2>nul && (echo  [ok] Grok CLI found - run "grok login" once if you haven't) || (echo  [ ] Grok CLI not found - optional, see README)
where claude >nul 2>nul && (echo  [ok] Claude Code found - run "claude" once to sign in if you haven't) || (echo  [ ] Claude Code not found - optional, see README)
where npx >nul 2>nul && (echo  [ok] Node.js found - local connectors can run) || (echo  [ ] Node.js not found - optional, needed for most connectors)
echo.
echo  Done! Start Darkmoon from the desktop shortcut or start.bat
echo.
pause
