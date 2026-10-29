"""
CallZenza Backend Server Launcher.
Runs Uvicorn with reload restricted to app/ and excludes runtime logs, DB files, and recordings.
Prevents PowerShell/CMD globbing issues with wildcard arguments.
"""
import sys
import uvicorn

if __name__ == "__main__":
    port = 8001
    host = "0.0.0.0"
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])
    
    print(f"[*] Starting CallZenza backend on http://{host}:{port} (watching app/)...")
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=True,
        reload_dirs=["app"],
        reload_excludes=["*.log", "*.db*", "*.wav", "*.mp3", "*static*", "*recordings*", "scratch/*", "*vector_stores*", "tests/*", "run_*.py"],
    )
