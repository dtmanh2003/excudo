@echo off
rem Creates the Excudo shortcuts with the Excudo logo on the Desktop. Run once after downloading.
rem (A .bat file cannot have its own icon; a shortcut can.)
chcp 65001 > nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_shortcuts.ps1"
echo.
pause
