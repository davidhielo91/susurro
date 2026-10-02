@echo off
rem Ejecuta el instalador de PowerShell sin cambiar la politica de ejecucion del sistema.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0instalar.ps1"
echo.
pause
