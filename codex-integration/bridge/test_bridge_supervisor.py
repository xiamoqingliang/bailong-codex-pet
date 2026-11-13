"""Lifecycle tests use temporary state files; no background bridge is started."""
import json
import io
import logging
import os
import subprocess
from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import bridge_policy
import bridge_supervisor as supervisor
from bridge_supervisor import (Observation, WorkerPolicy, WindowsMutex, NativeMiniController,
                               launch_hidden, native_control_command, process_alive,
                               read_observation, observer_command, pythonw_path, python_path)


OPEN = Observation(True, True, 123, True, True, "custom:bailong-full", True, 456)
FULL_HIDDEN = replace(OPEN, overlay_open=False)
CLOSED = Observation(True, False, 123, False, True, "codex", True)


class StartupTests(unittest.TestCase):
    def run_one_iteration(self, contents, *, missing_worker=False, stop_mode="marker"):
        """Exercise actual startup and shutdown with no native UI/process work."""
        with tempfile.TemporaryDirectory(prefix="bailong-first-start-") as folder:
            base = Path(folder) / "codex-integration/bridge"
            base.mkdir(parents=True)
            if contents is not None:
                (base / "bridge-status.json").write_bytes(contents)
            if not missing_worker:
                (base / "bridge_pet.py").write_text("# fixture")
            (base / "codex_observer.ps1").write_text("# fixture")
            mutex = Mock()
            mutex.acquire.return_value = True
            controller = Mock(process=None)
            controller.status.return_value = {}
            observer = Mock(pid=4567)
            observer.poll.return_value = 0

            def stop_after_iteration(_seconds):
                if stop_mode == "interrupt":
                    raise KeyboardInterrupt
                target = base.parents[1] / "uninstall.request" if stop_mode == "uninstall" else base / "supervisor.stop"
                target.write_text("test complete")

            with (patch.object(supervisor, "WindowsMutex", return_value=mutex),
                  patch.object(supervisor, "NativeMiniController", return_value=controller) as controller_factory,
                  patch.object(supervisor, "launch_hidden", return_value=observer) as launch,
                  patch.object(supervisor, "read_observation", return_value=None),
                  patch.object(supervisor, "process_alive", return_value=False),
                  patch("detached_launcher.process_requires_detach", return_value=False),
                  patch.object(supervisor.time, "sleep", side_effect=stop_after_iteration),
                  patch.object(supervisor.sys, "stderr", io.StringIO())):
                result = supervisor.run_supervisor(base)
            state = json.loads((base / "bridge-status.json").read_text(encoding="utf-8"))
            error_path = base / "launch-error.json"
            error = json.loads(error_path.read_text(encoding="utf-8")) if error_path.exists() else None
            self.assertEqual(mutex.close.call_count, 1)
            controller.close.assert_called_once()
            self.assertEqual(launch.call_count, 0 if missing_worker else 1)
            return result, state, controller_factory.call_args.kwargs, error

    def test_first_start_and_unusable_old_status_reach_observer_loop(self):
        for contents in (None, b"{truncated", b"\xff", b"null", b"[]", b"true", b'"old"'):
            with self.subTest(contents=contents):
                result, state, controller_args, error = self.run_one_iteration(contents)
                self.assertEqual(result, 0)
                self.assertEqual(state["exitCode"], 0)
                self.assertEqual(state["phase"], "stopped")
                self.assertEqual(state["stopReason"], "supervisor-stop")
                self.assertTrue(state["independentProcess"])
                self.assertIsNone(controller_args["previous_host"])
                self.assertIsNone(error)

    def test_invalid_pending_pids_do_not_prevent_startup(self):
        for pending in (None, 7, "123", {}, [None, True, "123", -1, 0, {}, 42]):
            with self.subTest(pending=pending):
                result, state, _, error = self.run_one_iteration(
                    json.dumps({"pendingChildPids": pending}).encode())
                self.assertEqual(result, 0)
                self.assertEqual(state["pendingChildPids"], [])
                self.assertIsNone(error)

    def test_normal_shutdown_reason_distinguishes_uninstall_and_interrupt(self):
        for mode, reason in (("uninstall", "uninstall-request"), ("interrupt", "keyboard-interrupt")):
            with self.subTest(mode=mode):
                result, state, _, error = self.run_one_iteration(None, stop_mode=mode)
                self.assertEqual(result, 0)
                self.assertEqual(state["stopReason"], reason)
                self.assertIsNone(error)

    def test_previous_host_is_restored_and_legacy_pid_remains_supported(self):
        cases = (
            ({"nativeControl": {"lastHost": [123, 100.5]}, "codexPid": 456}, (123, 100.5)),
            ({"nativeControl": {"lastHost": [123, None]}}, (123, None)),
            ({"codexPid": 456}, (456, None)),
            ({"nativeControl": "invalid", "codexPid": 456}, (456, None)),
            ({"nativeControl": {"lastHost": [True, 100]}, "codexPid": True}, None),
        )
        for old_status, expected in cases:
            with self.subTest(old_status=old_status):
                result, _, controller_args, _ = self.run_one_iteration(json.dumps(old_status).encode())
                self.assertEqual(result, 0)
                self.assertEqual(controller_args["previous_host"], expected)

    def test_startup_failure_records_pid_scoped_error_and_cleans_up(self):
        result, state, _, error = self.run_one_iteration(None, missing_worker=True)
        self.assertEqual(result, 1)
        self.assertEqual(state["exitCode"], 1)
        self.assertEqual(state["stopReason"], "error")
        self.assertEqual(error["pid"], os.getpid())
        self.assertEqual(error["errorType"], "FileNotFoundError")
        self.assertIn("bridge_pet.py", error["reason"])

    def test_main_reports_errors_before_supervisor_logger_is_initialized(self):
        with tempfile.TemporaryDirectory(prefix="bailong-start-error-") as folder:
            base = Path(folder) / "codex-integration/bridge"
            base.mkdir(parents=True)
            with (patch.object(supervisor, "BASE", base),
                  patch("detached_launcher.process_requires_detach", return_value=False),
                  patch.object(supervisor, "run_supervisor", side_effect=RuntimeError("fixture startup failure")),
                  patch.object(supervisor.sys, "stderr", io.StringIO())):
                self.assertEqual(supervisor.main([]), 1)
            error = json.loads((base / "launch-error.json").read_text(encoding="utf-8"))
            self.assertEqual(error["pid"], os.getpid())
            self.assertEqual(error["errorType"], "RuntimeError")
            self.assertEqual(error["reason"], "fixture startup failure")

    def test_verified_persistent_job_starts_without_detaching_again(self):
        with tempfile.TemporaryDirectory(prefix="bailong-verified-start-") as folder:
            base = Path(folder) / "codex-integration/bridge"
            base.mkdir(parents=True)
            with (patch.object(supervisor, "BASE", base),
                  patch("detached_launcher.process_in_job", return_value=True),
                  patch("detached_launcher.process_requires_detach", return_value=False),
                  patch("detached_launcher.launch_detached") as detach,
                  patch.object(supervisor, "run_supervisor", return_value=0) as run):
                self.assertEqual(supervisor.main(["--show-on-start"]), 0)
            detach.assert_not_called()
            run.assert_called_once_with(base, show_on_start=True)

    def test_unwritable_error_report_does_not_mask_original_exception(self):
        with tempfile.TemporaryDirectory(prefix="bailong-start-error-") as folder:
            stderr = io.StringIO()
            with patch.object(supervisor.sys, "stderr", stderr):
                supervisor.write_launch_error(Path(folder) / "missing", ValueError("original failure"))
            self.assertIn("ValueError: original failure", stderr.getvalue())

    def test_failed_fixture_worker_publishes_stage_and_exit_code(self):
        with tempfile.TemporaryDirectory(prefix="bailong-worker-status-") as folder:
            base = Path(folder) / "codex-integration/bridge"
            base.mkdir(parents=True)
            for name in ("bridge_pet.py", "codex_observer.ps1"):
                (base / name).write_text("# inert fixture")
            mutex = Mock()
            mutex.acquire.return_value = True
            controller = Mock(process=None)
            controller.status.return_value = {}
            observer, worker = Mock(pid=1234), Mock(pid=2345)
            observer.poll.return_value = None
            worker.poll.return_value = 3
            def fake_launch(command, _base):
                if Path(command[1]).name != "bridge_pet.py":
                    return observer
                path = base / "pet-status.json"
                written = time.time()
                path.write_text(json.dumps({"pid": worker.pid,
                    "updatedAt": written, "phase": "initializing", "stage": "qt-init"}))
                os.utime(path, (written, written))
                return worker

            ticks = 0
            def sleep(_seconds):
                nonlocal ticks
                ticks += 1
                if ticks >= 2:
                    observer.poll.return_value = 0
                    (base / "supervisor.stop").write_text("test complete")

            with (patch.object(supervisor, "WindowsMutex", return_value=mutex),
                  patch.object(supervisor, "NativeMiniController", return_value=controller),
                  patch.object(supervisor, "launch_hidden", side_effect=fake_launch) as launch,
                  patch.object(supervisor, "read_observation", return_value=OPEN),
                  patch.object(supervisor, "process_alive", return_value=False),
                  patch("detached_launcher.process_requires_detach", return_value=False),
                  patch.object(supervisor.time, "sleep", side_effect=sleep)):
                self.assertEqual(supervisor.run_supervisor(base), 0)
            self.assertEqual(launch.call_count, 2)
            state = json.loads((base / "bridge-status.json").read_text())
            self.assertEqual(state["lastWorkerExit"]["workerStage"], "qt-init")
            self.assertEqual(state["lastWorkerExit"]["code"], 3)


