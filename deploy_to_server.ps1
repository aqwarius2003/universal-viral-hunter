[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$ServerIP,

    [string]$User = 'root',
    [int]$Port = 22,
    [string]$RemoteDir = '~/viral-hunter'
)

$ErrorActionPreference = 'Stop'
$Archive = Join-Path $env:TEMP 'viral-hunter-deploy.tar.gz'
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$Target = "$User@$ServerIP"

function Invoke-RemoteSsh {
    param([string]$Command)

    ssh -p $Port $Target $Command
    if ($LASTEXITCODE -ne 0) {
        throw ("Remote command failed on $Target`: " + $Command)
    }
}

try {
    Write-Host "[1/5] Creating project archive..." -ForegroundColor Cyan
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

    Write-Host "[2/5] Creating remote data backup on server..." -ForegroundColor Cyan
    $BackupCommand = "set -e; mkdir -p $RemoteDir/data/backups; if [ -d $RemoteDir/data ] && [ `$`(ls -A $RemoteDir/data 2>/dev/null) ]; then tar --exclude=backups -czf $RemoteDir/data/backups/data-$Stamp.tar.gz -C $RemoteDir/data .; fi"
    Invoke-RemoteSsh $BackupCommand

    Write-Host "[3/5] Uploading archive to server ($Target)..." -ForegroundColor Cyan
    scp -P $Port $Archive "${Target}:~/viral-hunter-deploy.tar.gz"
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not upload archive to server.'
    }

    Write-Host "[4/5] Extracting archive and rebuilding Docker container..." -ForegroundColor Cyan
    $DeployCommand = "set -e; mkdir -p $RemoteDir/data; tar -xzf ~/viral-hunter-deploy.tar.gz -C $RemoteDir; rm -f ~/viral-hunter-deploy.tar.gz; cd $RemoteDir; touch .env; docker ps -a --filter `"name=viral-hunter`" -q | xargs -r docker rm -f || true; if docker compose version >/dev/null 2>&1; then docker compose up -d --build --remove-orphans; else docker-compose up -d --build --remove-orphans; fi"
    Invoke-RemoteSsh $DeployCommand

    Write-Host "[5/5] Checking container status..." -ForegroundColor Cyan
    $StatusCommand = "set -e; cd $RemoteDir; if docker compose version >/dev/null 2>&1; then docker compose ps; else docker-compose ps; fi"
    Invoke-RemoteSsh $StatusCommand

    Write-Host "`n✅ Deployment successfully finished to $ServerIP!" -ForegroundColor Green
    Write-Host "Data in ./data and .env file were preserved on the server." -ForegroundColor Yellow
}
finally {
    if (Test-Path $Archive) {
        Remove-Item $Archive -Force
    }
}
