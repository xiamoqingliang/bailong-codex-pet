"""Isolated installer tests: no actual Codex/profile/startup is changed."""
from __future__ import annotations

import json
import io
import hashlib
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import time
import tomllib
import unittest
import types
from unittest import mock

import compact_install as setup


class FakeRuntime:
    def __init__(self, mutex=False):
        self.mutex = mutex
        self.launches = []
        self.matches = set()

    def mutex_open(self):
        return self.mutex

    def process_matches(self, pid, executable):
        return (pid, str(executable)) in self.matches

    def launch(self, root, args):
        self.launches.append((root, args))


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bailong-compact-tests-")
        self.base = Path(self.temporary.name)
        self.root = self.base / "白龙 完整版" / "data"
        self.user = self.base / "isolated-user"
        self.startup = self.user / "Startup"
        self.home = self.user / ".codex"
        self.root.mkdir(parents=True)
        self.runtime = FakeRuntime()
        for relative in ("runtime/python.exe", "runtime/pythonw.exe", "BailongPet.exe",
                         "runtime/python313.dll", "runtime/DLLs/_sqlite3.pyd",
                         "_internal/Qt6Core.dll", "_internal/PySide6/QtCore.cp313-win_amd64.pyd",
                         "_internal/PIL/_imaging.cp313-win_amd64.pyd", "_internal/PIL/_webp.cp313-win_amd64.pyd",
                         "codex-integration/bridge/bridge_supervisor.py", "codex-integration/bridge/bridge_pet.py",
                         "codex-integration/bridge/bridge-config.json", "codex-integration/clean-assets/素材清单.json"):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
        supervisor = self.root / "codex-integration/bridge/bridge_supervisor.py"
        supervisor.write_text('MUTEX_NAME = "BailongCodexBridgeSupervisorV1"\nSTOP_NAMES = ("supervisor.stop",)\n', encoding="utf-8")
        (self.root / "uninstall.ps1").write_text("# fixture uninstaller", encoding="utf-8")
        for name in setup.REQUIRED_BRIDGE_FILES:
            path = self.root / "codex-integration/bridge" / name
            if not path.exists():
                path.write_text("# fixture dependency", encoding="utf-8")
        assets = self.root / "codex-integration/clean-assets"
        animation_records = [{"file": f"animation{i}.webp"} for i in range(25)]
        manifest_data = json.dumps({"animations": animation_records}).encode()
        (assets / "素材清单.json").write_bytes(manifest_data)
        (assets / "repair-validation.json").write_text(json.dumps({"animations": 25, "frames": 2529,
                                                                  "manifestSha256": hashlib.sha256(manifest_data).hexdigest()}))
        for record in animation_records:
            (assets / record["file"]).write_bytes(b"fixture")
        entry = self.root / "codex-integration/full-entry"
        entry.mkdir()
        (entry / "pet.json").write_text(json.dumps({"displayName": "白龙完整版", "spriteVersionNumber": 1,
                                                   "spritesheetPath": "spritesheet.webp"}, ensure_ascii=False), encoding="utf-8")
        (entry / "spritesheet.webp").write_bytes(b"RIFF" + struct.pack("<I", 4) + b"WEBP")

    def tearDown(self):
        self.temporary.cleanup()

    def install(self, **options):
        return setup.install(self.root, self.user, self.startup, runtime=self.runtime, **options)

    def config(self, raw):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.toml").write_bytes(raw)

    def test_check_creates_nothing_and_launches_nothing(self):
        result = self.install(check=True)
        self.assertTrue(result["checkOnly"])
        self.assertEqual(5, result["filesChanged"])
        self.assertFalse(self.user.exists())
        self.assertEqual([], self.runtime.launches)

    def test_new_install_preserves_unicode_paths_and_initializes_selection(self):
        result = self.install()
        value = tomllib.loads((self.home / "config.toml").read_text())
        self.assertEqual({"selected-avatar-id": setup.LOCAL_ID, "avatar-overlay-pet-visible": True}, value["desktop"])
        entry = self.root / "codex-integration/full-entry"
        for name in ("pet.json", "spritesheet.webp"):
            self.assertEqual((entry / name).read_bytes(), (self.home / "pets/bailong-full" / name).read_bytes())
        parsed = setup.parse_old_startup((self.startup / "BailongCodexBridge.vbs").read_bytes())
        self.assertEqual((self.root, self.root / "runtime/pythonw.exe"), parsed)
        self.assertEqual("started", result["activation"])
        self.assertEqual(1, len(self.runtime.launches))
        self.assertIn("--show-on-start", self.runtime.launches[0][1])
        self.assertNotIn("--show-on-start", (self.startup / "BailongCodexBridge.vbs").read_text(encoding="utf-16"))

    def test_activation_failure_reports_registration_was_written(self):
        with mock.patch.object(self.runtime, "launch", side_effect=setup.InstallError("fixture launch refused")):
            with self.assertRaisesRegex(setup.ActivationError, "fixture launch refused"):
                self.install()
        self.assertTrue((self.home / "pets/bailong-full/pet.json").is_file())
        self.assertTrue((self.home / "pet-install-receipts/bailong-full.json").is_file())
        self.assertTrue((self.startup / "BailongCodexBridge.vbs").is_file())

    def ready_status(self, **changes):
        value = {"supervisorPid": 123, "phase": "waiting-for-observer", "observerPid": 456,
                 "independentProcess": True}
        value.update(changes)
        status = self.root / "codex-integration/bridge/bridge-status.json"
        status.write_text(json.dumps(value), encoding="utf-8")
        self.runtime.matches.add((123, str(self.root / "runtime/pythonw.exe")))
        return status

    def wait_ready(self, **kwargs):
        return setup.wait_for_supervisor(self.root, {"pid": 123}, self.runtime,
                                         started_at=time.time(), timeout=0, **kwargs)

    def test_startup_confirmation_accepts_same_live_initialized_supervisor(self):
        self.ready_status()
        self.wait_ready()

    def test_startup_confirmation_rejects_no_status_or_stale_or_other_pid(self):
        self.runtime.matches.add((123, str(self.root / "runtime/pythonw.exe")))
        with self.assertRaisesRegex(setup.InstallError, "超时"):
            self.wait_ready()
        status = self.ready_status()
        os.utime(status, (0, 0))
        with self.assertRaisesRegex(setup.InstallError, "超时"):
            self.wait_ready()
        self.ready_status(supervisorPid=999)
        with self.assertRaisesRegex(setup.InstallError, "超时"):
            self.wait_ready()

    def test_startup_confirmation_rejects_dead_process_and_missing_observer(self):
        self.ready_status()
        self.runtime.matches.clear()
        with self.assertRaisesRegex(setup.InstallError, "立即退出"):
            self.wait_ready()
        for changes in ({"observerPid": None}, {"independentProcess": False}, {"phase": "starting"}):
            self.ready_status(**changes)
            with self.assertRaisesRegex(setup.InstallError, "超时"):
                self.wait_ready()

    def test_startup_confirmation_rejects_stopped_supervisor_and_uninstall(self):
        self.ready_status(phase="stopped")
        with self.assertRaisesRegex(setup.InstallError, "初始化期间退出"):
            self.wait_ready()
        (self.root / setup.UNINSTALL_MARKER).write_text("fixture")
        with self.assertRaisesRegex(setup.InstallError, "卸载请求"):
            self.wait_ready()

    def test_startup_diagnostic_requires_same_pid_and_sanitizes_error_type(self):
        self.ready_status()
        path = self.root / "codex-integration/bridge/launch-error.json"
        path.write_text(json.dumps({"pid": 999, "errorType": "OldError", "reason": "private data"}))
        self.wait_ready()
        path.write_text(json.dumps({"pid": 123, "errorType": "UnboundLocalError", "reason": "private data"}))
        with self.assertRaisesRegex(setup.InstallError, "UnboundLocalError") as error:
            self.wait_ready()
        self.assertNotIn("private data", str(error.exception))
        path.write_text(json.dumps({"pid": 123, "errorType": "private data", "reason": "private data"}))
        with self.assertRaisesRegex(setup.InstallError, "StartupError"):
            self.wait_ready()

    def test_real_runtime_waits_only_for_supervisor_launch(self):
        launcher = types.ModuleType("detached_launcher")
        launcher.launch_detached = mock.Mock(return_value={"pid": 123})
        runtime = setup.WindowsRuntime()
        with mock.patch.dict("sys.modules", {"detached_launcher": launcher}), \
                mock.patch.object(setup, "wait_for_supervisor") as wait:
            self.assertEqual({"pid": 123}, runtime.launch(self.root,
                [str(self.root / "codex-integration/bridge/bridge_supervisor.py")]))
            wait.assert_called_once()
            wait.reset_mock()
            runtime.launch(self.root, [str(self.root / "install.py"), "--finish"])
            wait.assert_not_called()

    def test_delayed_activation_propagates_startup_failure(self):
        self.startup.mkdir(parents=True)
        (self.startup / "BailongCodexBridge.vbs").write_bytes(setup.startup_bytes(self.root))
        with mock.patch.object(self.runtime, "launch", side_effect=setup.InstallError("observer failed")):
            with self.assertRaisesRegex(setup.InstallError, "observer failed"):
                setup.finish_when_idle(self.root, None, self.runtime, maximum_seconds=1,
                                       startup_dir=self.startup)

    def test_diagnostic_omits_arbitrary_exception_text_and_absolute_paths(self):
        try:
            raise RuntimeError("secret-token-and-chat-text")
        except RuntimeError as error:
            with mock.patch.object(setup.sys, "stderr", io.StringIO()):
                message = setup.report_failure(self.root, error)
        report = (self.root / "install-diagnostic.json").read_text(encoding="utf-8")
        self.assertIn("RuntimeError", message)
        self.assertNotIn("secret-token-and-chat-text", message + report)
        self.assertNotIn(str(self.root), report)
        self.assertNotIn(str(Path(__file__).parent), report)
        self.assertTrue((self.root / "install-status.txt").is_file())

    def test_launcher_failure_keeps_meaningful_reason(self):
        launcher_error = type("DetachedLaunchError", (RuntimeError,), {"__module__": "__main__"})
        error = launcher_error("independent verification failed (Windows error 5)")
        launcher = types.ModuleType("detached_launcher")
        launcher.DetachedLaunchError = launcher_error
        with mock.patch.dict("sys.modules", {"detached_launcher": launcher}):
            self.assertIn("Windows error 5", setup.describe_error(error))
            self.assertIn("后台启动", setup.describe_error(error))

    def test_concurrent_install_accepts_ready_winner_after_own_launch_exits(self):
        self.ready_status(supervisorPid=999)
        self.runtime.matches.clear()
        self.runtime.matches.add((999, str(self.root / "runtime/pythonw.exe")))
        self.wait_ready()

    def test_concurrent_winner_can_publish_status_after_loser_exit(self):
        self.runtime.mutex = True
        def publish(_delay):
            self.ready_status(supervisorPid=999)
            self.runtime.matches.clear()
            self.runtime.matches.add((999, str(self.root / "runtime/pythonw.exe")))
        with mock.patch.object(setup.time, "sleep", side_effect=publish):
            setup.wait_for_supervisor(self.root, {"pid": 123}, self.runtime,
                                      started_at=time.time(), timeout=1)

    @unittest.skipUnless(os.name == "nt", "Windows live process query")
    def test_process_query_rejects_exited_process_with_retained_handle(self):
        runtime = setup.WindowsRuntime()
        self.assertTrue(runtime.process_matches(os.getpid(), Path(setup.sys.executable)))
        with subprocess.Popen([setup.sys.executable, "-c", "pass"],
                              creationflags=subprocess.CREATE_NO_WINDOW) as child:
            child.wait(timeout=10)
            self.assertFalse(runtime.process_matches(child.pid, Path(setup.sys.executable)))

    def test_existing_stopped_process_is_not_reported_already_running(self):
        self.ready_status(phase="stopped")
        with self.assertRaisesRegex(setup.InstallError, "初始化期间退出"):
            setup.activate(self.root, None, self.runtime)

    def test_failure_reporting_cannot_hide_error_when_directory_not_writable(self):
        with mock.patch.object(setup, "atomic_write", side_effect=PermissionError("private-path")), \
                mock.patch.object(setup.sys, "stderr", io.StringIO()) as stderr:
            setup.report_failure(self.root, setup.InstallError("真实失败"))
        self.assertIn("真实失败", stderr.getvalue())
        self.assertIn("诊断文件也无法写入", stderr.getvalue())
        self.assertNotIn("private-path", stderr.getvalue())

    def test_check_failure_emits_diagnostic_but_creates_no_files(self):
        with mock.patch.object(setup.sys, "stderr", io.StringIO()):
            setup.report_failure(self.root, ValueError("secret"), persist=False)
        self.assertFalse((self.root / "install-diagnostic.json").exists())
        self.assertFalse((self.root / "install-status.txt").exists())

    def test_main_failure_does_not_print_install_success(self):
        with mock.patch.object(setup, "__file__", str(self.root / "install.py")), \
                mock.patch.object(setup.sys, "argv", ["install.py"]), \
                mock.patch.object(setup, "current_startup", return_value=self.startup), \
                mock.patch.object(setup, "install", side_effect=setup.ActivationError("后台确认失败")), \
                mock.patch.object(setup.sys, "stdout", io.StringIO()) as stdout, \
                mock.patch.object(setup.sys, "stderr", io.StringIO()) as stderr:
            self.assertEqual(1, setup.main())
        self.assertNotIn("安装完成", stdout.getvalue())
        self.assertIn("后台确认失败", stderr.getvalue())
        report = json.loads((self.root / "install-diagnostic.json").read_text(encoding="utf-8"))
        self.assertTrue(report["registrationCompleted"])

    def test_background_finish_failure_saves_reason_without_console(self):
        launcher = types.ModuleType("detached_launcher")
        launcher.process_requires_detach = lambda: False
        launcher.launch_detached = mock.Mock()
        guard = mock.Mock(acquired=True)
        with mock.patch.dict("sys.modules", {"detached_launcher": launcher}), \
                mock.patch.object(setup, "__file__", str(self.root / "install.py")), \
                mock.patch.object(setup.sys, "argv", ["install.py", "--finish"]), \
                mock.patch.object(setup, "ActivationGuard", return_value=guard), \
                mock.patch.object(setup, "finish_when_idle", side_effect=setup.InstallError("初始化失败")), \
                mock.patch.object(setup.sys, "stdout", None), mock.patch.object(setup.sys, "stderr", None):
            self.assertEqual(1, setup.main())
        guard.close.assert_called_once()
        report = json.loads((self.root / "install-diagnostic.json").read_text(encoding="utf-8"))
        self.assertEqual("deferred-activation", report["operation"])
        self.assertIn("初始化失败", (self.root / "install-status.txt").read_text(encoding="utf-8"))

    def test_unrelated_config_sections_comments_and_bom_remain(self):
        source = (b"\xef\xbb\xbf# preserve\r\nmodel = 'fixture'\r\n[desktop]\r\n"
                  b"theme = 'dark'\r\nselected-avatar-id = 'old'\r\navatar-overlay-pet-visible = false\r\n"
                  b"[projects.'D:/sample']\r\ntrust_level = 'trusted'\r\n")
        self.config(source)
        self.install()
        actual = (self.home / "config.toml").read_bytes()
        self.assertTrue(actual.startswith(b"\xef\xbb\xbf# preserve\r\n"))
        self.assertIn(b"theme = 'dark'\r\n", actual)
        self.assertIn(b"[projects.'D:/sample']\r\ntrust_level = 'trusted'", actual)
        backups = list((self.home / "pet-install-backups").rglob("*-config.toml"))
        self.assertEqual([source], [path.read_bytes() for path in backups])

    def test_quoted_desktop_and_quoted_keys(self):
        source = b'["desktop"]\n"selected-avatar-id" = "previous"\n\'avatar-overlay-pet-visible\' = false\n'
        value = tomllib.loads(setup.update_config(source, setup.LOCAL_ID).decode())
        self.assertEqual(setup.LOCAL_ID, value["desktop"]["selected-avatar-id"])
        self.assertTrue(value["desktop"]["avatar-overlay-pet-visible"])

    def test_inline_or_dotted_desktop_fails_without_writes(self):
        for source in (b'desktop = { theme = "light" }\n', b'desktop.theme = "light"\n'):
            self.config(source)
            with self.assertRaises(setup.InstallError):
                self.install()
            self.assertEqual(source, (self.home / "config.toml").read_bytes())
            self.assertFalse((self.home / "pets").exists())
            self.assertFalse(self.startup.exists())

    def test_duplicate_install_is_file_idempotent(self):
        self.install()
        paths = list(self.user.rglob("*"))
        stamps = {p: p.stat().st_mtime_ns for p in paths if p.is_file()}
        second = self.install()
        self.assertEqual(0, second["filesChanged"])
        self.assertEqual(stamps, {p: p.stat().st_mtime_ns for p in stamps})

    def test_receipt_retains_initial_settings_across_repeat_and_directory_migration(self):
        self.config(b'[desktop]\nselected-avatar-id = "custom:previous"\navatar-overlay-pet-visible = false\n')
        self.install()
        path = self.home / "pet-install-receipts/bailong-full.json"
        receipt = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["dataRoot"], str(self.root))
        self.assertEqual(receipt["desktopBefore"], {
            "selected-avatar-id": {"present": True, "value": "custom:previous"},
            "avatar-overlay-pet-visible": {"present": True, "value": False}})
        self.assertEqual(receipt["files"]["pet.json"], setup.sha(self.home / "pets/bailong-full/pet.json"))
        original = path.read_bytes()
        self.install()
        self.assertEqual(path.read_bytes(), original)
        old_receipt = dict(receipt, dataRoot=str(self.base / "old package/data"))
        path.write_text(json.dumps(old_receipt), encoding="utf-8")
        self.install()
        updated = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(updated["dataRoot"], str(self.root))
        self.assertEqual(updated["desktopBefore"], receipt["desktopBefore"])

    def test_receipt_failure_rolls_back_new_registration_and_startup(self):
        self.config(b'model = "keep"\n')
        original = setup.atomic_write
        def fail_receipt(path, content):
            if path.parent.name == "pet-install-receipts":
                raise OSError("fixture receipt failure")
            return original(path, content)
        with mock.patch.object(setup, "atomic_write", side_effect=fail_receipt):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual((self.home / "config.toml").read_bytes(), b'model = "keep"\n')
        self.assertFalse((self.startup / "BailongCodexBridge.vbs").exists())
        self.assertFalse((self.home / "pets/bailong-full/pet.json").exists())
        self.assertEqual(self.runtime.launches, [])

    def test_malformed_receipt_is_rejected_before_install_changes(self):
        receipt = self.home / "pet-install-receipts/bailong-full.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text('{"schemaVersion": 999}', encoding="utf-8")
        with self.assertRaises(setup.InstallError):
            self.install()
        self.assertFalse((self.home / "config.toml").exists())
        self.assertFalse(self.startup.exists())

    def test_uninstall_marker_cancels_activation_and_waiter_even_with_matching_startup(self):
        self.startup.mkdir(parents=True)
        (self.startup / "BailongCodexBridge.vbs").write_bytes(setup.startup_bytes(self.root))
        (self.root / setup.UNINSTALL_MARKER).write_bytes(b"uninstall\n")
        self.assertEqual(setup.activate(self.root, None, self.runtime), "cancelled-by-uninstall")
        self.assertTrue(setup.finish_when_idle(self.root, None, self.runtime,
                                              maximum_seconds=1, startup_dir=self.startup))
        self.assertEqual(self.runtime.launches, [])

    def test_explicit_reinstall_clears_uninstall_marker_but_check_does_not(self):
        marker = self.root / setup.UNINSTALL_MARKER
        marker.write_bytes(b"uninstall\n")
        self.install(check=True)
        self.assertTrue(marker.exists())
        self.assertEqual(self.install()["activation"], "started")
        self.assertFalse(marker.exists())

    def test_same_running_install_does_not_launch_another_helper(self):
        status = self.root / "codex-integration/bridge/bridge-status.json"
        self.ready_status(phase="visible")
        self.runtime.matches.add((123, str(self.root / "runtime/pythonw.exe")))
        self.runtime.mutex = True
        result = self.install()
        self.assertEqual("already-running", result["activation"])
        self.assertEqual([], self.runtime.launches)

    def test_migration_only_reuses_already_selected_full_pet(self):
        state = {"electron-persisted-atom-state": {"migrated-cloud-pet-ids-v1": {
            "account-one": {setup.LOCAL_ID: "pet_one", "custom:other": "pet_other"},
            "account-two": {setup.LOCAL_ID: "pet_two"}}}}
        raw = json.dumps(state).encode()
        self.assertEqual("pet_one", setup.choose_avatar({"desktop": {"selected-avatar-id": "pet_one"}}, raw))
        self.assertEqual(setup.LOCAL_ID, setup.choose_avatar({"desktop": {"selected-avatar-id": "pet_other"}}, raw))
        self.assertEqual(setup.LOCAL_ID, setup.choose_avatar({}, raw))
        self.assertEqual(setup.LOCAL_ID, setup.choose_avatar({"desktop": {"selected-avatar-id": "pet_unknown"}}, raw))

    def test_malformed_map_does_not_break_install(self):
        self.config(b'[desktop]\nselected-avatar-id = "pet_unknown"\n')
        (self.home / ".codex-global-state.json").write_bytes(b"not json")
        self.install()
        self.assertEqual(b"not json", (self.home / ".codex-global-state.json").read_bytes())
        self.assertEqual(setup.LOCAL_ID, tomllib.loads((self.home / "config.toml").read_text())["desktop"]["selected-avatar-id"])

    def test_unsafe_manifest_path_is_rejected(self):
        path = self.root / "codex-integration/full-entry/pet.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["spritesheetPath"] = "../../outside.webp"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(setup.InstallError):
            self.install()
        self.assertFalse(self.user.exists())

    def test_rollback_restores_previous_files_when_write_fails(self):
        self.config(b'model = "fixture"\n')
        original = setup.atomic_write
        def fail_startup(path, content):
            if path.name == "BailongCodexBridge.vbs":
                raise OSError("fixture failure")
            return original(path, content)
        with mock.patch.object(setup, "atomic_write", side_effect=fail_startup):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual(b'model = "fixture"\n', (self.home / "config.toml").read_bytes())
        self.assertFalse((self.home / "pets/bailong-full/pet.json").exists())
        self.assertEqual([], self.runtime.launches)

    def test_unknown_old_startup_never_executes_it(self):
        self.startup.mkdir(parents=True)
        source = b'Set shell = CreateObject("WScript.Shell")\nshell.Run "unexpected.exe"\n'
        (self.startup / "BailongCodexBridge.vbs").write_bytes(source)
        self.runtime.mutex = True
        result = self.install()
        self.assertEqual("waiting-for-existing-pet", result["activation"])
        args = self.runtime.launches[0][1]
        self.assertEqual([str(self.root / "install.py"), "--finish"], args)
        backups = list((self.home / "pet-install-backups").rglob("*-BailongCodexBridge.vbs"))
        self.assertEqual(source, backups[0].read_bytes())

    def test_no_switch_while_old_worker_or_codex_running(self):
        base = self.root / "codex-integration/bridge"
        (base / "bridge-status.json").write_text(json.dumps({"supervisorPid": 123, "workerPid": 456, "phase": "waiting-for-codex-pet"}))
        (base / "codex-state.json").write_text(json.dumps({"running": False}))
        self.runtime.matches.add((123, str(self.root / "runtime/pythonw.exe")))
        old = (self.root, self.root / "runtime/pythonw.exe")
        self.assertFalse(setup.safe_to_switch(old, self.runtime))
        (base / "bridge-status.json").write_text(json.dumps({"supervisorPid": 123, "workerPid": None, "phase": "waiting-for-codex-pet"}))
        (base / "codex-state.json").write_text(json.dumps({"running": True}))
        self.assertFalse(setup.safe_to_switch(old, self.runtime))
        (base / "codex-state.json").write_text(json.dumps({"running": False}))
        self.assertTrue(setup.safe_to_switch(old, self.runtime))
        # WorkerPolicy treats a graceful old worker exit as manually closed.
        (base / "bridge-status.json").write_text(json.dumps({"supervisorPid": 123, "workerPid": None, "phase": "manually-closed"}))
        self.assertTrue(setup.safe_to_switch(old, self.runtime))

    def test_deferred_activation_cancels_after_disable_or_newer_install(self):
        self.startup.mkdir(parents=True)
        startup = self.startup / "BailongCodexBridge.vbs"
        for content in (None, setup.startup_bytes(self.base / "newer-package")):
            if content is not None:
                startup.write_bytes(content)
            else:
                startup.unlink(missing_ok=True)
            self.assertTrue(setup.finish_when_idle(self.root, None, self.runtime,
                                                  maximum_seconds=1, startup_dir=self.startup))
            self.assertEqual([], self.runtime.launches)

    def test_deferred_activation_rechecks_request_immediately_before_launch(self):
        self.startup.mkdir(parents=True)
        startup = self.startup / "BailongCodexBridge.vbs"
        startup.write_bytes(setup.startup_bytes(self.root))
        def concurrent_disable():
            startup.unlink()
            return False
        with mock.patch.object(self.runtime, "mutex_open", side_effect=concurrent_disable):
            self.assertTrue(setup.finish_when_idle(self.root, None, self.runtime,
                                                  maximum_seconds=1, startup_dir=self.startup))
        self.assertEqual([], self.runtime.launches)

    def test_deferred_activation_exits_when_new_package_is_already_running(self):
        self.startup.mkdir(parents=True)
        (self.startup / "BailongCodexBridge.vbs").write_bytes(setup.startup_bytes(self.root))
        self.ready_status()
        self.runtime.matches.add((123, str(self.root / "runtime/pythonw.exe")))
        self.runtime.mutex = True
        self.assertTrue(setup.finish_when_idle(self.root, None, self.runtime,
                                              maximum_seconds=1, startup_dir=self.startup))
        self.assertEqual([], self.runtime.launches)

    def test_missing_animation_is_rejected_before_installation(self):
        (self.root / "codex-integration/clean-assets/animation3.webp").unlink()
        with self.assertRaises(setup.InstallError):
            self.install()
        self.assertFalse(self.user.exists())

    def test_missing_uninstaller_or_bridge_dependency_is_rejected_before_install(self):
        for name in ("uninstall.ps1", "codex-integration/bridge/native_menu_shield.cs"):
            path = self.root / name
            original = path.read_bytes()
            path.unlink()
            try:
                with self.assertRaises(setup.InstallError):
                    self.install()
                self.assertFalse(self.user.exists())
                self.assertEqual(self.runtime.launches, [])
            finally:
                path.write_bytes(original)

    @unittest.skipUnless(os.name == "nt", "Windows mutex test")
    def test_duplicate_activation_waiters_are_suppressed(self):
        first = setup.ActivationGuard(self.root)
        second = setup.ActivationGuard(self.root)
        try:
            self.assertTrue(first.acquired)
            self.assertFalse(second.acquired)
        finally:
            second.close()
            first.close()

    def test_stale_status_cannot_request_stop(self):
        base = self.root / "codex-integration/bridge"
        status = base / "bridge-status.json"
        status.write_text(json.dumps({"supervisorPid": 123, "workerPid": None, "phase": "waiting-for-codex-pet"}))
        (base / "codex-state.json").write_text(json.dumps({"running": False}))
        self.runtime.matches.add((123, str(self.root / "runtime/pythonw.exe")))
        os.utime(status, (0, 0))
        self.assertFalse(setup.safe_to_switch((self.root, self.root / "runtime/pythonw.exe"), self.runtime))

    def test_startup_parser_rejects_extra_commands_and_other_scripts(self):
        raw = setup.startup_bytes(self.root)
        self.assertIsNotNone(setup.parse_old_startup(raw))
        self.assertIsNone(setup.parse_old_startup((raw.decode("utf-16") + 'shell.Run "bad.exe"\n').encode("utf-16")))
        self.assertIsNone(setup.parse_old_startup(raw.decode("utf-16").replace("bridge_supervisor.py", "other.py").encode("utf-16")))

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_junction_destination_is_rejected(self):
        self.user.mkdir()
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        command = f"New-Item -ItemType Junction -Path '{self.home}' -Target '{elsewhere}' | Out-Null"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command], capture_output=True)
        if result.returncode:
            self.skipTest("junction unavailable")
        try:
            with self.assertRaises(setup.InstallError):
                self.install()
            self.assertEqual([], list(elsewhere.iterdir()))
        finally:
            self.home.rmdir()  # Remove the junction itself, never recurse into target.


if __name__ == "__main__":
    unittest.main()
