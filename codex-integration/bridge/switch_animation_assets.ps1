[CmdletBinding()]
param([ValidateSet('Clean', 'Original')][string]$Mode = 'Clean')
$ErrorActionPreference = 'Stop'
$pythonDirectory = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313'
$python = Join-Path $pythonDirectory 'python.exe'
$pythonw = Join-Path $pythonDirectory 'pythonw.exe'
$selector = Join-Path $PSScriptRoot 'asset_source.py'
$supervisor = Join-Path $PSScriptRoot 'bridge_supervisor.py'
& $python $selector ('--' + $Mode.ToLowerInvariant())
if ($LASTEXITCODE -ne 0) { throw 'Asset validation failed; active configuration was not replaced.' }
$statusText = & $python $supervisor --status
$status = $statusText | ConvertFrom-Json
if ($status.supervisorAlive) {
    & $python $supervisor --stop
    $root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
    Start-Process -FilePath $pythonw -ArgumentList ('"' + $supervisor + '"') -WorkingDirectory $root -WindowStyle Hidden
    Write-Output 'The active bridge is restarting with the selected animation assets.'
} else {
    Write-Output 'Animation source saved. It will apply when the bridge next starts.'
}
