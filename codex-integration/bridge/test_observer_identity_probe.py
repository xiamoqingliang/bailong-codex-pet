"""Observer probe scheduling uses fake targets; never inspect Codex windows."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PREFIX = r'''
$ErrorActionPreference='Stop'
$source='__SOURCE__'
$stateDirectory='__ROOT__'
$PythonPath='fixture-python.exe'
function Write-ObserverError { param($Stage,$Failure) }
function Require([bool]$Value,[string]$Message) { if(!$Value){throw $Message} }
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Observer source failed PowerShell parsing'}
$names=@('Initialize-IdentityProbeState','Remove-IdentityProbeFiles','Close-IdentityProbe',
    'Get-IdentityProbeKey','Set-IdentityProbeFailure','Read-IdentityProbeReady',
    'Update-IdentityProbe','New-IdentityProbeProcess','Start-IdentityProbe',
    'Get-IdentityProbeCandidates','Get-IdentityProbeDiagnostic','Read-DesktopProjection')
foreach($name in $names){
    $function=$ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name},$true)
    if($null -eq $function){throw ('Missing function: '+$name)}
    . ([ScriptBlock]::Create($function.Extent.Text))
}
Initialize-IdentityProbeState
$knownOverlayHandles=@{}
$candidate=[pscustomobject]@{Pid=123;Handle=[IntPtr]456;Visible=$true}
$script:created=@()
function Fake-Process {
    $process=[pscustomobject]@{Id=1000+$script:created.Count;HasExited=$false;Killed=0;Disposed=0;StandardOutput=(New-Object IO.StringReader(''))}
    $process | Add-Member ScriptMethod Kill { $this.Killed++; $this.HasExited=$true }
    $process | Add-Member ScriptMethod Dispose { $this.Disposed++ }
    return $process
}
function New-IdentityProbeProcess($Info){
    $script:lastStartInfo=$Info
    $process=Fake-Process
    $script:created+=,$process
    return $process
}
function Ready-CurrentProbe {
    $probe=$script:identityProbe
    [IO.File]::WriteAllText($probe.ReadyPath,(@{token=$probe.Token;helperPid=$probe.Process.Id;pid=$probe.Pid;hwnd=$probe.Handle}|ConvertTo-Json -Compress))
}
function Finish-CurrentProbe([bool]$Verified,[string]$Token='',[int]$TargetPid=123,[long]$TargetHwnd=456){
    $probe=$script:identityProbe
    if(!$Token){$Token=$probe.Token}
    $text=@{token=$Token;pid=$TargetPid;hwnd=$TargetHwnd;verified=$Verified;reason='identity-not-found'}|ConvertTo-Json -Compress
    $probe.Process.StandardOutput=New-Object IO.StringReader($text)
    $probe.Process.HasExited=$true
}
'''


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell probe fixture')
class IdentityProbeTests(unittest.TestCase):
    def run_fixture(self, body, files=None, timeout=25):
        with tempfile.TemporaryDirectory(prefix='bailong-identity-fixture-') as name:
            root = Path(name)
            for filename, content in (files or {}).items():
                (root / filename).write_text(content, encoding='utf-8-sig')
            source = str(Path(__file__).with_name('codex_observer.ps1')).replace("'", "''")
            script = PREFIX.replace('__SOURCE__', source).replace('__ROOT__', str(root).replace("'", "''"))
            script += '\ntry {\n' + body + '\n} finally { Close-IdentityProbe "fixture-cleanup" }\n'
            powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
            completed = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', script],
                                       capture_output=True, timeout=timeout,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(completed.returncode, 0, completed.stderr.decode('utf-8', errors='replace'))
            return json.loads(completed.stdout)

    def test_slow_initialization_has_separate_query_budget_and_exact_success(self):
        result = self.run_fixture(r'''
$sentinel=Join-Path $stateDirectory 'unrelated.json'
[IO.File]::WriteAllText($sentinel,'keep')
Start-IdentityProbe $candidate
$child=$identityProbe.Process
$readyPath=$identityProbe.ReadyPath
$identityProbe.StartedAt=[DateTime]::UtcNow.AddSeconds(-4)
Update-IdentityProbe @($candidate)
Require ($null -ne $identityProbe -and $child.Killed -eq 0) 'Slow startup was killed after two seconds'
Require ((Get-IdentityProbeDiagnostic).phase -eq 'initializing') 'Initialization phase missing'
Ready-CurrentProbe
Update-IdentityProbe @($candidate)
Require ((Get-IdentityProbeDiagnostic).phase -eq 'querying-uia') 'Readiness did not start separate query budget'
Require ($identityDiagnostic.lastInitializationMs -ge 4000) 'Cold-start timing not retained'
Finish-CurrentProbe $true
Update-IdentityProbe @($candidate)
Require ($knownOverlayHandles['456'] -eq 123) 'Verified exact target was not cached'
Require ($null -eq $identityProbe -and $child.Killed -eq 0 -and $child.Disposed -eq 1) 'Successful process cleanup incorrect'
Require (![IO.File]::Exists($readyPath) -and [IO.File]::ReadAllText($sentinel) -eq 'keep') 'Cleanup escaped own ready file'
Require ($lastStartInfo.CreateNoWindow -and $lastStartInfo.Arguments.Contains('-PythonPath "fixture-python.exe"')) 'Hidden interpreter-explicit child contract lost'
Get-IdentityProbeDiagnostic | ConvertTo-Json -Compress
''')
        self.assertEqual(result['verified'], 1)
        self.assertEqual(result['failures'], 0)

    def test_initialization_timeout_and_exponential_backoff_are_bounded(self):
        result = self.run_fixture(r'''
for($attempt=1;$attempt -le 6;$attempt++){
    if($attempt -gt 1){$identityRetryAt['123:456']=[DateTime]::UtcNow.AddSeconds(-1)}
    Start-IdentityProbe $candidate
    $child=$identityProbe.Process
    $identityProbe.StartedAt=[DateTime]::UtcNow.AddSeconds(-21)
    Update-IdentityProbe @($candidate)
    Require ($null -eq $identityProbe -and $child.Killed -eq 1 -and $child.Disposed -eq 1) 'Initialization timeout did not reclaim own child'
    $delay=($identityRetryAt['123:456']-[DateTime]::UtcNow).TotalSeconds
    $expected=[Math]::Min(15,[Math]::Pow(2,$attempt-1))
    Require ($delay -gt $expected-.7 -and $delay -le $expected) 'Retry delay was not bounded exponential backoff'
    Start-IdentityProbe $candidate
    Require ($created.Count -eq $attempt) 'Backoff started another cold process immediately'
}
Get-IdentityProbeDiagnostic | ConvertTo-Json -Compress
''')
        self.assertEqual(result['initializationTimeouts'], 6)
        self.assertEqual(result['attempts'], 6)

    def test_blocked_uia_is_reclaimed_after_readiness_without_blocking_loop(self):
        result = self.run_fixture(r'''
Start-IdentityProbe $candidate
$child=$identityProbe.Process
Ready-CurrentProbe
Update-IdentityProbe @($candidate)
$identityProbe.StartedAt=[DateTime]::UtcNow.AddSeconds(-15)
$identityProbe.ReadyAt=[DateTime]::UtcNow.AddSeconds(-2)
Update-IdentityProbe @($candidate)
Require ($null -ne $identityProbe) 'Initialization duration was charged to UIA query'
$identityProbe.ReadyAt=[DateTime]::UtcNow.AddSeconds(-4)
Update-IdentityProbe @($candidate)
Require ($null -eq $identityProbe -and $child.Killed -eq 1) 'Blocked UIA was not reclaimed'
Require ($knownOverlayHandles.Count -eq 0) 'Timed-out window became trusted'
Get-IdentityProbeDiagnostic | ConvertTo-Json -Compress
''')
        self.assertEqual(result['queryTimeouts'], 1)
        self.assertEqual(result['initializationTimeouts'], 0)
        self.assertEqual(result['lastReason'], 'uia-timeout')

    def test_disappeared_target_and_reused_hwnd_cancel_only_owned_child(self):
        result = self.run_fixture(r'''
$unrelated=Fake-Process
foreach($remaining in @(@(),@([pscustomobject]@{Pid=999;Handle=[IntPtr]456;Visible=$true}))){
    Start-IdentityProbe $candidate
    $child=$identityProbe.Process
    Ready-CurrentProbe
    $readyPath=$identityProbe.ReadyPath
    Update-IdentityProbe $remaining
    Require ($null -eq $identityProbe -and $child.Killed -eq 1) 'Lost target retained probe'
    Require (![IO.File]::Exists($readyPath) -and $knownOverlayHandles.Count -eq 0) 'Lost target became trusted or leaked ready file'
}
Require ($unrelated.Killed -eq 0 -and $unrelated.Disposed -eq 0) 'Other process was touched'
Get-IdentityProbeDiagnostic | ConvertTo-Json -Compress
''')
        self.assertEqual(result['cancelled'], 2)

    def test_wrong_token_pid_or_window_never_establishes_identity(self):
        result = self.run_fixture(r'''
foreach($wrong in @('token','pid','hwnd','unrelated-window')){
    Initialize-IdentityProbeState
    Start-IdentityProbe $candidate
    switch($wrong){
        'token' {Finish-CurrentProbe $true 'wrong'}
        'pid' {Finish-CurrentProbe $true '' 999}
        'hwnd' {Finish-CurrentProbe $true '' 123 999}
        'unrelated-window' {Finish-CurrentProbe $false}
    }
    Update-IdentityProbe @($candidate)
    Require ($knownOverlayHandles.Count -eq 0 -and $identityDiagnostic.failures -eq 1) 'Different window or forged result was trusted'
}
@{passed=$true}|ConvertTo-Json -Compress
''')
        self.assertTrue(result['passed'])

    def test_mismatched_ready_file_cannot_reset_initialization_deadline(self):
        result = self.run_fixture(r'''
Start-IdentityProbe $candidate
[IO.File]::WriteAllText($identityProbe.ReadyPath,(@{token='wrong';helperPid=$identityProbe.Process.Id;pid=123;hwnd=456}|ConvertTo-Json -Compress))
$identityProbe.StartedAt=[DateTime]::UtcNow.AddSeconds(-21)
Update-IdentityProbe @($candidate)
Require ($null -eq $identityProbe -and $identityDiagnostic.initializationTimeouts -eq 1) 'Wrong ready token extended initialization budget'
Get-IdentityProbeDiagnostic | ConvertTo-Json -Compress
''')
        self.assertEqual(result['lastReason'], 'initialization-timeout')

    def test_visible_targets_are_prioritized_and_same_group_rotates(self):
        result = self.run_fixture(r'''
$second=[pscustomobject]@{Pid=123;Handle=[IntPtr]457;Visible=$true}
$hidden=[pscustomobject]@{Pid=123;Handle=[IntPtr]458;Visible=$false}
Start-IdentityProbe $candidate
Finish-CurrentProbe $false
Update-IdentityProbe @($candidate,$second,$hidden)
$ordered=@(Get-IdentityProbeCandidates @($hidden,$candidate,$second))
Require ($ordered[0].Handle.ToInt64() -eq 457 -and $ordered[2].Handle.ToInt64() -eq 458) 'Visible fresh target starved behind failed or hidden window'
foreach($item in $ordered){Start-IdentityProbe $item}
Require ($identityProbe.Handle -eq 457) 'Scheduler did not choose next visible target'
@{passed=$true}|ConvertTo-Json -Compress
''')
        self.assertTrue(result['passed'])

    def test_inline_selection_projection_accepts_bom(self):
        result = self.run_fixture(r'''
$PythonPath='__PYTHON__'
$desktopConfigPath=Join-Path $stateDirectory 'config.toml'
$withBom=New-Object Text.UTF8Encoding($true)
[IO.File]::WriteAllText($desktopConfigPath,"[desktop]`nselected-avatar-id='custom:bailong-full'`navatar-overlay-pet-visible=true`n",$withBom)
Read-DesktopProjection | ConvertTo-Json -Compress
'''.replace('__PYTHON__', sys.executable.replace("'", "''")))
        self.assertTrue(result['selectionKnown'])
        self.assertIsNone(result['readFailure'])

    def test_real_hidden_fixture_survives_more_than_two_seconds_of_cold_start(self):
        child = r'''
param([switch]$ProbeIdentity,[int]$ProbePid,[long]$ProbeHwnd,[string]$ProbeReadyPath,[string]$ProbeToken,[string]$PythonPath)
Start-Sleep -Milliseconds 2400
$utf8=New-Object Text.UTF8Encoding($false)
[IO.File]::WriteAllText($ProbeReadyPath,(@{token=$ProbeToken;helperPid=$PID;pid=$ProbePid;hwnd=$ProbeHwnd}|ConvertTo-Json -Compress),$utf8)
Start-Sleep -Milliseconds 100
@{token=$ProbeToken;pid=$ProbePid;hwnd=$ProbeHwnd;verified=$true;reason='verified'}|ConvertTo-Json -Compress
'''
        result = self.run_fixture(r'''
function New-IdentityProbeProcess($Info){
    $childPath=Join-Path $stateDirectory 'fixture-child.ps1'
    $Info.Arguments=$Info.Arguments -replace '-File "[^"]*"',('-File "'+$childPath+'"')
    return [Diagnostics.Process]::Start($Info)
}
Start-IdentityProbe $candidate
$deadline=[DateTime]::UtcNow.AddSeconds(12)
while($null -ne $identityProbe -and [DateTime]::UtcNow -lt $deadline){
    Update-IdentityProbe @($candidate)
    Start-Sleep -Milliseconds 25
}
Require ($knownOverlayHandles['456'] -eq 123 -and $identityDiagnostic.initializationTimeouts -eq 0) 'Real cold helper did not complete'
Get-IdentityProbeDiagnostic | ConvertTo-Json -Compress
''', files={'fixture-child.ps1': child})
        self.assertGreater(result['lastInitializationMs'], 2300)
        self.assertEqual(result['verified'], 1)


if __name__ == '__main__':
    unittest.main()
