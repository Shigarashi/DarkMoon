@echo off
REM Builds a standalone Darkmoon.exe folder in dist\Darkmoon (no Python needed on the target PC).
REM Then optionally compile installer.iss with Inno Setup to get Darkmoon-Setup.exe.
cd /d "%~dp0"
if not exist .venv ( echo Run setup.bat first. & pause & exit /b 1 )
.venv\Scripts\python -m pip install -q pyinstaller
.venv\Scripts\python -m PyInstaller --noconfirm --clean darkmoon.spec || (echo Build failed & pause & exit /b 1)
echo.
echo Built: dist\Darkmoon\Darkmoon.exe
where iscc >nul 2>nul && (iscc installer.iss && echo Installer: Output\Darkmoon-Setup.exe) || echo (Install Inno Setup and run "iscc installer.iss" for a setup.exe)
pause
