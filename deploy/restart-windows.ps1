param(
    [string]$ListenHost = '127.0.0.1',
    [int]$Port = 8080,
    [string]$PythonPath = '',
    [string]$PublicOrigin = ''
)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$dataPath = Join-Path $projectRoot 'data'
Push-Location -LiteralPath $projectRoot
try {
    $owners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
    $oldProcesses = @()
    foreach ($processIdToReplace in $owners) {
        $record = Get-CimInstance Win32_Process -Filter "ProcessId = $processIdToReplace"
        if (-not $record -or $record.CommandLine -notmatch '-m\s+backend\.server\b' -or
            $record.CommandLine.IndexOf($dataPath, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
            throw "Port $Port belongs to an unrelated process; it was not stopped."
        }
        if (-not $PythonPath) { $PythonPath = $record.ExecutablePath }
        $oldProcesses += $record
    }
    if (-not $PythonPath) { $PythonPath = (Get-Command python -ErrorAction Stop).Source }
    $expectedVersion = (& $PythonPath -c 'import backend.credentials, backend.font_decoder; from backend.server import source_version; print(source_version())').Trim()
    if ($LASTEXITCODE -ne 0 -or $expectedVersion -notmatch '^[a-f0-9]{16}$') {
        throw 'Source validation failed; the running backend was not stopped.'
    }
    foreach ($record in $oldProcesses) {
        $currentRecord = Get-CimInstance Win32_Process -Filter "ProcessId = $($record.ProcessId)"
        if (-not $currentRecord -or $currentRecord.CreationDate -ne $record.CreationDate) {
            throw 'Process identity changed; restart aborted.'
        }
        Stop-Process -Id $record.ProcessId -Force
        Wait-Process -Id $record.ProcessId -Timeout 5 -ErrorAction SilentlyContinue
    }
    $logFolder = Join-Path $dataPath 'logs'
    New-Item -ItemType Directory -Path $logFolder -Force | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $arguments = @('-u', '-m', 'backend.server', '--host', $ListenHost, '--port', "$Port", '--data-dir', ('"' + $dataPath + '"'))
    if ($PublicOrigin) { $arguments += @('--public-origin', $PublicOrigin) }
    $started = Start-Process -FilePath $PythonPath -ArgumentList $arguments -WorkingDirectory $projectRoot `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logFolder "$stamp.stdout.log") `
        -RedirectStandardError (Join-Path $logFolder "$stamp.stderr.log")
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
            if ($health.status -eq 'ok' -and $health.version -eq $expectedVersion) {
                $result = @{ status='updated'; processId=$started.Id; port=$Port; version=$expectedVersion; restartClearsSessions=$false; restartStopsJobs=$true }
                $result | ConvertTo-Json -Compress
                return
            }
        } catch { }
        $started.Refresh()
        if ($started.HasExited) { throw "Updated backend exited; inspect $logFolder for startup diagnostics." }
        Start-Sleep -Milliseconds 150
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'Updated backend did not report the expected healthy version.'
} finally { Pop-Location }
