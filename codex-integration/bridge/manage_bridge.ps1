[CmdletBinding()]
param([ValidateSet('Enable', 'Disable', 'Stop', 'Status')][string]$Action = 'Status')
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$pythonDirectory = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\runtime'))
$python = Join-Path $pythonDirectory 'python.exe'
$pythonw = Join-Path $pythonDirectory 'pythonw.exe'
$supervisor = Join-Path $PSScriptRoot 'bridge_supervisor.py'
$startupFile = Join-Path ([Environment]::GetFolderPath('Startup')) 'BailongCodexBridge.vbs'
if (!(Test-Path -LiteralPath $python) -or !(Test-Path -LiteralPath $pythonw)) {
    throw 'The original engine requires the installed 64-bit Python 3.13 runtime.'
}
switch ($Action) {
    'Enable' {
        $command = '"' + $pythonw + '" "' + $supervisor + '"'
        $lines = @(
            'Option Explicit',
            'Dim shell',
            'Set shell = CreateObject("WScript.Shell")',
            ('shell.CurrentDirectory = "' + $root.Replace('"', '""') + '"'),
            ('shell.Run "' + $command.Replace('"', '""') + '", 0, False')
        )
        [IO.File]::WriteAllLines($startupFile, $lines, [Text.Encoding]::Unicode)
        & $python (Join-Path $PSScriptRoot 'detached_launcher.py') --executable $pythonw --cwd $root -- $supervisor --show-on-start
        if ($LASTEXITCODE -ne 0) { throw 'Independent bridge startup failed. The login entry remains available.' }
        Write-Output 'Full Bailong bridge enabled. Choose the dedicated Bailong entry in Codex Pets.'
    }
    'Disable' {
        if (Test-Path -LiteralPath $startupFile) { Remove-Item -LiteralPath $startupFile }
        & $python $supervisor --stop
        Write-Output 'Bridge autostart disabled. Original files and native pet artwork are preserved.'
    }
    'Stop' { & $python $supervisor --stop }
    'Status' { & $python $supervisor --status }
}
