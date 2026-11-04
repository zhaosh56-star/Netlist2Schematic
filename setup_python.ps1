$ErrorActionPreference = 'Stop'
$saRoot = $PSScriptRoot
$saRuntime = Join-Path $saRoot '.python\distribution\tools\python.exe'
$saPython = Join-Path $saRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $saRuntime)) {
    $saDownloads = Join-Path $saRoot '.runtime-downloads'
    $saArchive = Join-Path $saDownloads 'python.3.12.10.zip'
    $saDistribution = Join-Path $saRoot '.python\distribution'
    New-Item -ItemType Directory -Path $saDownloads -Force | Out-Null
    Invoke-WebRequest -Uri 'https://api.nuget.org/v3-flatcontainer/python/3.12.10/python.3.12.10.nupkg' -OutFile $saArchive -UseBasicParsing
    Expand-Archive -LiteralPath $saArchive -DestinationPath $saDistribution -Force
}
$saSignature = Get-AuthenticodeSignature -LiteralPath $saRuntime
if ($saSignature.Status -ne 'Valid') {
    throw "Python executable signature is not valid: $($saSignature.Status)"
}
if (-not (Test-Path -LiteralPath $saPython)) {
    & $saRuntime -m venv (Join-Path $saRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python virtual environment' }
}
& $saPython -m pip install -r (Join-Path $saRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Could not install the pinned dependencies' }
& $saPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency verification failed' }
& $saPython --version
Write-Output "Ready: $saPython"
