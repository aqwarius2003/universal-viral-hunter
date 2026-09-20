[CmdletBinding()]
param(
    [string]$Instance = 'ai-viral-hunter',
    [string]$Zone = 'us-east1-b',
    [string]$RemoteDir = '~/ai-viral-hunter'
)

$ErrorActionPreference = 'Stop'
$Archive = Join-Path $env:TEMP 'ai-viral-hunter-deploy.tar.gz'
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'

function Invoke-GcloudSsh {
    param([string]$Command)

    & gcloud compute ssh $Instance --zone=$Zone --command=$Command
    if ($LASTEXITCODE -ne 0) {
        throw ('Remote command failed: ' + $Command)
    }
}

try {
    Write-Host '[1/5] Creating project archive...'
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
        .
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not create project archive.'
    }

    Write-Host '[2/5] Backing up persistent data on VM...'
    $BackupCommand = 'set -e; mkdir -p ' + $RemoteDir + '/data/backups; tar --exclude=backups -czf ' + $RemoteDir + '/data/backups/data-' + $Stamp + '.tar.gz -C ' + $RemoteDir + '/data .'
    Invoke-GcloudSsh $BackupCommand

    Write-Host '[3/5] Uploading archive to VM...'
    & gcloud compute scp $Archive ($Instance + ':~/ai-viral-hunter-deploy.tar.gz') --zone=$Zone
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not upload archive to VM.'
    }

    Write-Host '[4/5] Updating and rebuilding container...'
    $DeployCommand = 'set -e; mkdir -p ' + $RemoteDir + '/data; tar -xzf ~/ai-viral-hunter-deploy.tar.gz -C ' + $RemoteDir + '; rm ~/ai-viral-hunter-deploy.tar.gz; cd ' + $RemoteDir + '; sudo docker compose -f docker-compose.cloud.yml up -d --build --remove-orphans'
    Invoke-GcloudSsh $DeployCommand

    Write-Host '[5/5] Checking service status...'
    $StatusCommand = 'set -e; cd ' + $RemoteDir + '; sudo docker compose -f docker-compose.cloud.yml ps'
    Invoke-GcloudSsh $StatusCommand

    Write-Host 'Update completed. .env and data were not overwritten.'
}
finally {
    if (Test-Path $Archive) {
        Remove-Item $Archive -Force
    }
}
