@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File whoports.ps1 > ports.txt 2>&1
