@echo off
REM Runs with a console window so you can see errors. Logs are also in %APPDATA%\Darkmoon\localagent.log
cd /d "%~dp0"
.venv\Scripts\python.exe -m app.main --debug %*
pause
