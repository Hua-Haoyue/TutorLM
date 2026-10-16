param(
    [int]$Port = 8090,
    [string]$Python = "",
    [string]$DataDir = "",
    [string]$ConfigPath = ""
)

# Local development launcher for the control plane (fake adapter, no Docker).
#
# Configuration precedence is defaults < file < environment, so this script only
# supplies explicit *development defaults*. If a config file is in play it is
# left authoritative: the script does not set any CONTROL_PLANE_* value that
# would otherwise override the file.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python = Join-Path $root ".venv/Scripts/python.exe" }
if (-not (Test-Path -LiteralPath $Python)) { throw "Create .venv and install control-plane requirements first." }
$Python = (Resolve-Path -LiteralPath $Python).Path
if (-not $DataDir) { $DataDir = Join-Path $root "var/runtime-host" }
$DataDir = [IO.Path]::GetFullPath($DataDir)

# Resolve the effective config file the same way the worker does: an explicit
# -ConfigPath, otherwise CONTROL_PLANE_CONFIG from the environment.
$configFile = $ConfigPath
if (-not $configFile) { $configFile = [Environment]::GetEnvironmentVariable("CONTROL_PLANE_CONFIG", "Process") }
$hasConfigFile = -not [string]::IsNullOrWhiteSpace($configFile)

$names = @(
    "CONTROL_PLANE_CONFIG", "CONTROL_PLANE_ADAPTER", "CONTROL_PLANE_DB_PATH",
    "CONTROL_PLANE_SLOTS", "CONTROL_PLANE_ADMIN_TOKEN", "CONTROL_PLANE_TOKEN_PEPPER"
)
$saved = @{}
foreach ($name in $names) { $saved[$name] = [Environment]::GetEnvironmentVariable($name, "Process") }
try {
    if ($hasConfigFile) {
        # The file is authoritative. Only surface an explicit -ConfigPath; never
        # inject adapter/slots/credentials that would override it.
        if ($ConfigPath) { $env:CONTROL_PLANE_CONFIG = [IO.Path]::GetFullPath($ConfigPath) }
        Write-Host "Control plane from config file: $env:CONTROL_PLANE_CONFIG"
    } else {
        # Explicit development defaults (fake adapter, two slots). Existing
        # environment values are preserved so an operator can still override.
        if (-not $env:CONTROL_PLANE_ADAPTER) { $env:CONTROL_PLANE_ADAPTER = "fake" }
        if (-not $env:CONTROL_PLANE_DB_PATH) { $env:CONTROL_PLANE_DB_PATH = Join-Path $DataDir "control-plane.sqlite3" }
        if (-not $env:CONTROL_PLANE_SLOTS) { $env:CONTROL_PLANE_SLOTS = "2" }
        if (-not $env:CONTROL_PLANE_ADMIN_TOKEN) { $env:CONTROL_PLANE_ADMIN_TOKEN = "local-development-admin-only" }
        if (-not $env:CONTROL_PLANE_TOKEN_PEPPER) { $env:CONTROL_PLANE_TOKEN_PEPPER = "local-development-pepper-only" }
        Write-Host "Fake control plane (adapter=$env:CONTROL_PLANE_ADAPTER, slots=$env:CONTROL_PLANE_SLOTS)"
    }
    Write-Host "Docs: http://127.0.0.1:$Port/docs (no application containers)"
    Push-Location (Join-Path $root "runtime-host/control-plane")
    try {
        & $Python -m control_plane --host 127.0.0.1 --port $Port
        if ($LASTEXITCODE -ne 0) { throw "Control plane exited with code $LASTEXITCODE" }
    } finally { Pop-Location }
} finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $saved[$name], "Process") }
}
