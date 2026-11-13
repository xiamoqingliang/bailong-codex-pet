"""Check hotkey selection guards using isolated profile files, without UI input."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == "nt", "Windows PowerShell selection projection")
class NativePetSelectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="bailong-native-selection-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = Path(__file__).with_name("native_mini_control.ps1")
        self.owner = "a" * 64
        self.other_owner = "b" * 64

    def select(self, selected, state, *, configured=None, visible="true"):
        if configured is None:
            configured = ["custom:bailong-full", "custom:bailong-full-local"]
        (self.root / "bridge-config.json").write_text(
            json.dumps({"fullAvatarIds": configured}), encoding="utf-8")
        (self.root / ".codex-global-state.json").write_text(json.dumps(state), encoding="utf-8")
        source = str(self.source).replace("'", "''")
        root = str(self.root).replace("'", "''")
        program = r"""
$ErrorActionPreference='Stop'
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile('__SOURCE__',[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Native controller has parser errors'}
$function=$ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Test-FullPetHotkeySelection'},$true)
if($null -eq $function){throw 'Hotkey selection function missing'}
$fixture='__ROOT__'
$functionPath=Join-Path $fixture 'selection-functions.ps1'
[IO.File]::WriteAllText($functionPath,$function.Extent.Text)
. $functionPath
$ConfigPath=Join-Path $fixture 'config.toml'
$ExpectedSelectedAvatarId='__SELECTED__'
$ExpectedNativePetVisible='__VISIBLE__'
ConvertTo-Json -Compress @{selected=(Test-FullPetHotkeySelection)}
""".replace("__SOURCE__", source).replace("__ROOT__", root).replace(
            "__SELECTED__", selected.replace("'", "''")).replace("__VISIBLE__", visible)
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", program],
            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        return json.loads(completed.stdout)["selected"]

    @staticmethod
    def atoms(mapping):
        return {"migrated-cloud-pet-ids-v1": {"fixture-account": mapping}}

    def test_configured_local_fallback_entries_are_enabled(self):
        for suffix in ("", "-local", "-local-2", "-local-16"):
            selected = "custom:bailong-full" + suffix
            with self.subTest(selected=selected):
                self.assertTrue(self.select(selected, {}, configured=["custom:bailong-full", selected]))
        for selected in ("custom:bailong", "custom:bailong-full-local-17", "custom:bailong-full-copy"):
            with self.subTest(selected=selected):
                self.assertFalse(self.select(selected, {}, configured=["custom:bailong-full", selected]))

    def test_legacy_mapping_accepts_only_configured_full_slugs(self):
        state = {"electron-persisted-atom-state": self.atoms({
            "custom:bailong-full-local": "pet_fallback",
            "custom:bailong-full-local-2": "pet_unconfigured",
            "custom:bailong": "pet_unrelated",
        })}
        self.assertTrue(self.select("pet_fallback", state))
        self.assertFalse(self.select("pet_unconfigured", state))
        self.assertFalse(self.select("pet_unrelated", state))
        self.assertFalse(self.select("pet_fallback", state, visible="false"))

    def test_active_owner_mapping_ignores_other_owner_and_legacy(self):
        state = {
            "electron-renderer-content-owner": self.owner,
            "electron-owner-atoms:" + self.owner: self.atoms({"custom:bailong-full-local": "pet_active"}),
            "electron-owner-atoms:" + self.other_owner: self.atoms({"custom:bailong-full-local": "pet_other"}),
            "electron-persisted-atom-state": self.atoms({"custom:bailong-full-local": "pet_legacy"}),
        }
        self.assertTrue(self.select("pet_active", state))
        self.assertFalse(self.select("pet_other", state))
        self.assertFalse(self.select("pet_legacy", state))
        self.assertFalse(self.select("pet_active", state, configured=["custom:bailong-full"]))

    def test_missing_or_malformed_owner_does_not_reuse_legacy(self):
        for owner in (None, 12, "bad", "A" * 64, self.owner):
            state = {
                "electron-renderer-content-owner": owner,
                "electron-persisted-atom-state": self.atoms({"custom:bailong-full-local": "pet_stale"}),
                "electron-owner-atoms:" + self.other_owner: self.atoms({"custom:bailong-full-local": "pet_stale"}),
            }
            with self.subTest(owner=owner):
                self.assertFalse(self.select("pet_stale", state))
        for atoms in (None, [], "bad", {"migrated-cloud-pet-ids-v1": []}):
            state = {
                "electron-renderer-content-owner": self.owner,
                "electron-owner-atoms:" + self.owner: atoms,
                "electron-persisted-atom-state": self.atoms({"custom:bailong-full-local": "pet_stale"}),
            }
            with self.subTest(atoms=atoms):
                self.assertFalse(self.select("pet_stale", state))

    def test_explicit_configured_cloud_id_remains_supported(self):
        self.assertTrue(self.select("pet_explicit", {}, configured=["pet_explicit"]))
        self.assertFalse(self.select("pet_other", {}, configured=["pet_explicit"]))


if __name__ == "__main__":
    unittest.main()
