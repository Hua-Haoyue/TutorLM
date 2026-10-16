<#
.SYNOPSIS
    Local development launcher for the in-container instance application.

.DESCRIPTION
    Starts the real application backend (`python -m backend`, i.e.
    `backend.adapters.http.app:create_app` behind uvicorn) on loopback for local
    development. This is NOT the fake-adapter control plane: no Docker, no lease
    database, no outer source tree and no paid model call is involved.

    Safety defaults:
      * binds to 127.0.0.1 only (a non-loopback address requires -AllowNonLoopback);
      * forces the deterministic offline `fake` provider and clears any model
        endpoint/key inherited from the environment, so no remote/paid call can
        happen by accident;
      * never touches a real persistent workspace. Demo content and sessions are
        written under `var/instance-app/` (git-ignored). `-AllowImplicitSession`
        uses a throw-away scratch workspace there, not the outer project volume;
      * if the port is already in use it reports and exits -- it never kills the
        process that holds it;
      * every `TUTORLM_*`/`PYTHONPATH` value it sets is saved and restored on exit.

    The application does not serve `/docs` or the OpenAPI schema by design
    (`docs/instance-app-design.md` section 6); use `/health`, `/health/ready`,
    `/api/runtime` and the documented `/api/*` routes instead.

.PARAMETER Port
    TCP port to listen on. Default 8091 (the control-plane dev script uses 8090).

.PARAMETER ListenAddress
    Bind address. Default 127.0.0.1 (loopback only).

.PARAMETER Python
    Interpreter to use. Default: the repository `.venv` interpreter.

.PARAMETER DataDir
    Override the demo data root. Default `var/instance-app/data`.

.PARAMETER SessionDir
    Override the volume-external session directory. Default
    `var/instance-app/sessions/s01`. Must stay outside the data root.

.PARAMETER AllowImplicitSession
    Explicit opt-in to implicit local sessions (no invite code). This requires a
    bound workspace, so the script creates a scratch workspace under
    `var/instance-app/scratch/` and runs the app in `local` mode with
    `TUTORLM_ALLOW_IMPLICIT_SESSION=true`. Without this flag the app runs in
    `demo` mode and issues sessions only for the printed dev invite code.

.PARAMETER AllowNonLoopback
    Required to bind a non-loopback address. Only use on a trusted network.

.PARAMETER InviteCode
    Demo invite code handed to `POST /api/sessions`. Default `TLM-DEV-LOCAL`
    (a local placeholder, never a real credential).

.PARAMETER LogLevel
    uvicorn log level. Default `info`.

.PARAMETER Help
    Print this help and exit (also available as `-?`).

.EXAMPLE
    .\scripts\dev-instance-app.ps1

.EXAMPLE
    .\scripts\dev-instance-app.ps1 -Port 8092 -AllowImplicitSession
#>
[CmdletBinding()]
param(
    [int]$Port = 8091,
    [string]$ListenAddress = "127.0.0.1",
    [string]$Python = "",
    [string]$DataDir = "",
    [string]$SessionDir = "",
    [switch]$AllowImplicitSession,
    [switch]$AllowNonLoopback,
    [string]$InviteCode = "",
    [string]$LogLevel = "info",
    [switch]$Help
)

$ErrorActionPreference = "Stop"

if ($Help) {
    Get-Help $PSCommandPath -Detailed
    exit 0
}

$root = Split-Path -Parent $PSScriptRoot
$instanceApp = Join-Path $root "instance-app"

# ---- interpreter ---------------------------------------------------------
if (-not $Python) { $Python = Join-Path $root ".venv/Scripts/python.exe" }
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python interpreter not found at '$Python'. Create .venv and install instance-app/backend/requirements-dev.txt first (see instance-app/backend/README.md)."
}
$Python = (Resolve-Path -LiteralPath $Python).Path
if (-not (Test-Path -LiteralPath (Join-Path $instanceApp "backend/__main__.py"))) {
    throw "instance-app/backend is missing; run this script from the TutorLM repository."
}

# ---- safety: bind address ------------------------------------------------
$loopback = @("127.0.0.1", "::1", "localhost")
if (($loopback -notcontains $ListenAddress) -and (-not $AllowNonLoopback)) {
    throw "Refusing to bind '$ListenAddress': only loopback is allowed by default. Pass -AllowNonLoopback to override."
}

# ---- paths (never a real workspace volume) ------------------------------
$varRoot = Join-Path $root "var/instance-app"
if (-not $DataDir) {
    if ($AllowImplicitSession) { $DataDir = Join-Path $varRoot "scratch" } else { $DataDir = Join-Path $varRoot "data" }
}
if (-not $SessionDir) { $SessionDir = Join-Path $varRoot "sessions/s01" }
$DataDir = [IO.Path]::GetFullPath($DataDir)
$SessionDir = [IO.Path]::GetFullPath($SessionDir)

