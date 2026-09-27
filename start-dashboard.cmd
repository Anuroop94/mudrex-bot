@echo off
rem Bot dashboard: one read-only server (http://127.0.0.1:8765) serving the UI in ui\ and its live data.
cd /d "%~dp0"
if not exist ui\dist\client\_shell.html (cd ui && call npx vite build && cd ..)
start "" http://127.0.0.1:8765
python dashboard.py