class OwnedWorkerShutdownTests(unittest.TestCase):
    class Clock:
        def __init__(self):
            self.now = 0.0
            self.on_sleep = lambda: None

        def sleep(self, seconds):
            self.now += seconds
            if self.now > 100:
                raise AssertionError("fixture exceeded bounded shutdown")
            self.on_sleep()

    class Child:
        def __init__(self, pid, clock):
            self.pid, self.clock = pid, clock
            self.result = None
            self.killed_at = []

        def poll(self):
            return self.result

        def kill(self):
            self.killed_at.append(self.clock.now)
            self.result = 1

        def wait(self, timeout):
            if self.result is None:
                raise subprocess.TimeoutExpired("inert child", timeout)
            return self.result

    def run_scenario(self, *, reason="codex-closed", phase="closing", new_host=True,
                     intent_at=1.0, reuse_pid=False, natural_exit_at=None,
                     natural_exit_code=7, show_cycle=False):
        with tempfile.TemporaryDirectory(prefix="bailong-stuck-worker-") as folder:
            base = Path(folder) / "codex-integration/bridge"
            base.mkdir(parents=True)
            for name in ("bridge_pet.py", "codex_observer.ps1"):
                (base / name).write_text("# inert fixture")
            clock = self.Clock()
            observer = self.Child(9100, clock)
            workers = []
            first_host = replace(OPEN, app_started_at=10.0)
            next_host = replace(first_host, pid=OPEN.pid if reuse_pid else 789, app_started_at=11.0)
            intent_written = False

            def launch(command, _base):
                if Path(command[1]).name != "bridge_pet.py":
                    return observer
                self.assertFalse((base / "pet.stop").exists(), "old stop must not kill the next worker")
                worker = self.Child(9200 + len(workers), clock)
                workers.append(worker)
                return worker

            def observation(*args, **kwargs):
                if show_cycle and 1.0 <= clock.now < 2.0:
                    return replace(first_host, overlay_open=False)
                if new_host and clock.now >= 2.0:
                    return next_host
                if new_host and clock.now >= 1.0 and not reuse_pid:
                    return Observation(False, False, None)
                return first_host

            def on_sleep():
                nonlocal intent_written
                if reason is not None and not intent_written and workers and clock.now >= intent_at:
                    (base / "pet-status.json").write_text(json.dumps({
                        "pid": workers[0].pid, "updatedAt": time.time(), "phase": phase,
                        "exitReason": reason, "exitHostPid": first_host.pid, "stage": "running"}))
                    intent_written = True
                if natural_exit_at is not None and workers and clock.now >= natural_exit_at:
                    workers[0].result = natural_exit_code
                if clock.now >= 25.0:
                    (base / "supervisor.stop").write_text("fixture complete")
                    observer.result = 0
                    for worker in workers[1:]:
                        worker.result = 0

            clock.on_sleep = on_sleep
            mutex = Mock()
            mutex.acquire.return_value = True
            controller = Mock(process=None)
            controller.status.return_value = {}
            with (patch.object(supervisor, "WindowsMutex", return_value=mutex),
                  patch.object(supervisor, "NativeMiniController", return_value=controller),
                  patch.object(supervisor, "launch_hidden", side_effect=launch),
                  patch.object(supervisor, "read_observation", side_effect=observation),
                  patch.object(supervisor, "process_alive", return_value=False),
                  patch("detached_launcher.process_requires_detach", return_value=False),
                  patch.object(supervisor.time, "monotonic", side_effect=lambda: clock.now),
                  patch.object(supervisor.time, "sleep", side_effect=clock.sleep),
                  patch.object(supervisor, "request_visibility") as visibility):
                self.assertEqual(supervisor.run_supervisor(base), 0)
            state = json.loads((base / "bridge-status.json").read_text())
            return workers, state, visibility.call_args_list

    def test_stuck_auto_close_is_reaped_after_twenty_seconds_and_new_host_launches(self):
        workers, state, visibility = self.run_scenario()
        self.assertEqual(len(workers), 2)
        self.assertGreaterEqual(workers[0].killed_at[0], 21.0)
        self.assertLess(workers[0].killed_at[0], 21.2)
        self.assertEqual(state["lastWorkerExit"]["reason"], "codex-closed")
        self.assertTrue(state["lastWorkerExit"]["verified"])
        self.assertTrue(state["lastWorkerExit"]["forcedTermination"])
        self.assertEqual(state["workerCrashRetries"], 0)
        self.assertEqual(visibility, [])

    def test_stuck_user_quit_stays_suppressed_after_forced_termination(self):
        for phase in ("closing", "stopped"):
            with self.subTest(phase=phase):
                workers, state, visibility = self.run_scenario(reason="user", phase=phase, new_host=False)
                self.assertEqual(len(workers), 1)
                self.assertEqual(len(workers[0].killed_at), 1)
                self.assertEqual(state["lastWorkerExit"]["reason"], "user")
                self.assertEqual(state["workerCrashRetries"], 0)
                self.assertEqual(len(visibility), 1)

    def test_old_host_without_closing_record_cannot_block_new_host(self):
        workers, state, visibility = self.run_scenario(reason=None)
        self.assertEqual(len(workers), 2)
        self.assertGreaterEqual(workers[0].killed_at[0], 21.0)
        self.assertEqual(state["lastWorkerExit"]["reason"], "codex-closed")
        self.assertFalse(state["lastWorkerExit"]["verified"], "inferred retirement is not worker status")
        self.assertEqual(visibility, [])

    def test_old_user_close_never_hides_reused_pid_with_new_start_time(self):
        workers, state, visibility = self.run_scenario(reason="user", intent_at=3.0, reuse_pid=True)
        self.assertEqual(len(workers), 2)
        self.assertEqual(visibility, [])
        self.assertEqual(state["lastWorkerExit"]["reason"], "user")

    def test_watchdog_exit_code_preserves_trusted_user_close_without_relaunch(self):
        workers, state, _ = self.run_scenario(reason="user", new_host=False, natural_exit_at=15.0)
        self.assertEqual(len(workers), 1)
        self.assertEqual(workers[0].killed_at, [])
        self.assertEqual(state["lastWorkerExit"]["code"], 7)
        self.assertEqual(state["lastWorkerExit"]["reason"], "user")
        self.assertEqual(state["workerCrashRetries"], 0)

    def test_old_user_close_cannot_hide_same_host_new_show_cycle(self):
        # Cover both request_visibility branches in the actual supervisor
        # loop: still-alive stopped status, and an already-exited old worker.
        for exit_at in (2.0, 4.0):
            with self.subTest(exit_at=exit_at):
                workers, _, visibility = self.run_scenario(
                    reason="user", phase="stopped", new_host=False, intent_at=2.0,
                    natural_exit_at=exit_at, natural_exit_code=0, show_cycle=True)
                self.assertEqual(len(workers), 2)
                self.assertEqual(visibility, [], "old close must not Hide the new explicit Show")

    def test_final_shutdown_is_bounded_and_only_kills_retained_worker(self):
        clock = self.Clock()
        worker, observer, native = (self.Child(pid, clock) for pid in (11, 22, 33))
        with (patch.object(supervisor.time, "monotonic", side_effect=lambda: clock.now),
              patch.object(supervisor.time, "sleep", side_effect=clock.sleep)):
            remaining = supervisor.shutdown_owned_children(worker, observer, native, Mock())
        self.assertGreaterEqual(clock.now, 20.0)
        self.assertLess(clock.now, 20.3)
        self.assertEqual(len(worker.killed_at), 1)
        self.assertEqual(observer.killed_at, [])
        self.assertEqual(native.killed_at, [])
        self.assertEqual(remaining, [22, 33])

    def test_final_shutdown_does_not_restart_an_existing_closing_budget(self):
        clock = self.Clock()
        clock.now = 18.0
        worker = self.Child(11, clock)
        with (patch.object(supervisor.time, "monotonic", side_effect=lambda: clock.now),
              patch.object(supervisor.time, "sleep", side_effect=clock.sleep)):
            self.assertEqual(supervisor.shutdown_owned_children(worker, None, None, Mock(), closing_at=0.0), [])
        self.assertLess(clock.now, 20.5)
        self.assertEqual(len(worker.killed_at), 1)

    def test_fresh_host_evidence_is_required_for_retirement(self):
        host = (OPEN.pid, 10.0)
        self.assertFalse(supervisor.worker_host_retired(host, None))
        self.assertFalse(supervisor.worker_host_retired(host, replace(OPEN, app_started_at=None)))
        self.assertFalse(supervisor.worker_host_retired(host, replace(OPEN, app_started_at=10.0)))
        self.assertTrue(supervisor.worker_host_retired(host, replace(OPEN, app_started_at=11.0)))
        self.assertTrue(supervisor.worker_host_retired(host, Observation(False, False, None)))


