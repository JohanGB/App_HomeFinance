@echo off
REM Abre MyCents sin dejar una consola abierta.
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" "src\main.py"
