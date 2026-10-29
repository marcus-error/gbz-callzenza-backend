# Clean Auto-Start Script for CallZenza FastAPI Backend
Write-Host "===================================================" -ForegroundColor Cyan
Write-Host "CallZenza Backend - Clean Auto-Start (Port 8001)" -ForegroundColor Cyan
Write-Host "===================================================" -ForegroundColor Cyan

Write-Host "[1/2] Checking ports 8000, 8001 (TCP) and 5062 (UDP) for stale/zombie processes..." -ForegroundColor Yellow
$portsToCheck = @(8000, 8001)
foreach ($port in $portsToCheck) {
    $staleConnections = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
    if ($staleConnections) {
        foreach ($conn in $staleConnections) {
            $pidToKill = $conn.OwningProcess
            if ($pidToKill -and $pidToKill -ne 0) {
                Write-Host "  -> Killing stale process holding port $port (PID: $pidToKill)" -ForegroundColor Magenta
                Stop-Process -Id $pidToKill -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

$udpEndpoints = Get-NetUDPEndpoint -LocalPort 5062 -ErrorAction SilentlyContinue
if ($udpEndpoints) {
    foreach ($ep in $udpEndpoints) {
        $pidToKill = $ep.OwningProcess
        if ($pidToKill -and $pidToKill -ne 0) {
            Write-Host "  -> Killing stale process holding UDP port 5062 (PID: $pidToKill)" -ForegroundColor Magenta
            Stop-Process -Id $pidToKill -Force -ErrorAction SilentlyContinue
        }
    }
}
Start-Sleep -Seconds 1
Write-Host "  -> Ports 8000, 8001 & 5062 are completely clear." -ForegroundColor Green

Write-Host "[2/2] Launching Uvicorn on http://0.0.0.0:8001 via run_server.py ..." -ForegroundColor Green
$pythonExe = if (Test-Path ".\venv\Scripts\python.exe") { ".\venv\Scripts\python.exe" } else { "python" }
& $pythonExe run_server.py