class LifecycleTests(unittest.TestCase):
    def test_uninstalled_package_does_not_start_or_clear_its_stop_files(self):
        with tempfile.TemporaryDirectory(prefix="bailong-uninstalled-start-") as folder:
            root = Path(folder)
            base = root / "codex-integration/bridge"
            base.mkdir(parents=True)
            (root / "uninstall.request").write_text("uninstalled")
            stop = base / "supervisor.stop"
            stop.write_text("keep stop")
            with patch.object(supervisor, "WindowsMutex", side_effect=AssertionError("Must not launch")):
                self.assertEqual(supervisor.run_supervisor(base), 0)
                with patch.object(supervisor, "BASE", base):
                    self.assertEqual(supervisor.main([]), 0)
            self.assertEqual(stop.read_text(), "keep stop")

    def test_launch_requires_fresh_running_open_observation(self):
        policy = WorkerPolicy()
        for state in (None, Observation(False, False, None), CLOSED):
            policy.observe(state)
            self.assertFalse(policy.should_launch(0))
        policy.observe(OPEN)
        self.assertTrue(policy.should_launch(0))

    def test_manual_exit_waits_for_real_hide_show(self):
        policy = WorkerPolicy()
        policy.observe(OPEN)
        generation = policy.started()
        policy.exited(0, 1, generation)
        self.assertFalse(policy.should_launch(1000))
        policy.observe(None)
        policy.observe(OPEN)
        self.assertFalse(policy.should_launch(1001), "stale state must not reset manual quit")
        policy.observe(CLOSED)
        policy.observe(OPEN)
        self.assertTrue(policy.should_launch(1002))

    def test_new_codex_pid_resets_manual_exit(self):
        policy = WorkerPolicy()
        policy.observe(OPEN)
        policy.exited(0, 1, policy.started())
        policy.observe(replace(OPEN, pid=456))
        self.assertTrue(policy.should_launch(2))

    def test_crash_has_three_retries_at_thirty_second_intervals(self):
        policy = WorkerPolicy()
        policy.observe(OPEN)
        now = 0
        for retry in range(3):
            self.assertTrue(policy.should_launch(now))
            policy.exited(2, now, policy.started())
            self.assertEqual(policy.crash_retries, retry + 1)
            self.assertFalse(policy.should_launch(now + 29.99))
            now += 30
        self.assertTrue(policy.should_launch(now))
        policy.exited(2, now, policy.started())
        self.assertFalse(policy.should_launch(10000))
        policy.observe(CLOSED)
        policy.observe(OPEN)
        self.assertTrue(policy.should_launch(10001))
        self.assertEqual(policy.crash_retries, 0)

    def test_closed_or_stale_state_blocks_pending_retry(self):
        policy = WorkerPolicy()
        policy.observe(OPEN)
        policy.exited(1, 0, policy.started())
        policy.observe(None)
        self.assertFalse(policy.should_launch(31))
        policy.observe(OPEN)
        self.assertTrue(policy.should_launch(31))
        policy.observe(CLOSED)
        self.assertFalse(policy.should_launch(31))

    def test_exit_from_old_generation_does_not_suppress_new_codex(self):
        policy = WorkerPolicy()
        policy.observe(OPEN)
        generation = policy.started()
        policy.observe(replace(OPEN, pid=456))
        policy.exited(0, 2, generation)
        self.assertTrue(policy.should_launch(2))

    def test_surviving_worker_manual_quit_in_new_cycle_is_suppressed(self):
        policy = WorkerPolicy()
        policy.observe(OPEN)
        policy.started()
        policy.observe(CLOSED)
        policy.observe(OPEN)
        current_generation = policy.started()  # live worker adopts current cycle
        policy.exited(0, 2, current_generation)
        self.assertFalse(policy.should_launch(100))

    def test_native_hide_hides_worker_without_resetting_manual_quit(self):
        policy = WorkerPolicy()
        policy.observe(OPEN)
        generation = policy.started()
        policy.observe(FULL_HIDDEN)
        self.assertFalse(policy.visible)
        self.assertEqual(policy.generation, generation)
        policy.exited(0, 1, generation)
        policy.observe(FULL_HIDDEN)
        self.assertFalse(policy.should_launch(20))

    def test_user_show_native_mini_reopens_manually_quit_full_worker(self):
        policy = WorkerPolicy()
        policy.observe(FULL_HIDDEN)
        generation = policy.started()
        policy.exited(0, 1, generation)
        self.assertFalse(policy.should_launch(20))
        policy.observe(OPEN)
        self.assertTrue(policy.should_launch(21))
        self.assertGreater(policy.generation, generation)

    def test_native_show_other_pet_does_not_launch_full_worker(self):
        policy = WorkerPolicy()
        policy.observe(CLOSED)
        policy.observe(replace(CLOSED, overlay_open=True))
        self.assertFalse(policy.should_launch(0))


class StateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        config = Path(self.directory.name) / "bridge-config.json"
        config.write_text('{"fullAvatarIds":["custom:bailong-full"]}', encoding="utf-8")
        patcher = patch.object(bridge_policy, "CONFIG_FILE", config)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def valid_state():
        return {"running": True, "overlayOpen": True, "pid": 123,
                "selectionKnown": True, "selectedAvatarId": "custom:bailong-full",
                "nativePetVisible": True, "overlayHwnd": 456}

    def test_state_freshness_and_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            now = time.time()
            self.assertIsNone(read_observation(path, now))
            path.write_text(json.dumps(self.valid_state()), encoding="utf-8")
            os.utime(path, (now, now))
            self.assertEqual(read_observation(path, now), OPEN)
            self.assertIsNone(read_observation(path, now + 15.01))
            self.assertIsNone(read_observation(path, now, now + 1))
            self.assertIsNone(read_observation(path, now - 3))
            for data in ({"running": "true", "overlayOpen": True, "pid": 123},
                         {"running": True, "overlayOpen": True, "pid": True},
                         {"running": True, "overlayOpen": True, "pid": None}, []):
                path.write_text(json.dumps(data), encoding="utf-8")
                self.assertIsNone(read_observation(path, time.time()))
            path.write_text("{truncated", encoding="utf-8")
            self.assertIsNone(read_observation(path, time.time()))

    def test_selection_is_evaluated_by_shared_policy(self):
        path = Path(self.directory.name) / "state.json"
        data = self.valid_state()
        for changes, expected in (({"overlayOpen": False}, False),
                                  ({"nativePetVisible": False}, False),
                                  ({"selectedAvatarId": "codex"}, False),
                                  ({"selectionKnown": False}, False)):
            path.write_text(json.dumps(dict(data, **changes)), encoding="utf-8")
            self.assertEqual(read_observation(path, time.time()).visible, expected)

    def test_launch_arguments_use_exact_observer_contract(self):
        base = Path(r"D:\Folder with spaces\bridge")
        command = observer_command(base)
        self.assertTrue(command[0].lower().endswith(r"system32\windowspowershell\v1.0\powershell.exe"))
        self.assertEqual(command[1:5], ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"])
        self.assertEqual(command[5:], [str(base / "codex_observer.ps1"), "-StatePath",
                                     str(base / "codex-state.json"), "-StopFile", str(base / "observer.stop"),
                                     "-PythonPath", str(python_path())])
        self.assertEqual(pythonw_path().name, "pythonw.exe")
        self.assertTrue(pythonw_path().is_file())


class FakeNativeProcess:
    pid = 987

    def __init__(self):
        self.result = None
        self.stdout = io.BytesIO()

    def poll(self):
        return self.result

    def finish(self, acted=False, reason="no-visible-show-control"):
        self.stdout = io.BytesIO(json.dumps({"acted": acted, "method": "fixture",
                                           "reason": reason, "globalOpenMayRemain": False}).encode())
        self.result = 0


class NativeCommandTests(unittest.TestCase):
    def test_snapshot_guards_and_action_validation(self):
        for action in ("Hide", "Show"):
            command = native_control_command(Path("fixture"), action, OPEN)
            self.assertEqual(command[command.index("-ExpectedSelectedAvatarId") + 1], "custom:bailong-full")
            self.assertEqual(command[command.index("-ExpectedNativePetVisible") + 1], "true")
            self.assertEqual(command[command.index("-OverlayHwnd") + 1], "456")
        for action, observation in (("Hide", CLOSED),
                                    ("Hide", replace(OPEN, selection_known=False)),
                                    ("Show", replace(OPEN, running=False))):
            with self.assertRaises(ValueError):
                native_control_command(Path("fixture"), action, observation)

    def test_hidden_native_window_hides_full_companion(self):
        self.assertTrue(OPEN.visible)
        self.assertFalse(FULL_HIDDEN.visible)


@unittest.skipUnless(os.name == "nt", "Windows integration")
class WindowsIntegrationTests(unittest.TestCase):
    @patch('bridge_supervisor.MUTEX_NAME', r'Local\BailongBridgeTest_' + str(os.getpid()))
    def test_singleton_mutex_rejects_duplicate_and_can_reacquire(self):
        first, second, third = WindowsMutex(), WindowsMutex(), WindowsMutex()
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.close()
            self.assertTrue(third.acquire())
        finally:
            first.close()
            second.close()
            third.close()

    def test_hidden_pythonw_child_exits_and_can_report_completion(self):
        self.assertTrue(process_alive(os.getpid()))
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            child = launch_hidden([str(pythonw_path()), "-c",
                                   "from pathlib import Path; Path('completed.txt').write_text('ok')"], base)
            self.assertEqual(child.wait(timeout=10), 0)
            self.assertEqual((base / "completed.txt").read_text(), "ok")
            self.assertFalse(process_alive(child.pid))

    def test_shutdown_timeout_reclaims_only_its_real_disposable_child(self):
        with tempfile.TemporaryDirectory(prefix="bailong-owned-shutdown-") as directory:
            child = launch_hidden([str(pythonw_path()), "-c", "import time; time.sleep(30)"],
                                  Path(directory))
            try:
                started = time.monotonic()
                with patch.object(supervisor, "WORKER_SHUTDOWN_SECONDS", 0.2):
                    remaining = supervisor.shutdown_owned_children(child, None, None, Mock())
                self.assertEqual(remaining, [])
                self.assertIsNotNone(child.poll())
                self.assertLess(time.monotonic() - started, 3)
                self.assertTrue(process_alive(os.getpid()))
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
