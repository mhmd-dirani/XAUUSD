# Historical replay: old (v1.5) vs new (v1.6) rules on MT5 history. No Claude calls, no orders.
# Usage: .\scripts\run_replay.ps1            (120 days)
#        .\scripts\run_replay.ps1 -Days 60 -Ticks
#        .\scripts\run_replay.ps1 -Commission 7
param([int]$Days = 120, [switch]$Ticks, [double]$Commission = 0, [double]$Slippage = 0.10)
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$env:PYTHONUTF8 = "1"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }
$args_ = @("-u", "tools\replay.py", "--days", $Days, "--commission", $Commission, "--slippage", $Slippage)
if ($Ticks) { $args_ += "--ticks" }
& $Python @args_
exit $LASTEXITCODE
