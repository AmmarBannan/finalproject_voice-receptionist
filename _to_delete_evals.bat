@echo off
cd /d "%~dp0"
echo Running all eval cases against the official clinic data (CLINIC_TODAY=2026-11-02)...
powershell -NoProfile -ExecutionPolicy Bypass -File free_ports.ps1 >nul 2>&1
set CLINIC_TODAY=2026-11-02
start "eval clinic-api :8101" /min cmd /c ".venv\Scripts\python.exe -m uvicorn services.clinic_api:app --port 8101"
timeout /t 4 /nobreak >nul
start "eval app :8000" /min cmd /c ".venv\Scripts\python.exe -m uvicorn app.main:app --port 8000"
timeout /t 20 /nobreak >nul
.venv\Scripts\python.exe evals\run_evals.py --cases evals\all_cases.jsonl --out _to_delete_scorecard_official.md > _to_delete_evals.log 2>&1
echo EVALS_DONE>> _to_delete_evals.log
powershell -NoProfile -ExecutionPolicy Bypass -File free_ports.ps1 >nul 2>&1
set CLINIC_TODAY=
call start.bat
