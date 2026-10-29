"""Host restart/worker shutdown regressions, without starting the real bridge."""
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from bridge_supervisor import Observation, WorkerExit, WorkerPolicy, read_worker_exit


OLD = Observation(True, True, 111, True, True, "custom:bailong-full", True, 222)
NEW = replace(OLD, pid=333, overlay_hwnd=444)
ABSENT = Observation(False, False, None)


class WorkerRestartTests(unittest.TestCase):
    def test_automatic_host_shutdown_waits_for_the_next_host_without_manual_suppression(self):
        policy = WorkerPolicy()
        policy.observe(OLD)
        generation = policy.started()
        policy.observe(ABSENT)
        policy.exited(0, 1, generation, exit_reason="codex-closed", exit_host_pid=111)
        self.assertFalse(policy.manual_suppressed)
        self.assertFalse(policy.should_launch(2))
        policy.observe(NEW)
        self.assertTrue(policy.should_launch(3))

    def test_old_auto_close_after_generation_adoption_cannot_suppress_new_host(self):
        for reason in ("codex-closed", "supervisor-stop"):
            with self.subTest(reason=reason):
                policy = WorkerPolicy()
                policy.observe(OLD)
                policy.started()
                policy.observe(ABSENT)
                policy.observe(NEW)
                adopted_generation = policy.started()
                policy.exited(0, 4, adopted_generation, exit_reason=reason, exit_host_pid=111)
                self.assertFalse(policy.manual_suppressed)
                self.assertTrue(policy.should_launch(4))
                self.assertFalse(WorkerExit(reason, 111, True).should_hide(NEW))

    def test_user_close_from_old_host_does_not_hide_or_suppress_new_host(self):
        policy = WorkerPolicy()
        policy.observe(OLD)
        policy.started()
        policy.observe(NEW)
        adopted_generation = policy.started()
        policy.exited(0, 1, adopted_generation, exit_reason="user", exit_host_pid=111)
        self.assertTrue(policy.should_launch(1))
        self.assertFalse(WorkerExit("user", 111, True).should_hide(NEW))

    def test_real_user_quit_suppresses_this_host_but_not_next_codex_open(self):
        policy = WorkerPolicy()
        policy.observe(OLD)
        generation = policy.started()
        policy.exited(0, 1, generation, exit_reason="user", exit_host_pid=111)
        self.assertTrue(policy.manual_suppressed)
        self.assertFalse(policy.should_launch(2))
        self.assertTrue(WorkerExit("user", 111, True).should_hide(OLD))
        policy.observe(NEW)
        self.assertFalse(policy.manual_suppressed)
        self.assertTrue(policy.should_launch(3))

    def test_unknown_selection_recovery_cannot_reopen_a_manually_exited_worker(self):
        for native_open in (True, False):
            with self.subTest(native_open=native_open):
                policy = WorkerPolicy()
                policy.observe(OLD)
                generation = policy.started()
                policy.exited(0, 1, generation, exit_reason="user", exit_host_pid=111)
                policy.observe(replace(OLD, selection_known=False, full_selected=False,
                                       overlay_open=native_open, selected_avatar_id=None))
                policy.observe(None)
                policy.observe(OLD)
                self.assertTrue(policy.manual_suppressed)
                self.assertEqual(policy.generation, generation)
                self.assertFalse(policy.should_launch(100))

    def test_pid_reuse_is_a_new_host_but_learning_start_time_is_not(self):
        policy = WorkerPolicy()
        policy.observe(OLD)
        policy.exited(0, 1, policy.started())
        policy.observe(replace(OLD, app_started_at=10.0))
        self.assertFalse(policy.should_launch(2))
        policy.observe(OLD)  # A temporary missing timestamp is not a restart.
        self.assertFalse(policy.should_launch(3))
        policy.observe(replace(OLD, app_started_at=11.0))
        self.assertTrue(policy.should_launch(4))

    def test_observed_host_exit_resets_quit_even_if_pid_is_reused(self):
        policy = WorkerPolicy()
        policy.observe(OLD)
        policy.exited(0, 1, policy.started())
        policy.observe(ABSENT)
        self.assertFalse(policy.should_launch(2))
        policy.observe(OLD)
        self.assertTrue(policy.should_launch(3))

    def test_explicit_show_during_async_close_survives_the_old_worker_final_exit(self):
        policy = WorkerPolicy()
        policy.observe(OLD)
        generation = policy.started()
        # The supervisor sees the closing record before Qt/chat cleanup ends.
        policy.exited(0, 1, generation, exit_reason="user", exit_host_pid=111)
        policy.observe(replace(OLD, overlay_open=False))
        self.assertFalse(policy.should_launch(2))
        policy.observe(OLD)  # New explicit native Show while old worker closes.
        self.assertTrue(policy.should_launch(3))
        policy.exited(0, 4, generation, exit_reason="user", exit_host_pid=111)
        self.assertTrue(policy.should_launch(4))

    def test_supervisor_stop_is_not_user_quit_or_an_immediate_restart_loop(self):
        policy = WorkerPolicy()
        policy.observe(OLD)
        generation = policy.started()
        policy.exited(0, 1, generation, exit_reason="supervisor-stop", exit_host_pid=111)
        self.assertFalse(policy.manual_suppressed)
        self.assertFalse(policy.should_launch(100))
        self.assertFalse(WorkerExit("supervisor-stop", 111, True).should_hide(OLD))
        policy.observe(NEW)
        self.assertTrue(policy.should_launch(101))

    def test_unverified_zero_exit_never_hides_the_native_mini(self):
        for info in (WorkerExit("unknown"), WorkerExit("user", 111, False),
                     WorkerExit("user", None, True), WorkerExit("error", 111, True)):
            self.assertFalse(info.should_hide(OLD))


class WorkerExitFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="BailongWorkerExit-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "pet-status.json"
        self.started = time.time() - 1

    def write(self, **changes):
        data = {"pid": 222, "phase": "stopped", "updatedAt": time.time(),
                "exitReason": "codex-closed", "exitHostPid": 111}
        data.update(changes)
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def test_pid_bound_fresh_final_status_is_recognized(self):
        self.write()
        self.assertEqual(read_worker_exit(self.path, 222, 0, self.started),
                         WorkerExit("codex-closed", 111, True))
        self.write(phase="error", exitReason="error")
        self.assertEqual(read_worker_exit(self.path, 222, 1, self.started),
                         WorkerExit("error", 111, True))

    def test_fresh_closing_intent_is_available_before_process_exit(self):
        self.write(phase="closing", exitReason="user")
        self.assertEqual(read_worker_exit(self.path, 222, 0, self.started, closing=True),
                         WorkerExit("user", 111, True))
        self.assertEqual(read_worker_exit(self.path, 222, 0, self.started), WorkerExit("unknown"))
        self.write(phase="running", exitReason="user")
        self.assertEqual(read_worker_exit(self.path, 222, 0, self.started, closing=True),
                         WorkerExit("unknown"))

    def test_wrong_pid_old_stamp_invalid_phase_and_reason_are_not_trusted(self):
        for changes in ({"pid": 223}, {"pid": True}, {"updatedAt": self.started - 1},
                        {"updatedAt": time.time() + 10}, {"updatedAt": True},
                        {"phase": "running"}, {"phase": "existing-pet"},
                        {"exitReason": "invalid"}, {"exitReason": "error"}):
            with self.subTest(changes=changes):
                self.write(**changes)
                result = read_worker_exit(self.path, 222, 0, self.started)
                self.assertEqual(result, WorkerExit("unknown"))
                self.assertFalse(result.should_hide(NEW))
        self.write()
        os.utime(self.path, (self.started - 1, self.started - 1))
        self.assertEqual(read_worker_exit(self.path, 222, 0, self.started), WorkerExit("unknown"))

    def test_missing_or_partial_status_has_no_user_authority(self):
        self.assertEqual(read_worker_exit(self.path, 222, 0), WorkerExit("unknown"))
        self.path.write_text("{", encoding="utf-8")
        self.assertEqual(read_worker_exit(self.path, 222, 0), WorkerExit("unknown"))
        self.assertEqual(read_worker_exit(self.path, 222, 2), WorkerExit("error"))


if __name__ == "__main__":
    unittest.main()
