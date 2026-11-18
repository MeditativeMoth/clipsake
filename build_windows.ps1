# Build the standalone app and its Windows installer.
# Run from the project root (needs uv and Inno Setup 6):
#     powershell -ExecutionPolicy Bypass -File build_windows.ps1
# Output: dist\ClipSake\ (the app) and dist\installer\ClipSake-Setup-<version>.exe

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Never let packages from the user's personal folder (pip install --user) into
# the build; clipsake.spec also refuses to run outside the project venv.
$env:PYTHONNOUSERSITE = "1"

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { throw "uv not found. Install it with: winget install astral-sh.uv" }

# Install exactly what uv.lock specifies; fails if the lock is out of date.
& $uv sync --locked
if ($LASTEXITCODE -ne 0) { throw "uv sync failed; run 'uv lock' if pyproject.toml changed" }

# Refuse to ship known-vulnerable dependencies (see tools/audit.py).
& $uv run --locked python tools/audit.py
if ($LASTEXITCODE -ne 0) { throw "pip-audit found known vulnerabilities; update the affected packages first" }

$version = & $uv run --locked python -c "from importlib.metadata import version; print(version('clipsake'))"
if ($LASTEXITCODE -ne 0) { throw "Could not read the version" }
Write-Host "Building ClipSake $version"

& $uv run --locked pyinstaller --noconfirm clipsake.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$iscc = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 not found. Install it with: winget install JRSoftware.InnoSetup" }

& $iscc /Q "/DMyAppVersion=$version" installer\clipsake.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }

Write-Host "Installer: dist\installer\ClipSake-Setup-$version.exe"
