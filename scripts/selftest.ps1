# Run all offline tests (no MT5 terminal, network or Telegram needed).
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$env:PYTHONUTF8 = "1"   # Arabic text on any Windows console
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }
& $Python -u src\gold_monitor.py --self-test
exit $LASTEXITCODE
