"""Run only observer projection functions against isolated locked files."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(os.name == "nt", "Windows sharing-violation fixture")
class ObserverProjectionIOTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bailong-observer-projection-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = Path(__file__).with_name("codex_observer.ps1")

    def run_fixture(self, body):
        source = str(self.source).replace("'", "''")
        root = str(self.root).replace("'", "''")
        python = sys.executable.replace("'", "''")
        prefix = r"""
$ErrorActionPreference='Stop'
function Write-ObserverError { param($Stage,$Failure) }
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile('__SOURCE__',[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Observer source has parser errors'}
foreach($functionName in @('Test-TransientStateReadError','Test-RecentStateRead','Read-DesktopProjection','Read-DesktopSelection','Read-GlobalPetIntent')){
    $function=$ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $functionName},$true)
    if($null -eq $function){throw 'Projection function missing'}
    . ([ScriptBlock]::Create($function.Extent.Text))
}
$fixture='__ROOT__'
$PythonPath='__PYTHON__'
function Require([bool]$Value,[string]$Message){if(-not $Value){throw $Message}}
""".replace("__SOURCE__", source).replace("__ROOT__", root).replace("__PYTHON__", python)
        completed = subprocess.run(["powershell.exe", "-NoProfile", "-Command", prefix + body],
                                   capture_output=True, timeout=20,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        return json.loads(completed.stdout)

    def test_global_projection_unchanged_stamp_grace_expiry_and_new_mapping(self):
        result = self.run_fixture(r"""
