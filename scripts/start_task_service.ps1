$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw '未找到项目 Python 环境，请先查看 README。' }
& $pythonPath -B (Join-Path $PSScriptRoot 'launch_task_panel.pyw')
