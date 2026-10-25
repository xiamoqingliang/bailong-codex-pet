"""Real disposable helper timeout and identity binding; no real UI access."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == "nt", "Windows observer helpers")
class ObserverIdentityTests(unittest.TestCase):
    def test_blocked_uia_helper_cannot_block_host_lifecycle_sampling(self):
        source = str(Path(__file__).with_name("codex_observer.ps1")).replace("'", "''")
        code = r'''
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile('__SOURCE__',[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Observer parser errors'}
foreach($name in @('Initialize-IdentityProbeState','Remove-IdentityProbeFiles','Close-IdentityProbe',
  'Get-IdentityProbeKey','Set-IdentityProbeFailure','Read-IdentityProbeReady','Update-IdentityProbe')) {
  $node=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true)
  . ([ScriptBlock]::Create($node.Extent.Text))
}
Initialize-IdentityProbeState
$knownOverlayHandles=@{}
$info=New-Object Diagnostics.ProcessStartInfo
$info.FileName=Join-Path $PSHOME 'powershell.exe'
$info.Arguments='-NoProfile -Command Start-Sleep -Seconds 30'
$info.UseShellExecute=$false;$info.CreateNoWindow=$true
$info.WindowStyle=[Diagnostics.ProcessWindowStyle]::Hidden
$info.RedirectStandardOutput=$true;$info.RedirectStandardError=$true
$child=[Diagnostics.Process]::Start($info)
$childId=$child.Id
$identityProbe=@{Process=$child;Pid=101;Handle=202;HandleKey='202';Key='101:202';Token='fixture';
  ReadyPath='__READY__';ReadyAt=[DateTime]::UtcNow.AddSeconds(-4);StartedAt=[DateTime]::UtcNow.AddSeconds(-9)}
$candidate=[pscustomobject]@{Pid=101;Handle=[IntPtr]202}
$timer=[Diagnostics.Stopwatch]::StartNew()
try {
Update-IdentityProbe @($candidate)
$timer.Stop()
if($null -ne $identityProbe -or $timer.ElapsedMilliseconds -gt 1000){throw 'Probe blocked observer'}
if($knownOverlayHandles.Count -ne 0 -or !$identityRetryAt.ContainsKey('101:202')){throw 'Timed out result was trusted'}
if($identityDiagnostic.queryTimeouts -ne 1 -or $identityDiagnostic.initializationTimeouts -ne 0){throw 'Wrong timeout phase'}
@{passed=$true;elapsedMs=$timer.ElapsedMilliseconds;realUiAccessed=$false}|ConvertTo-Json -Compress
} finally { Close-IdentityProbe 'fixture-cleanup' }
'''.replace("__SOURCE__", source)
        with tempfile.TemporaryDirectory(prefix="bailong-observer-timeout-") as directory:
            code = code.replace("__READY__", str(Path(directory) / "own-ready.json").replace("'", "''"))
            result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", code],
                capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertTrue(json.loads(result.stdout)["passed"])


if __name__ == "__main__":
    unittest.main()