# Sessions must never live inside the data root (a copied volume would leak them).
$sessionInsideData = $SessionDir.TrimEnd('\', '/').StartsWith($DataDir.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar)
if ($sessionInsideData -or ($SessionDir -eq $DataDir)) {
    throw "TUTORLM_SESSION_DATA_DIR ('$SessionDir') must be outside TUTORLM_DATA_DIR ('$DataDir')."
}

# ---- safety: port availability (never kill the current holder) -----------
$probe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
try {
    $probe.Start()
    $portFree = $true
} catch {
    $portFree = $false
} finally {
    try { $probe.Stop() } catch { }
}
if (-not $portFree) {
    Write-Error "Port $Port is already in use. Stop that process yourself (this script never kills it) or pass -Port <other>."
    exit 1
}

# ---- environment (saved and restored) ------------------------------------
$names = @(
    "TUTORLM_RUN_MODE", "TUTORLM_DATA_DIR", "TUTORLM_SESSION_DATA_DIR", "TUTORLM_PROVIDER",
    "TUTORLM_MODEL_NAME", "TUTORLM_MODEL_API_BASE", "TUTORLM_MODEL_API_KEY",
    "TUTORLM_INVITE_CODE", "TUTORLM_ALLOW_IMPLICIT_SESSION", "TUTORLM_PERSISTENT_WORKSPACE", "TUTORLM_TEX_COMPILER_AVAILABLE",
    "TUTORLM_WORKSPACE_ID", "TUTORLM_OWNER_ID", "TUTORLM_MOUNT_PATH", "TUTORLM_WORKSPACE_SUBDIRECTORY",
    "TUTORLM_HOST", "TUTORLM_PORT", "TUTORLM_LOG_LEVEL", "PYTHONPATH"
)
$saved = @{}
foreach ($name in $names) { $saved[$name] = [Environment]::GetEnvironmentVariable($name, "Process") }

try {
    # Guarantee an offline, deterministic, loopback-only dev instance.
    $env:TUTORLM_PROVIDER = "fake"
    $env:TUTORLM_MODEL_NAME = "offline-demo"
    $env:TUTORLM_MODEL_API_BASE = $null
    $env:TUTORLM_MODEL_API_KEY = $null      # never echo or persist a key
    $env:TUTORLM_TEX_COMPILER_AVAILABLE = $null
    $env:TUTORLM_SESSION_DATA_DIR = $SessionDir
    $env:TUTORLM_LOG_LEVEL = $LogLevel
    $env:PYTHONPATH = $instanceApp        # so `backend.*` imports resolve

    if ($AllowImplicitSession) {
        # Explicit opt-in: a bound *scratch* workspace so implicit sessions are
        # permitted. It lives under var/ (git-ignored) and is not the real project.
        $workspaceId = "11111111-1111-4111-8111-111111111111"
        $ownerId = "22222222-2222-4222-8222-222222222222"
        $subdirectory = "users/$ownerId/workspaces/$workspaceId"
        $mount = Join-Path $varRoot "scratch"
        $env:TUTORLM_RUN_MODE = "local"
        $env:TUTORLM_PERSISTENT_WORKSPACE = "true"
        $env:TUTORLM_MOUNT_PATH = $mount
        $env:TUTORLM_WORKSPACE_ID = $workspaceId
        $env:TUTORLM_OWNER_ID = $ownerId
        $env:TUTORLM_WORKSPACE_SUBDIRECTORY = $subdirectory
        $env:TUTORLM_DATA_DIR = Join-Path $mount $subdirectory
        $env:TUTORLM_ALLOW_IMPLICIT_SESSION = "true"
        $env:TUTORLM_INVITE_CODE = $null
    } else {
        if (-not $InviteCode) { $InviteCode = "TLM-DEV-LOCAL" }
        $env:TUTORLM_RUN_MODE = "demo"
        $env:TUTORLM_PERSISTENT_WORKSPACE = $null
        $env:TUTORLM_WORKSPACE_ID = $null
        $env:TUTORLM_OWNER_ID = $null
        $env:TUTORLM_MOUNT_PATH = $null
        $env:TUTORLM_WORKSPACE_SUBDIRECTORY = $null
        $env:TUTORLM_DATA_DIR = $DataDir
        $env:TUTORLM_ALLOW_IMPLICIT_SESSION = $null
        $env:TUTORLM_INVITE_CODE = $InviteCode
    }

    Write-Host "TutorLM instance application (real backend, local development)"
    Write-Host "  mode            : $env:TUTORLM_RUN_MODE (provider=$env:TUTORLM_PROVIDER, offline)"
    Write-Host "  data root       : $env:TUTORLM_DATA_DIR"
    Write-Host "  session dir     : $env:TUTORLM_SESSION_DATA_DIR (outside the data root)"
    if ($AllowImplicitSession) {
        Write-Host "  sessions        : implicit allowed (scratch workspace, explicit opt-in)"
    } else {
        Write-Host "  sessions        : demo mode; invite code '$InviteCode' -> POST /api/sessions {""invite_code"":""$InviteCode""}"
    }
    Write-Host "  bind            : http://${ListenAddress}:$Port"
    Write-Host "  endpoints       : /health  /health/ready  /api/runtime  (no /docs: disabled by design)"
    Write-Host "  press Ctrl+C to stop (application environment is restored on exit)"
    Write-Host ""

    Push-Location $instanceApp
    try {
        & $Python -m backend --host $ListenAddress --port $Port --log-level $LogLevel
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($code -ne 0) { throw "backend exited with code $code" }
    exit 0
} finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $saved[$name], "Process") }
}
