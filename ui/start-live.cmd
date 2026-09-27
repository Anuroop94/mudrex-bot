@echo off
rem Live Mudrex dashboard: bot data server (read-only, 127.0.0.1:8765) + this UI (http://localhost:8080)
cd /d "%~dp0"
start "mudrex-data" /min python "%~dp0..\dashboard.py"
start "" http://localhost:8080
npx vite dev
