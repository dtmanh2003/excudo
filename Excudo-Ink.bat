@echo off
cd /d "%~dp0"
uv run --locked --extra desktop --extra keichan --extra studio --extra browser python -m excudo.launcher --app ink %*
if errorlevel 1 pause
