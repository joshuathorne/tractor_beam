@echo off
rem Start tractor_beam natively on Windows. Usage: run.cmd [--lan] [port]
setlocal
cd /d "%~dp0"

set "HOST=127.0.0.1"
set "PORT=8877"
set "A1=%~1"
set "A2=%~2"
if /i "%A1%"=="--lan" (
  set "HOST=0.0.0.0"
  set "PORTARG=%A2%"
) else (
  set "PORTARG=%A1%"
)
if not "%PORTARG%"=="" set "PORT=%PORTARG%"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" goto build
rem A venv copied or moved from elsewhere still has the old paths baked in.
"%PY%" -c "import uvicorn, yt_dlp" >nul 2>&1
if errorlevel 1 goto build
goto run

:build
echo Building .venv (this takes a minute)...
if exist ".venv" rmdir /s /q ".venv"
where py >nul 2>&1
if errorlevel 1 (python -m venv .venv) else (py -3 -m venv .venv)
"%PY%" -m pip install --quiet --upgrade pip
"%PY%" -m pip install --quiet -r requirements.txt

:run
where ffmpeg >nul 2>&1
if errorlevel 1 echo WARNING: ffmpeg is not on PATH - muxing and audio extraction will fail.
rem -m uvicorn, not the .exe shim, so a moved folder still launches.
"%PY%" -m uvicorn app:app --host %HOST% --port %PORT% --log-level warning
