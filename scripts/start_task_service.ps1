$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$panelUrl = 'http://127.0.0.1:8765'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw '未找到项目 Python 环境，请先查看 README。' }
$alreadyRunning = $false
try {
    $health = Invoke-RestMethod -Uri "$panelUrl/api/health" -TimeoutSec 2
    $alreadyRunning = $health.product -eq 'mini-swe-task-panel'
} catch { }
if ($alreadyRunning) {
    Start-Process $panelUrl
} else {
    $logFolder = Join-Path $projectRoot 'temp\task_service_launcher'
    New-Item -ItemType Directory -Path $logFolder -Force | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss_ffff'
    Start-Process -FilePath $pythonPath -ArgumentList @('-B', "`"$PSScriptRoot\task_service.py`"", '--open') -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logFolder "$stamp.out.log") -RedirectStandardError (Join-Path $logFolder "$stamp.err.log")
}
