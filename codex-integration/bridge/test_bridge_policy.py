"""Selection and configuration regression tests; never operate on live Codex."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import bridge_policy as policy


class FullModeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name) / "bridge-config.json"
        self.config.write_text(json.dumps({"fullAvatarIds": [policy.FULL_LOCAL_ID]}), encoding="utf-8")
        self.config_patch = patch.object(policy, "CONFIG_FILE", self.config)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.state = {"running": True, "selectionKnown": True,
                      "selectedAvatarId": policy.FULL_LOCAL_ID, "nativePetVisible": True,
                      "overlayOpen": True, "requestedOpen": True,
                      "fullBailongMigratedIds": []}

    def test_native_toggle_is_authoritative(self):
        self.assertTrue(policy.full_mode_selected(self.state))
        self.assertTrue(policy.full_mode_visible(self.state))
        self.state["overlayOpen"] = False
        self.assertTrue(policy.full_mode_selected(self.state))
        self.assertFalse(policy.full_mode_visible(self.state))
        self.state.pop("overlayOpen")
        self.assertFalse(policy.full_mode_visible(self.state))

    def test_none_retains_id_but_disables_companion(self):
        self.state["nativePetVisible"] = False
        self.assertFalse(policy.full_mode_selected(self.state))
        self.assertFalse(policy.full_mode_visible(self.state))

    def test_other_native_pets_never_enable_companion(self):
        for value in ("codex", "custom:bailong", "custom:bailong-full-copy", "pet_other", None, ""):
            with self.subTest(value=value):
                self.state["selectedAvatarId"] = value
                self.assertFalse(policy.full_mode_visible(self.state))

    def test_exact_cloud_migration_is_accepted(self):
        self.state["selectedAvatarId"] = "pet_full123"
        self.state["fullBailongMigratedIds"] = ["pet_full123"]
        self.assertTrue(policy.full_mode_visible(self.state))

    def test_local_fallback_requires_exact_registration_and_accepts_projected_migration(self):
        self.config.write_text(json.dumps({'fullAvatarIds': [policy.FULL_LOCAL_ID, 'custom:bailong-full-local']}), encoding='utf-8')
        self.state['selectedAvatarId'] = 'custom:bailong-full-local'
        self.assertTrue(policy.full_mode_visible(self.state))
        self.state['selectedAvatarId'] = 'custom:bailong-full-local-2'
        self.assertFalse(policy.full_mode_visible(self.state))
        self.state['selectedAvatarId'] = 'pet_fallback_cloud'
        self.state['fullBailongMigratedIds'] = ['pet_fallback_cloud']
        self.assertTrue(policy.full_mode_visible(self.state))
        for selected in ("pet_other", "pet_full123-copy", "custom:bailong"):
            self.state["selectedAvatarId"] = selected
            self.assertFalse(policy.full_mode_visible(self.state))

    def test_bad_migration_projection_is_not_trusted(self):
        self.state["selectedAvatarId"] = "pet_full123"
        for value in ("pet_full123", {"pet_full123": True}, [True, 123, None]):
            self.state["fullBailongMigratedIds"] = value
            self.assertFalse(policy.full_mode_visible(self.state))

    def test_explicit_configured_cloud_id_is_accepted(self):
        self.config.write_text('{"fullAvatarIds":["pet_full123"]}', encoding="utf-8")
        self.state["selectedAvatarId"] = "pet_full123"
        self.assertTrue(policy.full_mode_visible(self.state))
        self.state["selectedAvatarId"] = "pet_other"
        self.state["fullBailongMigratedIds"] = ["pet_other"]
        self.assertFalse(policy.full_mode_visible(self.state), "migration requires configured dedicated slug")

    def test_running_and_selection_flags_must_be_booleans(self):
        for key in ("selectionKnown", "nativePetVisible", "running"):
            for value in (False, None, 1, "true"):
                with self.subTest(key=key, value=value):
                    state = dict(self.state, **{key: value})
                    self.assertFalse(policy.full_mode_visible(state))
        for value in (None, [], "open"):
            self.assertFalse(policy.full_mode_visible(value))

    def test_missing_or_malformed_config_fails_closed_even_for_migration(self):
        self.state["selectedAvatarId"] = "pet_full123"
        self.state["fullBailongMigratedIds"] = ["pet_full123"]
        self.assertTrue(policy.full_mode_visible(self.state))
        self.config.unlink()
        self.assertFalse(policy.full_mode_visible(self.state))
        for content in ('{', '[]', '{}', '{"fullAvatarIds":[]}',
                        '{"fullAvatarIds":"custom:bailong-full"}',
                        '{"fullAvatarIds":[true]}', '{"fullAvatarIds":["*"]}'):
            self.config.write_text(content, encoding="utf-8")
            self.assertFalse(policy.full_mode_visible(self.state), content)

    def test_config_changes_invalidate_cached_ids(self):
        self.assertTrue(policy.full_mode_visible(self.state))
        self.config.write_text('{"fullAvatarIds":["pet_changed"]}', encoding="utf-8")
        self.assertFalse(policy.full_mode_visible(self.state))
        self.state["selectedAvatarId"] = "pet_changed"
        self.assertTrue(policy.full_mode_visible(self.state))

    def test_unreadable_unchanged_file_does_not_keep_old_cache_forever(self):
        self.assertTrue(policy.full_mode_visible(self.state))
        expired = policy._cached_at + policy.CONFIG_CACHE_SECONDS + 0.1
        with patch.object(policy.time, "monotonic", return_value=expired), \
                patch.object(Path, "read_text", side_effect=PermissionError):
            self.assertFalse(policy.full_mode_visible(self.state))
        self.assertTrue(policy.full_mode_visible(self.state), "a repaired file is read again")


class DesktopProjectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.toml"

    def project(self, value):
        self.path.write_text(value, encoding="utf-8")
        return policy.read_desktop_selection(self.path)

    def test_missing_visibility_defaults_true_and_missing_id_is_none(self):
        self.assertEqual(self.project("[desktop]\n"),
                         {"selectionKnown": True, "selectedAvatarId": None, "nativePetVisible": True})
        self.assertEqual(self.project('[desktop]\nselected-avatar-id = "custom:bailong-full"\n'),
                         {"selectionKnown": True, "selectedAvatarId": policy.FULL_LOCAL_ID, "nativePetVisible": True})

    def test_only_desktop_keys_are_projected(self):
        result = self.project('selected-avatar-id = "wrong"\n[unrelated]\n'
                              'avatar-overlay-pet-visible = false\n'
                              '[desktop]\nselected-avatar-id = "custom:bailong-full" # comment\n'
                              'avatar-overlay-pet-visible = false\nprivate_value = "never-output"\n')
        self.assertEqual(result, {"selectionKnown": True, "selectedAvatarId": policy.FULL_LOCAL_ID,
                                  "nativePetVisible": False})
        self.assertNotIn("never-output", json.dumps(result))

    def test_quoted_table_and_literal_ids_work(self):
        self.assertTrue(self.project('["desktop"]\nselected-avatar-id = \'custom:bailong-full\'\n')["selectionKnown"])

    def test_utf8_bom_from_installer_preserves_selection_and_visibility(self):
        self.path.write_bytes(b'\xef\xbb\xbf[desktop]\r\nselected-avatar-id = "custom:bailong-full"\r\n'
                              b'avatar-overlay-pet-visible = true\r\n')
        self.assertEqual(policy.read_desktop_selection(self.path),
                         {"selectionKnown": True, "selectedAvatarId": policy.FULL_LOCAL_ID,
                          "nativePetVisible": True})
        self.path.write_bytes(self.path.read_bytes().replace(b'= true', b'= false'))
        self.assertFalse(policy.read_desktop_selection(self.path)["nativePetVisible"])

    def test_failed_parse_read_or_wrong_types_fail_closed(self):
        self.assertFalse(policy.read_desktop_selection(self.path)["selectionKnown"])
        for content in ('[desktop', '[desktop]\nselected-avatar-id = 123\n',
                        '[desktop]\navatar-overlay-pet-visible = "true"\n',
                        '[desktop]\nselected-avatar-id = ""\n',
                        '[desktop]\nselected-avatar-id="a"\nselected-avatar-id="b"\n'):
            with self.subTest(content=content):
                result = self.project(content)
                self.assertEqual(result, {"selectionKnown": False, "selectedAvatarId": None,
                                          "nativePetVisible": None})

    @unittest.skipUnless(os.name == "nt", "Windows observer projection")
    def test_observer_projects_only_full_slug_and_clears_failed_migration_read(self):
        fixture = Path(self.directory.name) / "global-state.json"
        fixture.write_text(json.dumps({
            "electron-avatar-overlay-open": True,
            "electron-persisted-atom-state": {"migrated-cloud-pet-ids-v1": {
                "fixture-account-a": {"custom:bailong-full": "pet_fullA", "custom:bailong": "pet_other",
                                      "custom:bailong-full-local": "pet_local", "custom:bailong-full-local-2": "pet_unconfigured"},
                "fixture-account-b": {"custom:bailong-full": "pet_fullB"},
                "fixture-account-c": {"custom:bailong-full": "codex"},
            }},
        }), encoding="utf-8")
        bridge_config = Path(self.directory.name) / 'bridge-fallback.json'
        bridge_config.write_text(json.dumps({'fullAvatarIds': [policy.FULL_LOCAL_ID, 'custom:bailong-full-local']}), encoding='utf-8')
        source = Path(policy.__file__).with_name("codex_observer.ps1")
        # Extract only the read-only projection function; do not start the
        # observer loop, scan windows or touch real Codex state in this test.
        script = """
