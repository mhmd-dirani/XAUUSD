# Remove files the bot rebuilds by itself: Python caches and the Claude patch backups.
# Safe: it never touches runtime\ (memory, journal), config\, lessons\, .venv\ or APIs.txt.
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$caches = Get-ChildItem -Path src, tools, tests -Recurse -Directory -Filter __pycache__ -ErrorAction SilentlyContinue
$backups = Get-ChildItem -Path src, tools -Filter *.pre_claude.bak -ErrorAction SilentlyContinue
foreach ($item in @($caches) + @($backups)) {
    if ($item) { Write-Host "removing $($item.FullName.Substring($Root.Length + 1))"; Remove-Item $item.FullName -Recurse -Force }
}
Write-Host "Done. Run scripts\selftest.ps1 to confirm (must end with OK)."
