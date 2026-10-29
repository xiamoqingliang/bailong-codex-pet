"""Startup diagnostics with temporary synthetic modules, never the real pet."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import bridge_pet
import bridge_supervisor as supervisor


SECRET = "PRIVATE-CHAT-TOKEN-MUST-NOT-BE-SAVED"


class WorkerDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="BailongDiagnosticFixture-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        shutil.copyfile(bridge_pet.__file__, self.base / "bridge_pet.py")

    def write_module(self, name, code):
        (self.base / (name + ".py")).write_text(code, encoding="utf-8")

    def bridge_stubs(self):
        self.write_module("bridge_policy", "full_mode_visible = lambda state: False\n")
        self.write_module("pet_lifecycle", "class PetLifecycle: pass\n")
        self.write_module("native_visibility", """
class BoundedStateFile:
    def __init__(self, **kwargs): pass
mask_ready = observer_generation = request_visibility = valid_observer_state = None
""")

    def launch_fixture(self):
        started = time.time()
        # The bundled interpreter uses ._pth and omits the script directory.
        # Insert the fixture explicitly so it cannot import production modules.
        bootstrap = "import sys,runpy;sys.path.insert(0,sys.argv[1]);runpy.run_path(sys.argv[2],run_name='__main__')"
        child = supervisor.launch_hidden(
            [sys.executable, "-c", bootstrap, str(self.base), str(self.base / "bridge_pet.py")], self.base)
        try:
            child.wait(timeout=10)
            stage = supervisor.read_worker_stage(self.base / "pet-status.json", child.pid, started)
            return child.returncode, stage
        finally:
            if child.poll() is None:
                child.terminate()  # only the fixture returned by this test
                child.wait(timeout=5)

    def assert_no_secret_saved(self):
        for path in (self.base / "pet-status.json", self.base / "pet-errors.log"):
            if path.exists():
                self.assertNotIn(SECRET, path.read_text(encoding="utf-8"))

    def test_top_level_import_failure_is_recorded_before_main(self):
        self.write_module("bridge_policy", f"raise RuntimeError({SECRET!r})\n")
        code, stage = self.launch_fixture()
        self.assertEqual(code, 1)
        self.assertEqual(stage, {"workerPhase": "error", "workerStage": "bridge-import"})
        report = json.loads((self.base / "pet-errors.log").read_text())
        self.assertEqual(report["errorType"], "RuntimeError")
        self.assertEqual(report["frames"][-1]["file"], "bridge_policy.py")
        self.assertEqual(set(report["frames"][-1]), {"file", "line", "function"})
        self.assert_no_secret_saved()

    def test_engine_import_failure_has_exact_stage_without_exception_text(self):
        self.bridge_stubs()
        self.write_module("bridge_loader", f"def load_original_module():\n    raise ImportError({SECRET!r})\n")
        code, stage = self.launch_fixture()
        self.assertEqual(code, 1)
        self.assertEqual(stage["workerStage"], "original-import")
        report = json.loads((self.base / "pet-errors.log").read_text())
        self.assertEqual(report["errorType"], "ImportError")
        self.assert_no_secret_saved()

    def test_qt_native_abort_keeps_last_stage_without_saving_stderr(self):
        self.bridge_stubs()
        self.write_module("bridge_loader", f"""
import os
from types import SimpleNamespace
def application(*args):
    os.write(2, b'Could not load the Qt platform plugin ' + {SECRET.encode()!r})
    os._exit(3)
def load_original_module(): return SimpleNamespace(QApplication=application)
""")
        self.write_module("desktop_chat_link", "install_desktop_chat_link=lambda original:None\n")
        self.write_module("conversation_sync", "install_conversation_sync=lambda original:None\n")
        self.write_module("asset_source", "configure_asset_source=lambda original:{}\n")
        self.write_module("bailong_toolbar", "create_toolbar=None\n")
        self.write_module("activity_cards", "create_activity_cards=None\n")
        self.write_module("v051_behavior", "V051Behavior=MetadataActivityDriver=None\nconfigure_preferences=lambda original:None\n")
        self.write_module("dock_controls", "create_dock_controls=None\n")
        self.write_module("activity_runtime", "ActivityRuntime=None\n")
        code, stage = self.launch_fixture()
        self.assertEqual(code, 3)
        self.assertEqual(stage, {"workerPhase": "initializing", "workerStage": "qt-init"})
        self.assert_no_secret_saved()

    def test_old_or_unrecognized_status_cannot_supply_diagnostic_text(self):
        path = self.base / "pet-status.json"
        now = time.time()
        path.write_text(json.dumps({"pid": 9, "updatedAt": now,
                                    "phase": "error", "stage": SECRET}))
        result = supervisor.read_worker_stage(path, 10, now - 1)
        self.assertEqual(result["workerStage"], "process-created")
        result = supervisor.read_worker_stage(path, 9, now - 1)
        self.assertEqual(result["workerStage"], "unknown")
        result = supervisor.read_worker_stage(path, 9, now + 1)
        self.assertEqual(result["workerStage"], "process-created")

    def test_error_report_replaces_large_old_log_and_keeps_no_source_line(self):
        path = self.base / "pet-errors.log"
        path.write_text(SECRET * 5000)
        with patch.object(bridge_pet, "DIRECTORY", self.base):
            try:
                raise RuntimeError(SECRET)
            except RuntimeError:
                bridge_pet.log_error()
        self.assertLess(path.stat().st_size, 10000)
        self.assertNotIn(SECRET, path.read_text())


if __name__ == "__main__":
    unittest.main()