$ErrorActionPreference = 'Stop'
function Write-ObserverError { param($Stage, $Failure) }
$tokens = $null; $parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('__SOURCE__', [ref]$tokens, [ref]$parseErrors)
foreach ($functionName in @('Test-TransientStateReadError','Test-RecentStateRead','Read-GlobalPetIntent')) {
    $function = $ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $functionName}, $true)
    . ([ScriptBlock]::Create($function.Extent.Text))
}
$globalStatePath = '__FIXTURE__'
$globalStateStamp = $null
$globalStateReadAt = [DateTime]::MinValue
$requestedOpen = $null
$fullBailongMigratedIds = @()
Read-GlobalPetIntent
$good = [ordered]@{requestedOpen=$requestedOpen; ids=@($fullBailongMigratedIds)}
$globalStateStamp = $null
Read-GlobalPetIntent -BridgeConfigPath '__BRIDGE_CONFIG__'
$fallback = [ordered]@{requestedOpen=$requestedOpen; ids=@($fullBailongMigratedIds)}
[IO.File]::WriteAllText($globalStatePath, '{broken')
$globalStateStamp = $null
Read-GlobalPetIntent
$bad = [ordered]@{requestedOpen=$requestedOpen; ids=@($fullBailongMigratedIds)}
ConvertTo-Json -Compress -Depth 4 @{good=$good; fallback=$fallback; bad=$bad}
""".replace("__SOURCE__", str(source).replace("'", "''")).replace("__FIXTURE__", str(fixture).replace("'", "''")).replace('__BRIDGE_CONFIG__', str(bridge_config).replace("'", "''"))
        result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script],
                                capture_output=True, text=True, check=True, timeout=15)
        value = json.loads(result.stdout)
        self.assertEqual(value["good"], {"requestedOpen": True, "ids": ["pet_fullA", "pet_fullB"]})
        self.assertEqual(value["bad"], {"requestedOpen": None, "ids": []})
        self.assertEqual(value['fallback'], {'requestedOpen': True, 'ids': ['pet_fullA', 'pet_fullB', 'pet_local']})
        self.assertNotIn("fixture-account", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
