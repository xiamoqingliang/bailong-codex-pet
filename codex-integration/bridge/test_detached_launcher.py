"""Independent launcher contracts; synthetic processes only."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

import detached_launcher as launcher


class Api:
    def __init__(self, child_in_job=False, fail_create=False,
                 current_in_job=True, flags=launcher.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE):
        self.child_in_job, self.fail_create = child_in_job, fail_create
        self.current_in_job, self.flags = current_in_job, flags
        self.resumed, self.terminated, self.closed = [], [], []

    def in_job(self, handle=None):
        return self.current_in_job if handle is None else self.child_in_job

    def current_job_limits(self): return self.flags

    def create(self, executable, command, cwd):
        self.command = command
        if self.fail_create:
            raise launcher.DetachedLaunchError("fixture denied")
        return SimpleNamespace(hProcess=11, hThread=12, dwProcessId=123)

    def resume(self, handle): self.resumed.append(handle)
    def terminate_suspended(self, handle): self.terminated.append(handle)
    def close(self, handle): self.closed.append(handle)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="BailongLauncherTest-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.exe = self.base / "pythonw.exe"
        self.script = self.base / "helper 空格.py"
        self.exe.write_bytes(b"fixture")
        self.script.write_text("", encoding="utf-8")
        provenance = patch.object(launcher, "_VERIFIED_INDEPENDENT_PID", None)
        provenance.start()
        self.addCleanup(provenance.stop)

    def test_direct_child_is_verified_before_resume(self):
        api, fallback = Api(), Mock()
        result = launcher.launch_detached(self.exe, [str(self.script), "a & b"], self.base, _api=api, _wmi=fallback)
        self.assertTrue(result["detached"])
        self.assertEqual(api.resumed, [12])
        self.assertEqual(api.terminated, [])
        self.assertEqual(api.closed, [12, 11])
        fallback.assert_not_called()

    def test_nested_job_child_is_discarded_before_wmi_fallback(self):
        # An immediate non-killing Job cannot prove there is no killing parent.
        api = Api(child_in_job=True, flags=0)
        fallback = Mock(return_value={"pid": 456, "detached": True})
        result = launcher.launch_detached(self.exe, [str(self.script)], self.base, _api=api, _wmi=fallback)
        self.assertEqual(api.resumed, [])
        self.assertEqual(api.terminated, [11])
        self.assertEqual(api.closed, [12, 11])
        self.assertEqual(result["pid"], 456)
        fallback.assert_called_once()

    def test_create_denied_uses_external_provider(self):
        fallback = Mock(return_value={"pid": 456})
        launcher.launch_detached(self.exe, [str(self.script)], self.base, _api=Api(fail_create=True), _wmi=fallback)
        fallback.assert_called_once()

    def test_unsafe_paths_or_arguments_never_launch(self):
        fallback = Mock()
        for args in (["relative.py"], [str(self.script), "bad\narg"], [], "string"):
            with self.subTest(args=args), self.assertRaises((ValueError, TypeError)):
                launcher.launch_detached(self.exe, args, self.base, _api=Api(), _wmi=fallback)
        fallback.assert_not_called()

    def test_guarded_wmi_child_checks_job_session_and_profile_before_script(self):
        report = self.base / "report.json"
        report.write_text("")
        args = ["--report", str(report), "--session", "1", "--profile", "expected", "--", str(self.script)]
        with patch.object(launcher, "WindowsProcessApi", return_value=Api()), \
             patch.object(launcher, "current_session_id", return_value=1), \
             patch.object(launcher, "profile_fingerprint", return_value="expected"), \
             patch.object(launcher.runpy, "run_path") as run:
            self.assertEqual(launcher.verified_child(args), 1)
            run.assert_not_called()
        self.assertFalse(json.loads(report.read_text())["detached"])

    def test_guarded_wmi_child_preserves_argv_when_verified(self):
        report = self.base / "report.json"
        report.write_text("")
        args = ["--report", str(report), "--session", "1", "--profile", "expected", "--", str(self.script), "quoted & value"]
        with patch.object(launcher, "WindowsProcessApi", return_value=Api(current_in_job=False)), \
             patch.object(launcher, "current_session_id", return_value=1), \
             patch.object(launcher, "profile_fingerprint", return_value="expected"), \
             patch.object(launcher.runpy, "run_path") as run, patch.object(sys, "argv", ["launcher.py"]):
            self.assertEqual(launcher.verified_child(args), 0)
            self.assertEqual(sys.argv, [str(self.script), "quoted & value"])
            run.assert_called_once_with(str(self.script), run_name="__main__")

    def test_verified_service_child_accepts_non_killing_job_without_relaunch(self):
        report = self.base / "report.json"
        report.write_text("")
        args = ["--report", str(report), "--session", "1", "--profile", "expected", "--", str(self.script)]
        api = Api(flags=0x400)
        with patch.object(launcher, "WindowsProcessApi", return_value=api), \
             patch.object(launcher, "current_session_id", return_value=1), \
             patch.object(launcher, "profile_fingerprint", return_value="expected"), \
             patch.object(launcher.runpy, "run_path") as run, patch.object(sys, "argv", ["launcher.py"]):
            self.assertTrue(launcher.process_requires_detach())
            self.assertEqual(launcher.verified_child(args), 0)
            self.assertFalse(launcher.process_requires_detach())
            run.assert_called_once()
        self.assertTrue(json.loads(report.read_text())["inJob"])

    def test_process_local_verification_does_not_transfer_to_another_pid(self):
        with patch.object(launcher, "_VERIFIED_INDEPENDENT_PID", os.getpid() + 1):
            self.assertTrue(launcher.process_requires_detach(_api=Api(flags=0)))

    def test_verified_service_child_still_rejects_new_killing_limit(self):
        with patch.object(launcher, "_VERIFIED_INDEPENDENT_PID", os.getpid()):
            self.assertTrue(launcher.process_requires_detach(_api=Api()))

    def test_failed_job_query_reports_reason_and_never_runs_script(self):
        report = self.base / "report.json"
        report.write_text("")
        args = ["--report", str(report), "--session", "1", "--profile", "expected", "--", str(self.script)]
        api = Api()
        api.current_job_limits = Mock(side_effect=launcher.DetachedLaunchError("fixture job query failed"))
        with patch.object(launcher, "WindowsProcessApi", return_value=api), \
             patch.object(launcher.runpy, "run_path") as run:
            self.assertEqual(launcher.verified_child(args), 1)
            run.assert_not_called()
        self.assertEqual(json.loads(report.read_text())["reason"], "fixture job query failed")

    def test_service_child_checks_session_and_profile_even_for_benign_job(self):
        for session, profile in ((2, "expected"), (1, "wrong")):
            with self.subTest(session=session, profile=profile):
                report = self.base / "report.json"
                report.write_text("")
                args = ["--report", str(report), "--session", "1", "--profile", "expected", "--", str(self.script)]
                with patch.object(launcher, "WindowsProcessApi", return_value=Api(flags=0)), \
                     patch.object(launcher, "current_session_id", return_value=session), \
                     patch.object(launcher, "profile_fingerprint", return_value=profile), \
                     patch.object(launcher.runpy, "run_path") as run:
                    self.assertEqual(launcher.verified_child(args), 1)
                    run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
