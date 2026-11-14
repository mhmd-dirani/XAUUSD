# Gold AUTO-TRADER (demo). See docs\AUTO_TRADER.md.  Usage: .\scripts\trader_report.ps1 
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$env:PYTHONUTF8 = "1"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }
& $Python -u src\auto_trader.py --report
exit $LASTEXITCODE
