"""Opt-in real Windows Job tests; all processes are inert temporary fixtures.

Run with BAILONG_TEST_WINDOWS_JOBS=1. No installed pet or Codex process is used.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback
import unittest

import detached_launcher as launcher


def wait_json(path, timeout=35):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            time.sleep(.05)
    raise RuntimeError(f"Fixture timed out waiting for {path.name}")


def write(path, value):
    pending = path.with_suffix(".pending")
    pending.write_text(json.dumps(value), encoding="utf-8")
    pending.replace(path)


def job_api():
    api = launcher.WindowsProcessApi()
    api.api.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    api.api.CreateJobObjectW.restype = wintypes.HANDLE
    api.api.SetInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    api.api.SetInformationJobObject.restype = wintypes.BOOL
    api.api.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    api.api.AssignProcessToJobObject.restype = wintypes.BOOL
    api.api.WaitForSingleObject.restype = wintypes.DWORD
    return api


def make_job(api, flags):
    handle = api.api.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = launcher.JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    limits.BasicLimitInformation.LimitFlags = flags
    if not api.api.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        api.close(handle)
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def assign(api, job, process):
    if not api.api.AssignProcessToJobObject(job, process):
        raise ctypes.WinError(ctypes.get_last_error())


def fixture(mode, folder):
    script = str(Path(__file__).resolve())
    if mode == "survivor":
        api = launcher.WindowsProcessApi()
        write(folder / "survivor-ready.json", {
            "requiresDetach": launcher.process_requires_detach(),
            "inJob": api.in_job(), "pid": os.getpid()})
        wait_json(folder / "closed.json")
        write(folder / "survivor-done.json", {"survived": True})
        return
    if mode == "nested-worker":
        api = launcher.WindowsProcessApi()
        flags = api.current_job_limits()
        result = launcher.launch_detached(sys.executable,
            [script, "--fixture", "survivor", str(folder)], folder)
        write(folder / "worker-result.json", {"initialFlags": flags, "launch": result})
        time.sleep(40)  # terminated by closing only the synthetic outer Job
        return
    api = job_api()
    if mode == "nested-controller":
        jobs, child = [], None
        try:
            jobs.append(make_job(api, launcher.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE))
            jobs.append(make_job(api, 0x800))  # benign inner Job allows breakaway
            command = subprocess.list2cmdline([sys.executable, script, "--fixture", "nested-worker", str(folder)])
            child = api.create(sys.executable, command, str(folder))
            assign(api, jobs[0], child.hProcess)
            assign(api, jobs[1], child.hProcess)
            api.resume(child.hThread)
            worker = wait_json(folder / "worker-result.json")
            ready = wait_json(folder / "survivor-ready.json")
            for job in reversed(jobs):
                api.close(job)
            jobs.clear()
            worker_stopped = api.api.WaitForSingleObject(child.hProcess, 5000) == 0
            write(folder / "closed.json", {})
            survived = wait_json(folder / "survivor-done.json")
            write(folder / "result.json", {"worker": worker, "ready": ready,
                "workerStopped": worker_stopped, **survived})
        finally:
            for job in reversed(jobs):
                api.close(job)
            if child:
                api.close(child.hThread)
                api.close(child.hProcess)
        return
    if mode == "benign-controller":
        job, child = None, None
        try:
            job = make_job(api, 0)
            assign(api, job, api.api.GetCurrentProcess())
            report = folder / "verified-report.json"
            report.write_text("", encoding="utf-8")
            child = subprocess.Popen([sys.executable, str(Path(launcher.__file__).resolve()),
                "--verified-child", "--report", str(report),
                "--session", str(launcher.current_session_id()),
                "--profile", launcher.profile_fingerprint(), "--", script,
                "--fixture", "survivor", str(folder)], cwd=folder,
                creationflags=launcher.CREATE_NO_WINDOW)
            verification = wait_json(report)
            ready = wait_json(folder / "survivor-ready.json")
            api.close(job)
            job = None
            write(folder / "closed.json", {})
            survived = wait_json(folder / "survivor-done.json")
            child.wait(timeout=5)
            write(folder / "result.json", {"verification": verification,
                "ready": ready, "childExit": child.returncode, **survived})
        finally:
            if job:
                api.close(job)
            if child and child.poll() is None:
                # This handle belongs only to our own synthetic wrapper.
                child.terminate()
                child.wait(timeout=5)
        return
    raise ValueError("Unknown fixture")


@unittest.skipUnless(os.name == "nt" and os.environ.get("BAILONG_TEST_WINDOWS_JOBS") == "1",
                     "set BAILONG_TEST_WINDOWS_JOBS=1 to run synthetic Windows Job processes")
class WindowsJobTests(unittest.TestCase):
    def run_fixture(self, mode):
        with tempfile.TemporaryDirectory(prefix="BailongJobFixture-") as name:
            folder = Path(name)
            launched = launcher._launch_wmi(sys.executable,
                [str(Path(__file__).resolve()), "--fixture", mode, str(folder)], folder)
            api = launcher.WindowsProcessApi()
            api.api.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            api.api.OpenProcess.restype = wintypes.HANDLE
            api.api.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            api.api.WaitForSingleObject.restype = wintypes.DWORD
            handles = []
            try:
                handle = api.api.OpenProcess(0x100000, False, launched['pid'])
                if handle:
                    handles.append(handle)
                ready = wait_json(folder / 'survivor-ready.json', timeout=55)
                handle = api.api.OpenProcess(0x100000, False, ready['pid'])
                if handle:
                    handles.append(handle)
                result = wait_json(folder / "result.json", timeout=55)
                self.assertNotIn("error", result)
                # A result file precedes controller/survivor process exit.
                # Wait on these creation-bound fixture handles before Temp
                # cleanup so Windows has released their working directory.
                for handle in handles:
                    self.assertEqual(api.api.WaitForSingleObject(handle, 5000), 0)
                return result
            finally:
                for handle in handles:
                    api.close(handle)

    def test_non_killing_service_job_is_accepted_and_survives_close(self):
        result = self.run_fixture("benign-controller")
        self.assertTrue(result["verification"]["detached"])
        self.assertTrue(result["verification"]["inJob"])
        self.assertEqual(result["verification"]["jobLimitFlags"], 0)
        self.assertFalse(result["ready"]["requiresDetach"])
        self.assertTrue(result["survived"])
        self.assertEqual(result["childExit"], 0)

    def test_benign_inner_job_does_not_hide_killing_ancestor(self):
        result = self.run_fixture("nested-controller")
        self.assertEqual(result["worker"]["initialFlags"], 0x800)
        self.assertEqual(result["worker"]["launch"]["method"], "windows-local-wmi")
        self.assertTrue(result["workerStopped"])
        self.assertFalse(result["ready"]["requiresDetach"])
        self.assertTrue(result["survived"])


if __name__ == "__main__":
    if sys.argv[1:2] == ["--fixture"]:
        try:
            fixture(sys.argv[2], Path(sys.argv[3]))
        except Exception:
            write(Path(sys.argv[3]) / "result.json", {"error": traceback.format_exc()})
            raise
    else:
        unittest.main()
