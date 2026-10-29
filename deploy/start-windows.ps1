param(
    [string]$ListenHost = '127.0.0.1',
    [int]$Port = 8080,
    [string]$PublicOrigin = '',
    [int]$MaxJobs = 4
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    $serverArgs = @('-m', 'backend.server', '--host', $ListenHost, '--port', "$Port", '--max-jobs', "$MaxJobs", '--data-dir', (Join-Path $projectRoot 'data'))
    if ($PublicOrigin) { $serverArgs += @('--public-origin', $PublicOrigin) }
    & python @serverArgs
    if ($LASTEXITCODE -ne 0) { throw "Backend exited with code $LASTEXITCODE" }
} finally { Pop-Location }
