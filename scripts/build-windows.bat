@echo off
REM Build the Windows executable with PyInstaller.
REM
REM Run from anywhere; the script cd's to the project root itself.
REM Assumes `python` and PyInstaller are on PATH (pip install pyinstaller).
REM
REM Outputs dist\f1overlay.exe. Ship settings.json next to it; the overlay reads
REM it from the executable's directory when frozen.

setlocal
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.."

REM Wipe previous artifacts so a stale .spec or build\ tree can't poison the
REM next run (PyInstaller will silently reuse an existing .spec).
if exist build rd /s /q build
if exist dist rd /s /q dist
if exist f1overlay.spec del /q f1overlay.spec

echo === Building f1overlay.exe ===
python -m PyInstaller --onefile --noconsole --name f1overlay --icon assets\icon.ico main.py
if errorlevel 1 goto :fail

copy /y settings.json dist\settings.json >nul

echo.
echo === Done. dist\f1overlay.exe + dist\settings.json ===
popd
endlocal
exit /b 0

:fail
echo.
echo === Build failed ===
popd
endlocal
exit /b 1
