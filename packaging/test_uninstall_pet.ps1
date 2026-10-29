[CmdletBinding()]
param([string]$Uninstaller)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
if(-not $Uninstaller) {$Uninstaller=Join-Path $PSScriptRoot 'uninstall_pet.ps1'}
$Uninstaller=[IO.Path]::GetFullPath($Uninstaller)
$tempBase=[IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
$fixture=Join-Path $tempBase ('BailongUninstallTest-'+[Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($fixture)|Out-Null
$utf8=New-Object Text.UTF8Encoding($false)
$passed=New-Object 'System.Collections.Generic.List[string]'
$processes=New-Object 'System.Collections.Generic.List[object]'
function Require([bool]$Value,[string]$Message) {if(-not $Value){throw $Message}}
function Put([string]$Path,[string]$Text) {
    [IO.Directory]::CreateDirectory((Split-Path -Parent $Path))|Out-Null
    [IO.File]::WriteAllText($Path,$Text,$utf8)
}
function Hash([string]$Path) {return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()}
function New-Case([string]$Name,[string]$Edition='native') {
    $base=Join-Path $fixture $Name
    $data=Join-Path $base '白龙 数据'
    $codexFixture=Join-Path $base '隔离 Codex'
    $startup=Join-Path $base '启动目录'
    $slug=if($Edition -eq 'full'){'bailong-full'}else{'bailong'}
    $entry=if($Edition -eq 'full'){Join-Path $data 'codex-integration\full-entry'}else{Join-Path $data 'bailong'}
    $pet=Join-Path $codexFixture ('pets\'+$slug)
    Put (Join-Path $entry 'pet.json') ('{"displayName":"fixture-'+$slug+'"}')
    Put (Join-Path $entry 'spritesheet.webp') 'fixture animation payload'
    [IO.Directory]::CreateDirectory($pet)|Out-Null
    foreach($name in @('pet.json','spritesheet.webp')){Copy-Item -LiteralPath (Join-Path $entry $name) -Destination (Join-Path $pet $name)}
    $config=Join-Path $codexFixture 'config.toml'
    $text="# keep model"+[char]13+[char]10+"model = 'fixture'"+[char]13+[char]10+"[desktop]"+[char]13+[char]10+
        "theme = 'dark'"+[char]13+[char]10+"selected-avatar-id = 'custom:"+$slug+"' # keep this comment"+[char]13+[char]10+
        "avatar-overlay-pet-visible = true"+[char]13+[char]10+"[projects.'D:\Example With Spaces\项目']"+[char]13+[char]10+"trust_level = 'trusted'"+[char]13+[char]10
    Put $config $text
    $receiptPath=Join-Path $codexFixture ('pet-install-receipts\'+$slug+'.json')
    $receipt=[ordered]@{schemaVersion=1;edition=$Edition;dataRoot=$data;avatarId=('custom:'+$slug);
        files=[ordered]@{'pet.json'=(Hash (Join-Path $pet 'pet.json'));'spritesheet.webp'=(Hash (Join-Path $pet 'spritesheet.webp'))};
        desktopBefore=[ordered]@{'selected-avatar-id'=@{present=$true;value='custom:fox'};'avatar-overlay-pet-visible'=@{present=$true;value=$false}};
        desktopInstalled=[ordered]@{'selected-avatar-id'=('custom:'+$slug);'avatar-overlay-pet-visible'=$true}}
    Put $receiptPath ($receipt|ConvertTo-Json -Depth 8)
    return [pscustomobject]@{Base=$base;Data=$data;Home=$codexFixture;Startup=$startup;Edition=$Edition;Pet=$pet;Config=$config;ReceiptPath=$receiptPath;Receipt=$receipt;Before=$text}
}
function Run-Uninstall($Case,[switch]$Check,[switch]$Failure) {
    $arguments=@('-NoProfile','-ExecutionPolicy','Bypass','-File',$Uninstaller,'-DataRoot',$Case.Data,'-Edition',$Case.Edition,'-CodexHome',$Case.Home,'-StartupDirectory',$Case.Startup)
    if($Check){$arguments+='-CheckOnly'}
    $previous=$ErrorActionPreference
    $ErrorActionPreference='Continue'
    try{$output=@(& powershell.exe @arguments 2>&1);$code=$LASTEXITCODE}finally{$ErrorActionPreference=$previous}
    if($Failure){Require ($code -ne 0) 'Expected uninstall to reject the fixture';return}
    if($code -ne 0){throw ('Uninstall failed in '+(Split-Path -Leaf $Case.Base)+': '+($output -join [Environment]::NewLine))}
    $json=@($output|Where-Object{$_ -is [string] -and $_.StartsWith('{')})
    Require ($json.Count -eq 1) ('Uninstall did not return one status: '+($output -join [Environment]::NewLine))
    return $json[0]|ConvertFrom-Json
}
try {
    $case=New-Case 'check'
    $before=Hash $case.Config
    $result=Run-Uninstall $case -Check
    Require ($result.Status -eq 'CheckOnly' -and (Hash $case.Config) -eq $before -and [IO.File]::Exists($case.ReceiptPath)) 'CheckOnly changed state'
    $passed.Add('check-only-no-writes')

    $case=New-Case 'native-normal'
    Put (Join-Path $case.Home 'conversations.json') 'keep personal fixture'
    $result=Run-Uninstall $case
    $actual=[IO.File]::ReadAllText($case.Config)
    Require ($result.Status -eq 'Uninstalled' -and $result.PetFilesRemoved -eq 2 -and $result.SettingsChanged) 'Native removal incomplete'
    Require ($actual.Contains('selected-avatar-id = "custom:fox" # keep this comment') -and $actual.Contains('avatar-overlay-pet-visible = false')) 'Original keys not restored'
    Require ($actual.Contains("[projects.'D:\Example With Spaces\项目']") -and [IO.File]::ReadAllText((Join-Path $case.Home 'conversations.json')) -eq 'keep personal fixture') 'Unrelated settings/history changed'
    Require (-not [IO.Directory]::Exists($case.Pet) -and -not [IO.File]::Exists($case.ReceiptPath)) 'Owned files left registered'
    $again=Run-Uninstall $case
    Require ($again.Status -eq 'AlreadyUninstalled') 'Repeat uninstall failed'
    $passed.Add('native-restore-and-repeat')

    $case=New-Case 'missing-before'
    $case.Receipt.desktopBefore['selected-avatar-id']=@{present=$false;value=$null}
    $case.Receipt.desktopBefore['avatar-overlay-pet-visible']=@{present=$false;value=$null}
    Put $case.ReceiptPath ($case.Receipt|ConvertTo-Json -Depth 8)
    $null=Run-Uninstall $case
    $actual=[IO.File]::ReadAllText($case.Config)
    Require (-not $actual.Contains('selected-avatar-id =') -and -not $actual.Contains('avatar-overlay-pet-visible =') -and $actual.Contains('# keep this comment')) 'Absent keys were not restored accurately'
    $passed.Add('absent-keys-restored')

    $case=New-Case 'other-selection'
    Put $case.Config $case.Before.Replace("'custom:bailong'","'custom:other'")
    $before=Hash $case.Config
    $result=Run-Uninstall $case
    Require (-not $result.SettingsChanged -and (Hash $case.Config) -eq $before -and $result.PetFilesRemoved -eq 2) 'Changed user selection overwritten'
    $passed.Add('later-user-choice-preserved')

    $case=New-Case 'changed-assets'
    Put (Join-Path $case.Pet 'spritesheet.webp') 'user modified image'
    $before=Hash $case.Config
    $result=Run-Uninstall $case
    Require ($result.Status -eq 'PreservedModifiedAssets' -and [IO.File]::Exists((Join-Path $case.Pet 'pet.json')) -and [IO.File]::ReadAllText((Join-Path $case.Pet 'spritesheet.webp')) -eq 'user modified image') 'Modified asset pair removed'
    Require ((Hash $case.Config) -eq $before -and [IO.File]::Exists($case.ReceiptPath)) 'Modified installation settings were removed'
    $passed.Add('modified-pair-preserved')

    $case=New-Case 'newer-owner'
    $case.Receipt.dataRoot=Join-Path $case.Base '另一份安装'
    Put $case.ReceiptPath ($case.Receipt|ConvertTo-Json -Depth 8)
    $before=Hash $case.Config
    $result=Run-Uninstall $case
    Require ($result.Status -eq 'PreservedNewerInstallation' -and (Hash $case.Config) -eq $before -and [IO.File]::Exists((Join-Path $case.Pet 'pet.json'))) 'Old package removed new installation'
    $passed.Add('newer-installation-preserved')

    $case=New-Case 'cloud-selection'
    Put $case.Config $case.Before.Replace("'custom:bailong'","'pet_fixture_native'")
    $globalPath=Join-Path $case.Home '.codex-global-state.json'
    Put $globalPath '{"electron-persisted-atom-state":{"migrated-cloud-pet-ids-v1":{"fixture-account":{"custom:bailong":"pet_fixture_native"}}},"unrelated":"keep"}'
    $globalBefore=Hash $globalPath
    $result=Run-Uninstall $case
    Require ($result.SettingsChanged -and (Hash $globalPath) -eq $globalBefore) 'Cloud selection mapping was not handled read-only'
    $passed.Add('late-cloud-migration-supported')

    $case=New-Case 'legacy-native'
    [IO.File]::Delete($case.ReceiptPath)
    $result=Run-Uninstall $case
    $actual=[IO.File]::ReadAllText($case.Config)
    Require ($result.PetFilesRemoved -eq 2 -and -not $actual.Contains('selected-avatar-id =') -and $actual.Contains('avatar-overlay-pet-visible = false')) 'Legacy cleanup guessed previous pet'
    $passed.Add('legacy-native-safe-default')

    $case=New-Case 'previous-selection-was-self'
    $case.Receipt.desktopBefore['selected-avatar-id']=@{present=$true;value='custom:bailong'}
    $case.Receipt.desktopBefore['avatar-overlay-pet-visible']=@{present=$true;value=$true}
    Put $case.ReceiptPath ($case.Receipt|ConvertTo-Json -Depth 8)
    $null=Run-Uninstall $case
    $actual=[IO.File]::ReadAllText($case.Config)
    Require (-not $actual.Contains('selected-avatar-id =') -and $actual.Contains('avatar-overlay-pet-visible = false')) 'Uninstall restored its own removed pet'
    $passed.Add('previous-self-selection-disabled')

    $case=New-Case 'legacy-full-no-local-files' 'full'
    [IO.File]::Delete($case.ReceiptPath)
    foreach($name in @('pet.json','spritesheet.webp')) {[IO.File]::Delete((Join-Path $case.Pet $name))}
    Put $case.Config $case.Before.Replace("'custom:bailong-full'","'pet_fixture_full'")
    Put (Join-Path $case.Home '.codex-global-state.json') '{"electron-persisted-atom-state":{"migrated-cloud-pet-ids-v1":{"fixture-account":{"custom:bailong-full":"pet_fixture_full"}}}}'
    $supervisor=Join-Path $case.Data 'codex-integration\bridge\bridge_supervisor.py'
    Put $supervisor 'pass'
    $pythonw=Join-Path $case.Data 'runtime\pythonw.exe'
    $command='"'+$pythonw+'" "'+$supervisor+'"'
    $startup=Join-Path $case.Startup 'BailongCodexBridge.vbs'
    [IO.Directory]::CreateDirectory($case.Startup)|Out-Null
    [IO.File]::WriteAllLines($startup,@('Option Explicit','Dim shell','Set shell = CreateObject("WScript.Shell")',('shell.CurrentDirectory = "'+$case.Data+'"'),('shell.Run "'+$command.Replace('"','""')+'", 0, False')),[Text.Encoding]::Unicode)
    $result=Run-Uninstall $case
    $actual=[IO.File]::ReadAllText($case.Config)
    Require ($result.StartupRemoved -and $result.PetFilesRemoved -eq 0 -and $result.SettingsChanged) 'Legacy full ownership lost when local files were absent'
    Require (-not $actual.Contains('selected-avatar-id =') -and $actual.Contains('avatar-overlay-pet-visible = false')) 'Legacy mapped selection remained dangling'
    $passed.Add('legacy-full-absent-assets-clears-own-selection')

    $case=New-Case 'invalid-receipt'
    $case.Receipt.schemaVersion=$true
    Put $case.ReceiptPath ($case.Receipt|ConvertTo-Json -Depth 8)
    $before=Hash $case.Config
    Run-Uninstall $case -Failure
    Require ((Hash $case.Config) -eq $before -and [IO.File]::Exists((Join-Path $case.Pet 'pet.json'))) 'Invalid receipt changed files'
    $passed.Add('malformed-receipt-rejected')

    $case=New-Case 'unsupported-config-still-stops' 'full'
    $bridge=Join-Path $case.Data 'codex-integration\bridge'
    Put (Join-Path $bridge 'bridge_supervisor.py') 'pass'
    Put $case.Config 'desktop = { selected-avatar-id = "custom:bailong-full", avatar-overlay-pet-visible = true }'
    $before=Hash $case.Config
    $result=Run-Uninstall $case
    Require ($result.Status -eq 'StoppedWithPreservedConfiguration' -and $result.ConfigCleanupDeferred -and $result.OwnProcessesReleased) 'Unsupported TOML prevented helper stopping'
    Require ((Hash $case.Config) -eq $before -and [IO.File]::Exists($case.ReceiptPath) -and [IO.File]::Exists((Join-Path $case.Pet 'pet.json'))) 'Unsupported TOML was partially unregistered'
    foreach($name in @('supervisor.stop','pet.stop','observer.stop')) {Require ([IO.File]::Exists((Join-Path $bridge $name))) 'Stop request missing for unsupported config'}
    $passed.Add('unsupported-config-preserves-registration-and-stops-helper')

    $case=New-Case 'full-graceful' 'full'
    $bridge=Join-Path $case.Data 'codex-integration\bridge'
    $python=(Get-Command python.exe -CommandType Application -ErrorAction Stop|Select-Object -First 1).Source
    $pythonw=Join-Path (Split-Path -Parent $python) 'pythonw.exe'
    if(-not [IO.File]::Exists($pythonw)){throw 'Pythonw is required for isolated full uninstall fixture'}
    $supervisor=Join-Path $bridge 'bridge_supervisor.py'
    Put $supervisor @'
from pathlib import Path
import time
base=Path(__file__).resolve().parent
while not (base/'supervisor.stop').exists():
    time.sleep(.02)
(base/'stopped.txt').write_text('stopped')
'@
    Put (Join-Path $bridge 'codex_observer.ps1') @'
while(-not [IO.File]::Exists((Join-Path $PSScriptRoot 'observer.stop'))){Start-Sleep -Milliseconds 20}
'@
    $startup=Join-Path $case.Startup 'BailongCodexBridge.vbs'
    $command='"'+$pythonw+'" "'+$supervisor+'"'
    $lines=@('Option Explicit','Dim shell','Set shell = CreateObject("WScript.Shell")',('shell.CurrentDirectory = "'+$case.Data+'"'),('shell.Run "'+$command.Replace('"','""')+'", 0, False'))
    [IO.Directory]::CreateDirectory($case.Startup)|Out-Null
    [IO.File]::WriteAllLines($startup,$lines,[Text.Encoding]::Unicode)
    $proc=Start-Process -FilePath $pythonw -ArgumentList ('"'+$supervisor+'"') -WorkingDirectory $case.Data -WindowStyle Hidden -PassThru
    $processes.Add($proc)
    Start-Sleep -Milliseconds 200
    $result=Run-Uninstall $case
    $proc.WaitForExit(2000)|Out-Null
    Require ($proc.HasExited -and [IO.File]::Exists((Join-Path $bridge 'stopped.txt')) -and -not [IO.File]::Exists($startup)) 'Full owned helper not stopped gracefully'
    Require ([IO.File]::Exists((Join-Path $case.Data 'uninstall.request')) -and $result.OwnProcessesReleased) 'Full cancellation marker missing'
    $resolved=[IO.Path]::GetFullPath($case.Data)
    Require ($resolved.StartsWith($fixture+'\',[StringComparison]::OrdinalIgnoreCase)) 'Rename fixture escaped scope'
    $moved=$resolved+'-renamed'
    Require ($moved.StartsWith($fixture+'\',[StringComparison]::OrdinalIgnoreCase)) 'Rename destination escaped scope'
    Move-Item -LiteralPath $resolved -Destination $moved
    Require ([IO.Directory]::Exists($moved)) 'Uninstalled package directory remains locked'
    $passed.Add('full-process-stop-and-folder-release')

    [pscustomobject]@{Passed=$true;Checks=$passed.Count;Cases=$passed.ToArray();RealUserProfileTouched=$false;RealCodexStopped=$false}|ConvertTo-Json -Depth 3
} finally {
    foreach($process in $processes){if(-not $process.HasExited){$process.Kill();$process.WaitForExit(2000)|Out-Null};$process.Dispose()}
    $resolved=[IO.Path]::GetFullPath($fixture)
    if($resolved.StartsWith($tempBase+'\',[StringComparison]::OrdinalIgnoreCase) -and (Split-Path -Leaf $resolved).StartsWith('BailongUninstallTest-') -and [IO.Directory]::Exists($resolved)){
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