$globalStatePath=Join-Path $fixture 'global.json'
$globalStateStamp=$null; $globalStateReadAt=[DateTime]::MinValue; $globalStateVerifiedAt=[DateTime]::MinValue
$requestedOpen=$null; $fullBailongMigratedIds=@()
[IO.File]::WriteAllText($globalStatePath,'{"electron-avatar-overlay-open":true,"electron-persisted-atom-state":{"migrated-cloud-pet-ids-v1":{"fixture":{"custom:bailong-full":"pet_first"}}}}')
Read-GlobalPetIntent
Require ($requestedOpen -eq $true -and $fullBailongMigratedIds -contains 'pet_first') 'Initial projection failed'
$globalStateReadAt=[DateTime]::UtcNow.AddSeconds(-4)
$globalStateVerifiedAt=[DateTime]::UtcNow.AddSeconds(-4)
$parseStamp=$globalStateReadAt
Read-GlobalPetIntent
Require ($globalStateReadAt -eq $parseStamp) 'Unchanged stamp unexpectedly reparsed'
Require (([DateTime]::UtcNow-$globalStateVerifiedAt).TotalSeconds -lt 1) 'Unchanged stamp did not refresh verification'
$verified=$globalStateVerifiedAt
$lock=[IO.File]::Open($globalStatePath,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
try {
    $globalStateStamp=$null
    Read-GlobalPetIntent
    Require ($requestedOpen -eq $true -and $fullBailongMigratedIds -contains 'pet_first') 'Brief I/O lost migration'
    Read-GlobalPetIntent
    Require ($globalStateVerifiedAt -eq $verified) 'Failure extended grace indefinitely'
    $globalStateVerifiedAt=[DateTime]::UtcNow.AddMilliseconds(-1100)
    Read-GlobalPetIntent
    Require ($null -eq $requestedOpen -and @($fullBailongMigratedIds).Count -eq 0) 'Expired I/O retained old migration'
} finally {$lock.Dispose()}
[IO.File]::WriteAllText($globalStatePath,'{"electron-avatar-overlay-open":false,"electron-persisted-atom-state":{"migrated-cloud-pet-ids-v1":{"fixture":{"custom:bailong-full":"pet_second"}}}}')
Read-GlobalPetIntent
Require ($requestedOpen -eq $false -and $fullBailongMigratedIds -contains 'pet_second' -and $fullBailongMigratedIds -notcontains 'pet_first') 'False/new mapping was not immediate'
[IO.File]::WriteAllText($globalStatePath,'{"electron-avatar-overlay-open":false,"electron-persisted-atom-state":{"migrated-cloud-pet-ids-v1":{}}}')
Read-GlobalPetIntent
Require ($requestedOpen -eq $false -and @($fullBailongMigratedIds).Count -eq 0) 'Successful empty mapping retained old IDs'
[IO.File]::WriteAllText($globalStatePath,'{broken')
Read-GlobalPetIntent
Require ($null -eq $requestedOpen -and @($fullBailongMigratedIds).Count -eq 0) 'Malformed JSON used grace'
ConvertTo-Json -Compress @{passed=$true;globalChecks=7;realProfileTouched=$false}
""")
        self.assertTrue(result["passed"])
        self.assertFalse(result["realProfileTouched"])

    def test_desktop_projection_distinguishes_io_from_invalid_and_applies_changes(self):
        result = self.run_fixture(r"""
$desktopConfigPath=Join-Path $fixture 'config.toml'
$desktopConfigStamp=$null; $desktopConfigReadAt=[DateTime]::MinValue; $desktopConfigVerifiedAt=[DateTime]::MinValue
$desktopSelection=@{selectionKnown=$false;selectedAvatarId=$null;nativePetVisible=$null}
[IO.File]::WriteAllText($desktopConfigPath,"[desktop]`nselected-avatar-id = 'custom:bailong-full'`navatar-overlay-pet-visible = true`n")
$null=Read-DesktopSelection
Require ($desktopSelection.selectionKnown -and $desktopSelection.selectedAvatarId -eq 'custom:bailong-full') 'Initial desktop projection failed'
$desktopConfigReadAt=[DateTime]::UtcNow.AddSeconds(-4)
$desktopConfigVerifiedAt=[DateTime]::UtcNow.AddSeconds(-4)
$parseStamp=$desktopConfigReadAt
$null=Read-DesktopSelection
Require ($desktopConfigReadAt -eq $parseStamp) 'Unchanged desktop stamp unexpectedly reparsed'
Require (([DateTime]::UtcNow-$desktopConfigVerifiedAt).TotalSeconds -lt 1) 'Desktop verification was not refreshed'
$verified=$desktopConfigVerifiedAt
$lock=[IO.File]::Open($desktopConfigPath,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
try {
    $desktopConfigStamp=$null
    $null=Read-DesktopSelection
    Require ($desktopSelection.selectionKnown -and $desktopSelection.selectedAvatarId -eq 'custom:bailong-full') 'Desktop sharing violation lost valid selection'
    Require ($desktopConfigVerifiedAt -eq $verified) 'Desktop failure renewed grace'
    $desktopConfigVerifiedAt=[DateTime]::UtcNow.AddMilliseconds(-1100)
    $null=Read-DesktopSelection
    Require (-not $desktopSelection.selectionKnown -and $null -eq $desktopSelection.selectedAvatarId) 'Expired desktop I/O retained selection'
} finally {$lock.Dispose()}
[IO.File]::WriteAllText($desktopConfigPath,"[desktop]`nselected-avatar-id = 'custom:other'`navatar-overlay-pet-visible = false`n")
$null=Read-DesktopSelection
Require ($desktopSelection.selectionKnown -and $desktopSelection.selectedAvatarId -eq 'custom:other' -and $desktopSelection.nativePetVisible -eq $false) 'Explicit desktop choice did not apply immediately'
[IO.File]::WriteAllText($desktopConfigPath,"[desktop]`nselected-avatar-id = 'unterminated")
$null=Read-DesktopSelection
Require (-not $desktopSelection.selectionKnown -and $null -eq $desktopSelection.selectedAvatarId) 'Invalid TOML reused cached selection'
ConvertTo-Json -Compress @{passed=$true;desktopChecks=6;realProfileTouched=$false}
""")
        self.assertTrue(result["passed"])
        self.assertFalse(result["realProfileTouched"])

    def test_no_valid_snapshot_cannot_be_created_from_missing_files(self):
        result = self.run_fixture(r"""
$globalStatePath=Join-Path $fixture 'absent-global.json'
$globalStateStamp=$null; $globalStateReadAt=[DateTime]::MinValue; $globalStateVerifiedAt=[DateTime]::MinValue
$requestedOpen=$null; $fullBailongMigratedIds=@()
Read-GlobalPetIntent
Require ($null -eq $requestedOpen -and @($fullBailongMigratedIds).Count -eq 0) 'Missing global state invented a projection'
$desktopConfigPath=Join-Path $fixture 'absent-config.toml'
$desktopConfigStamp=$null; $desktopConfigReadAt=[DateTime]::MinValue; $desktopConfigVerifiedAt=[DateTime]::MinValue
$desktopSelection=@{selectionKnown=$false;selectedAvatarId=$null;nativePetVisible=$null}
$null=Read-DesktopSelection
Require (-not $desktopSelection.selectionKnown) 'Missing desktop state invented a selection'
ConvertTo-Json -Compress @{passed=$true;realProfileTouched=$false}
""")
        self.assertTrue(result["passed"])


if __name__ == "__main__":
    unittest.main()
