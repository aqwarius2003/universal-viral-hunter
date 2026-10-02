[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$ServerIP,

    [string]$User = 'root',
    [int]$Port = 22,
    [string]$Instance = 'viral-hunter',
    [string]$Zone = 'us-east1-b',
    [string]$RemoteDir = '~/viral-hunter'
)

$ErrorActionPreference = 'Stop'
$Archive = Join-Path $env:TEMP 'viral-hunter-deploy.tar.gz'
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'

# Detect if first argument or ServerIP is an IP or host name
$IsIpMode = $false
if ($ServerIP -and ($ServerIP -match '^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$' -or $ServerIP -match '^[a-zA-Z0-9.-]+$')) {
    $IsIpMode = $true
} elseif ($Instance -and $Instance -match '^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$') {
    $ServerIP = $Instance
    $IsIpMode = $true
}

function Invoke-RemoteCommand {
    param([string]$Command)

    if ($IsIpMode) {
        $Target = "$User@$ServerIP"
        ssh -p $Port $Target $Command
    } else {
        & gcloud compute ssh $Instance --zone=$Zone --command=$Command
    }

    if ($LASTEXITCODE -ne 0) {
        throw ('Remote command failed: ' + $Command)
    }
}

try {
    Write-Host '[1/5] Creating project archive...' -ForegroundColor Cyan
    if (Test-Path $Archive) {
        Remove-Item $Archive -Force
    }

    tar -czf $Archive `
        --exclude=.git `
        --exclude=.venv `
        --exclude=__pycache__ `
        --exclude=.pytest_cache `
        --exclude=data `
        --exclude=.agent `
        --exclude=.kilo `
        --exclude=.env `
        --exclude=*.sqlite `
        --exclude=*.sqlite3 `
        .

    if ($LASTEXITCODE -ne 0) {
        throw 'Could not create project archive.'
    }

    Write-Host '[2/5] Backing up persistent data on server...' -ForegroundColor Cyan
    $BackupCommand = 'set -e; mkdir -p ' + $RemoteDir + '/data/backups; if [ -d ' + $RemoteDir + '/data ] && [ $(ls -A ' + $RemoteDir + '/data 2>/dev/null) ]; then tar --exclude=backups -czf ' + $RemoteDir + '/data/backups/data-' + $Stamp + '.tar.gz -C ' + $RemoteDir + '/data .; fi'
    Invoke-RemoteCommand $BackupCommand

    Write-Host '[3/5] Uploading archive to server...' -ForegroundColor Cyan
    if ($IsIpMode) {
        $Target = "$User@$ServerIP"
        scp -P $Port $Archive "${Target}:~/viral-hunter-deploy.tar.gz"
    } else {
        & gcloud compute scp $Archive ($Instance + ':~/viral-hunter-deploy.tar.gz') --zone=$Zone
    }

    if ($LASTEXITCODE -ne 0) {
        throw 'Could not upload archive to server.'
    }

    Write-Host '[4/5] Updating and rebuilding Docker container...' -ForegroundColor Cyan
    $DeployCommand = 'set -e; mkdir -p ' + $RemoteDir + '/data; tar -xzf ~/viral-hunter-deploy.tar.gz -C ' + $RemoteDir + '; rm -f ~/viral-hunter-deploy.tar.gz; cd ' + $RemoteDir + '; touch .env; docker rm -f viral-hunter 2>/dev/null || true; if docker compose version >/dev/null 2>&1; then docker compose up -d --build --remove-orphans; else docker-compose up -d --build --remove-orphans; fi'
    Invoke-RemoteCommand $DeployCommand

    Write-Host '[5/5] Checking service status...' -ForegroundColor Cyan
    $StatusCommand = 'set -e; cd ' + $RemoteDir + '; if docker compose version >/dev/null 2>&1; then docker compose ps; else docker-compose ps; fi'
    Invoke-RemoteCommand $StatusCommand

    Write-Host "`n✅ Update completed successfully!" -ForegroundColor Green
    Write-Host 'Data in ./data and .env file were preserved on the server.' -ForegroundColor Yellow
}
finally {
    if (Test-Path $Archive) {
        Remove-Item $Archive -Force
    }
}
