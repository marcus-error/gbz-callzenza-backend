@echo off
echo ===================================================
echo CallZenza Backend - Clean Auto-Start Script (Port 8001)
echo ===================================================

echo [1/2] Checking if ports 8000 or 8001 are occupied by stale processes...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8000 ^| findstr LISTENING') do (
    echo [!] Found process %%a on port 8000. Terminating to prevent bind conflict...
    taskkill /F /PID %%a >nul 2>&1
)
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8001 ^| findstr LISTENING') do (
    echo [!] Found process %%a on port 8001. Terminating to prevent bind conflict...
    taskkill /F /PID %%a >nul 2>&1
)

echo [2/2] Launching Uvicorn on http://127.0.0.1:8001 via run_server.py ...
python run_server.py
