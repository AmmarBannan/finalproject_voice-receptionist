@echo off
rem Starts the clinic API (:8101), the receptionist (:8000) and the UI (:7860) from the local .venv
cd /d "%~dp0"
echo Freeing ports 8000, 8101, 7860...
powershell -NoProfile -ExecutionPolicy Bypass -File free_ports.ps1 > ports_freed.log 2>&1
timeout /t 2 /nobreak >nul
start "clinic-api :8101" cmd /k ".venv\Scripts\python.exe -m uvicorn services.clinic_api:app --port 8101"
timeout /t 3 /nobreak >nul
start "app :8000" cmd /k ".venv\Scripts\python.exe -m uvicorn app.main:app --port 8000"
timeout /t 10 /nobreak >nul
start "ui :7860" cmd /k ".venv\Scripts\python.exe ui\app_ui.py"
timeout /t 8 /nobreak >nul
start "" http://localhost:7860
