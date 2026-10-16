param(
    [int]$Port = 8090,
    [string]$Python = "",
    [string]$DataDir = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python = Join-Path $root ".venv/Scripts/python.exe" }
if (-not (Test-Path -LiteralPath $Python)) { throw "Create .venv and install control-plane requirements first." }
$Python = (Resolve-Path -LiteralPath $Python).Path
if (-not $DataDir) { $DataDir = Join-Path $root "var/runtime-host" }
$DataDir = [IO.Path]::GetFullPath($DataDir)

$names = @(
    "CONTROL_PLANE_ADAPTER", "CONTROL_PLANE_DB_PATH", "CONTROL_PLANE_SLOTS",
    "CONTROL_PLANE_ADMIN_TOKEN", "CONTROL_PLANE_TOKEN_PEPPER"
)
$saved = @{}
foreach ($name in $names) { $saved[$name] = [Environment]::GetEnvironmentVariable($name, "Process") }
try {
    $env:CONTROL_PLANE_ADAPTER = "fake"
    $env:CONTROL_PLANE_DB_PATH = Join-Path $DataDir "control-plane.sqlite3"
    $env:CONTROL_PLANE_SLOTS = "2"
    if (-not $env:CONTROL_PLANE_ADMIN_TOKEN) { $env:CONTROL_PLANE_ADMIN_TOKEN = "local-development-admin-only" }
    if (-not $env:CONTROL_PLANE_TOKEN_PEPPER) { $env:CONTROL_PLANE_TOKEN_PEPPER = "local-development-pepper-only" }
    Write-Host "Fake control plane: http://127.0.0.1:$Port/docs (no application containers)"
    Push-Location (Join-Path $root "runtime-host/control-plane")
    try {
        & $Python -m control_plane --host 127.0.0.1 --port $Port
        if ($LASTEXITCODE -ne 0) { throw "Control plane exited with code $LASTEXITCODE" }
    } finally { Pop-Location }
} finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $saved[$name], "Process") }
}
