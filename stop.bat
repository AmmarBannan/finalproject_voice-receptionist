@echo off
rem Stops the clinic API (:8101), the receptionist (:8000) and the UI (:7860)
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File free_ports.ps1
echo Done. You can close the leftover terminal windows.
pause
