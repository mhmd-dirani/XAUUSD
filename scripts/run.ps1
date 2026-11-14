# Start the monitor from the project root. Alerts only; no trade execution.
# Pick the analyst for this run:  .\scripts\run.ps1 -Analyst claude   (or astra)
# Without -Analyst: the GOLD_ANALYST environment variable, else analyst_provider in the config.
param([string]$Config = "config\gold_v5_config.json", [ValidateSet("", "claude", "astra")][string]$Analyst = "")
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$env:PYTHONUTF8 = "1"   # Arabic text on any Windows console
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }
if (-not (Test-Path $Config)) {
    Write-Error "Config $Config not found. Copy config\gold_v5_config.example.json to $Config and edit it."
    exit 1
}
if ($Analyst) { $env:GOLD_ANALYST = $Analyst }
if ($env:GOLD_ANALYST) { Write-Host "Analyst: $env:GOLD_ANALYST" } else { Write-Host "Analyst: from config" }
& $Python -u src\gold_monitor.py --config $Config
