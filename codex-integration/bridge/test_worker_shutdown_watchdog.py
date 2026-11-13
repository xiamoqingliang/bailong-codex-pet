"""Bound shutdown in real disposable workers without touching Codex or assets."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import bridge_pet
from pet_lifecycle import ExitWatchdog


WORKER = r'''
import json,os,sys,threading,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
os.environ['QT_QPA_PLATFORM']='offscreen'
from bridge_loader import load_original_module
load_original_module()  # Register bundled Qt modules without constructing the pet.
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication,QWidget
import bridge_pet
from pet_lifecycle import ExitWatchdog,PetLifecycle
root=Path(sys.argv[2])
bridge_pet.STATUS_FILE=root/'pet-status.json'
mode=sys.argv[3]
reason=sys.argv[4]
app=QApplication(['disposable-shutdown-fixture','-platform','offscreen'])
class BlockedOriginal(QWidget):
    def closeEvent(self,event): threading.Event().wait()
class FixturePet(PetLifecycle,BlockedOriginal):
    host_pid=101
    def __init__(self):
        super().__init__()
        self._exit_watchdog=ExitWatchdog(
            lambda: bridge_pet.force_shutdown_exit(self.exit_reason,self.exit_host_pid),
            seconds=0.3)
    def _publish_closing(self):
        bridge_pet.save_status({'pid':__import__('os').getpid(),'phase':'closing',
            'updatedAt':time.time(),'exitReason':self.exit_reason,'exitHostPid':self.exit_host_pid})
    def _read_bridge_state(self): return {'running':True,'pid':101}
    def _request_native_visibility(self,visible,state):
        (root/'native-hide-requested').write_text(str(visible))
pet=FixturePet()
if mode=='blocked-status':
    def cannot_write(value): raise PermissionError('fixture status unavailable')
    bridge_pet.save_status=cannot_write
if mode=='blocked-ui':
    pet._hide_exit_ui=lambda: threading.Event().wait()
QTimer.singleShot(0,lambda:pet.request_exit(reason))
app.exec()
raise AssertionError('Blocked close handler unexpectedly returned')
'''


class ShutdownWatchdogTests(unittest.TestCase):
    def test_default_budget_is_fifteen_seconds_and_stays_unarmed_during_normal_work(self):
        expired = mock.Mock()
        with mock.patch("pet_lifecycle.threading.Timer") as timer:
            watchdog = ExitWatchdog(expired)
            self.assertEqual(watchdog.seconds, 15.0)
            timer.assert_not_called()
            watchdog.cancel()
        expired.assert_not_called()

    def test_repeated_start_uses_one_deadline_and_cancel_prevents_timeout(self):
        expired = mock.Mock()
        watchdog = ExitWatchdog(expired, seconds=0.1)
        self.addCleanup(watchdog.cancel)
        self.assertTrue(watchdog.start())
        self.assertFalse(watchdog.start())
        watchdog.cancel()
        time.sleep(0.2)
        expired.assert_not_called()
        self.assertFalse(watchdog.start())

    def run_blocked_worker(self, mode, reason):
        with tempfile.TemporaryDirectory(prefix="bailong-shutdown-watchdog-") as name:
            root = Path(name)
            started = time.monotonic()
            result = subprocess.run(
                [sys.executable, "-B", "-c", WORKER,
                 str(Path(bridge_pet.__file__).parent), str(root), mode, reason],
                capture_output=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            self.assertLess(time.monotonic() - started, 4)
            self.assertEqual((root / "native-hide-requested").exists(),
                             reason == "user" and mode != "blocked-ui")
            path = root / "pet-status.json"
            return json.loads(path.read_text()) if path.exists() else None

    def test_blocked_original_close_exits_and_preserves_automatic_and_user_reasons(self):
        for reason in ("codex-closed", "supervisor-stop", "user"):
            with self.subTest(reason=reason):
                status = self.run_blocked_worker("blocked-chat", reason)
                self.assertEqual(status["phase"], "stopped")
                self.assertEqual(status["exitReason"], reason)
                self.assertEqual(status["exitHostPid"], 101)
                self.assertTrue(status["exitWatchdogExpired"])

    def test_blocked_qt_visibility_work_does_not_prevent_timer_exit(self):
        status = self.run_blocked_worker("blocked-ui", "codex-closed")
        self.assertTrue(status["exitWatchdogExpired"])
        self.assertEqual(status["exitReason"], "codex-closed")

    def test_status_write_failure_cannot_leave_the_process_alive(self):
        self.assertIsNone(self.run_blocked_worker("blocked-status", "codex-closed"))


class ConcurrentStatusWriteTests(unittest.TestCase):
    def test_two_writers_atomically_publish_distinct_complete_json(self):
        with tempfile.TemporaryDirectory(prefix="bailong-concurrent-status-") as name:
            root = Path(name)
            target = root / "pet-status.json"
            barrier = threading.Barrier(2, timeout=3)
            replace = os.replace
            staged = []
            staged_paths = set()
            staged_lock = threading.Lock()
            values = [{"writer": index, "payload": str(index) * 10000} for index in range(2)]

            def synchronized_replace(source, destination):
                with staged_lock:
                    first_attempt = source not in staged_paths
                    if first_attempt:
                        staged_paths.add(source)
                        staged.append((Path(source), json.loads(Path(source).read_text(encoding="utf-8"))))
                # A Windows sharing collision can legitimately retry replace;
                # synchronize each writer's first attempt, not its retries.
                if first_attempt:
                    barrier.wait()
                return replace(source, destination)

            with mock.patch.object(bridge_pet.os, "replace", side_effect=synchronized_replace):
                with ThreadPoolExecutor(max_workers=2) as workers:
                    writes = [workers.submit(bridge_pet.write_json, target, value) for value in values]
                    for write in writes:
                        write.result(timeout=5)
            self.assertEqual(len({path for path, _ in staged}), 2)
            self.assertCountEqual([value for _, value in staged], values)
            self.assertIn(json.loads(target.read_text(encoding="utf-8")), values)
            self.assertEqual(list(root.glob("*.tmp")), [])

    def test_failed_writer_cleans_only_its_own_temporary_file(self):
        with tempfile.TemporaryDirectory(prefix="bailong-failed-status-") as name:
            root = Path(name)
            target = root / "pet-status.json"
            unrelated = root / "unrelated.tmp"
            unrelated.write_text("preserve", encoding="utf-8")
            barrier = threading.Barrier(2, timeout=3)
            failed_done = threading.Event()
            replace = os.replace
            first_failure = True
            staged = []

            def synchronized_replace(source, destination):
                nonlocal first_failure
                value = json.loads(Path(source).read_text(encoding="utf-8"))
                if value["writer"] == "failing":
                    if first_failure:
                        first_failure = False
                        staged.append(Path(source))
                        barrier.wait()
                    raise PermissionError("isolated denied replacement")
                staged.append(Path(source))
                barrier.wait()
                if not failed_done.wait(3):
                    raise AssertionError("Failed writer did not finish its cleanup")
                self.assertTrue(Path(source).is_file())
                return replace(source, destination)

            def fail_write():
                try:
                    with self.assertRaises(PermissionError):
                        bridge_pet.write_json(target, {"writer": "failing"})
                finally:
                    failed_done.set()

            with mock.patch.object(bridge_pet.os, "replace", side_effect=synchronized_replace):
                with ThreadPoolExecutor(max_workers=2) as workers:
                    writes = [workers.submit(fail_write),
                              workers.submit(bridge_pet.write_json, target, {"writer": "successful"})]
                    for write in writes:
                        write.result(timeout=5)
            self.assertEqual(len(set(staged)), 2)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"writer": "successful"})
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "preserve")
            self.assertEqual(list(root.glob("*.tmp")), [unrelated])


if __name__ == "__main__":
    unittest.main()
