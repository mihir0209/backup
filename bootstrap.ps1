# Receiver bootstrap (Windows): ensure Python>=3.10, 7z, rg, rclone via winget.
# Idempotent: checks first, installs only what is missing. Then prints next steps.
$missing = @()
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { $missing += "Python.Python.3.12" }
try { python -c 'import sys; exit(0 if sys.version_info >= (3, 10) else 1)' 2>$null; if ($LASTEXITCODE -ne 0) { $missing += "Python.Python.3.12" } } catch { $missing += "Python.Python.3.12" }
if (-not (Get-Command 7z -ErrorAction SilentlyContinue)) { $missing += "7zip.7zip" }
if (-not (Get-Command rg -ErrorAction SilentlyContinue)) { $missing += "BurntSushi.ripgrep.MSVC" }
if (-not (Get-Command rclone -ErrorAction SilentlyContinue)) { $missing += "Rclone.Rclone" }

if ($missing.Count -eq 0) {
  Write-Output "All dependencies present."
} else {
  Write-Output ("Missing, installing: " + ($missing -join ", "))
  foreach ($pkg in $missing) { winget install --accept-source-agreements --accept-package-agreements $pkg }
}

Write-Output "Next:"
Write-Output "  python backup.py --list-targets --no-elevate"
Write-Output "  python backup.py --dry-run --no-elevate"
Write-Output "  python backup.py --serve plan --no-elevate   # pick targets in the browser"
