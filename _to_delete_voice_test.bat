@echo off
cd /d "%~dp0"
echo Testing /api/voice with sample audio...
.venv\Scripts\python.exe _to_delete_voice_test.py
