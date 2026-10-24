"""Launch a hidden Python helper outside its caller's Windows job lifetime.

The new process stays suspended until IsProcessInJob confirms that it is not
owned by any job. Failure is explicit; this module never falls back to an
ordinary child that would be killed when Codex exits. No shell or service is
used, and no environment variables or command arguments are logged.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import runpy


CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000
CREATE_SUSPENDED = 0x00000004
CREATION_FLAGS = CREATE_BREAKAWAY_FROM_JOB | CREATE_NO_WINDOW | CREATE_SUSPENDED


class DetachedLaunchError(RuntimeError):
    pass


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


class WindowsProcessApi:
    def __init__(self):
        if os.name != "nt":
            raise DetachedLaunchError("Detached companion launch requires Windows.")
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.GetCurrentProcess.restype = wintypes.HANDLE
        self.api.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE,
                                            ctypes.POINTER(wintypes.BOOL))
        self.api.IsProcessInJob.restype = wintypes.BOOL
        self.api.CreateProcessW.argtypes = (
            wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
            wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
            ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION))
        self.api.CreateProcessW.restype = wintypes.BOOL
        self.api.ResumeThread.argtypes = (wintypes.HANDLE,)
        self.api.ResumeThread.restype = wintypes.DWORD
        self.api.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self.api.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self.api.CloseHandle.argtypes = (wintypes.HANDLE,)

    def in_job(self, handle=None):
        result = wintypes.BOOL()
        handle = self.api.GetCurrentProcess() if handle is None else handle
        if not self.api.IsProcessInJob(handle, None, ctypes.byref(result)):
            raise DetachedLaunchError(f"Could not verify process job ownership (Windows error {ctypes.get_last_error()}).")
        return bool(result.value)

    def create(self, executable, command, cwd):
        startup = STARTUPINFOW()
        startup.cb = ctypes.sizeof(startup)
        startup.dwFlags = 0x00000001  # STARTF_USESHOWWINDOW
        startup.wShowWindow = 0  # SW_HIDE
        process = PROCESS_INFORMATION()
        mutable_command = ctypes.create_unicode_buffer(command)
        if not self.api.CreateProcessW(executable, mutable_command, None, None, False,
                CREATION_FLAGS, None, cwd, ctypes.byref(startup), ctypes.byref(process)):
            raise DetachedLaunchError(f"Independent background launch failed (Windows error {ctypes.get_last_error()}); no dependent fallback was started.")
        return process

    def resume(self, handle):
        if self.api.ResumeThread(handle) != 1:
            raise DetachedLaunchError("The independent helper could not be resumed safely.")

    def terminate_suspended(self, handle):
        # Only the not-yet-started child returned by our own CreateProcessW.
        self.api.TerminateProcess(handle, 1)
        self.api.WaitForSingleObject(handle, 1000)

    def close(self, handle):
        if handle:
            self.api.CloseHandle(handle)


def process_in_job(*, _api=None):
    """Check the current process; no process is created or modified."""
    return (_api or WindowsProcessApi()).in_job()


def _absolute_existing(path, *, directory=False):
    value = os.fspath(path)
    if not isinstance(value, str) or any(char in value for char in ("\0", "\r", "\n")):
        raise ValueError("Launcher paths must be ordinary absolute filesystem paths.")
    candidate = Path(value)
    if not candidate.is_absolute():
        raise ValueError("Launcher paths must be absolute.")
    resolved = candidate.resolve(strict=True)
    if not (resolved.is_dir() if directory else resolved.is_file()):
        raise ValueError("The launcher path has the wrong file type.")
    return resolved


def _launch_direct(executable, args, cwd, *, _api=None):
    """Start one explicit Python script, returning only safe process metadata.

    ``args[0]`` must name the script's existing absolute path. Arguments are
    quoted with Windows argv rules, not interpreted by cmd or PowerShell.
    """
    executable = _absolute_existing(executable)
    cwd = _absolute_existing(cwd, directory=True)
    if executable.name.lower() not in ("python.exe", "pythonw.exe"):
        raise ValueError("Only an explicit Python interpreter can launch this helper.")
    if not isinstance(args, (list, tuple)) or not args or not all(isinstance(value, str) for value in args):
        raise ValueError("The helper arguments must be a nonempty string sequence.")
    if any(any(char in value for char in ("\0", "\r", "\n")) for value in args):
        raise ValueError("The helper arguments contain invalid control characters.")
    script = _absolute_existing(args[0])
    if script.suffix.lower() != ".py":
        raise ValueError("The helper must be an explicit Python script.")
    argv = [str(executable), str(script), *args[1:]]
    command = subprocess.list2cmdline(argv)
    if len(command.encode("utf-16-le")) // 2 >= 32767:
        raise ValueError("The helper command is too long.")
    api = _api or WindowsProcessApi()
    parent_in_job = api.in_job()
    process = api.create(str(executable), command, str(cwd))
    resumed = False
    try:
        if api.in_job(process.hProcess):
            raise DetachedLaunchError("The new helper is still owned by a Windows job; it was not started.")
        api.resume(process.hThread)
        resumed = True
        return {"pid": int(process.dwProcessId), "detached": True,
                "parentInJob": parent_in_job, "method": "windows-breakaway"}
    finally:
        if not resumed:
            api.terminate_suspended(process.hProcess)
        api.close(process.hThread)
        api.close(process.hProcess)


def current_session_id():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.ProcessIdToSessionId.argtypes = (wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    value = wintypes.DWORD()
    if not api.ProcessIdToSessionId(os.getpid(), ctypes.byref(value)):
        raise DetachedLaunchError("Could not verify the interactive Windows session.")
    return value.value


def profile_fingerprint():
    values = [os.path.normcase(os.environ.get(key, "")) for key in ("USERPROFILE", "LOCALAPPDATA", "APPDATA")]
    return hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()


_WMI_CREATE = r'''
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
$startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{
    ShowWindow = [uint16]0
    CreateFlags = [uint32]150994944
}
$created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = [string]$payload.command
    CurrentDirectory = [string]$payload.cwd
    ProcessStartupInformation = $startup
}
@{ pid = [int]$created.ProcessId; result = [int]$created.ReturnValue } | ConvertTo-Json -Compress
'''


def _launch_wmi(executable, args, cwd):
    # WMI's local provider is outside the caller's entire nested Job tree.
    # The wrapper checks ownership and interactive-user identity before it
    # executes the actual companion or installer migration script.
    descriptor, name = tempfile.mkstemp(prefix="bailong-launch-", suffix=".json")
    os.close(descriptor)
    report_path = Path(name)
    session_id = current_session_id()
    fingerprint = profile_fingerprint()
    wrapper = [str(executable), str(Path(__file__).resolve()), "--verified-child",
               "--report", str(report_path), "--session", str(session_id),
               "--profile", fingerprint, "--", *args]
    command = subprocess.list2cmdline(wrapper)
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        completed = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", _WMI_CREATE],
            input=json.dumps({"command": command, "cwd": str(cwd)}, ensure_ascii=True),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            timeout=25, creationflags=CREATE_NO_WINDOW)
        if completed.returncode:
            raise DetachedLaunchError("Windows could not start the independent companion through its local process service.")
        result = json.loads(completed.stdout.lstrip("\ufeff"))
        if result.get("result") != 0 or type(result.get("pid")) is not int or result["pid"] <= 0:
            raise DetachedLaunchError("The local process service rejected independent companion startup.")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
                if report.get("pid") == result["pid"]:
                    if report.get("detached") is not True or report.get("sameSession") is not True or report.get("sameProfile") is not True:
                        raise DetachedLaunchError("The background process failed its independent-user check and did not run the companion.")
                    return {"pid": result["pid"], "detached": True, "method": "windows-local-wmi",
                            "sameSession": True, "sameProfile": True}
            except (OSError, ValueError):
                pass
            time.sleep(.05)
        raise DetachedLaunchError("Independent startup was not confirmed; check the bridge status before retrying.")
    except (subprocess.TimeoutExpired, ValueError) as error:
        raise DetachedLaunchError("Independent startup could not be confirmed; no dependent fallback was used.") from error
    finally:
        report_path.unlink(missing_ok=True)


def launch_detached(executable, args, cwd, *, _api=None, _wmi=None):
    """Start outside all parent Jobs, including Codex's nested Job hierarchy."""
    executable = _absolute_existing(executable)
    cwd = _absolute_existing(cwd, directory=True)
    # Validate the complete command before invoking either creation mechanism.
    if executable.name.lower() not in ("python.exe", "pythonw.exe"):
        raise ValueError("Only an explicit Python interpreter can launch this helper.")
    if not isinstance(args, (list, tuple)) or not args or not all(isinstance(value, str) for value in args):
        raise ValueError("The helper arguments must be a nonempty string sequence.")
    if any(any(char in value for char in ("\0", "\r", "\n")) for value in args):
        raise ValueError("The helper arguments contain invalid control characters.")
    script = _absolute_existing(args[0])
    if script.suffix.lower() != ".py":
        raise ValueError("The helper must be an explicit Python script.")
    args = [str(script), *args[1:]]
    try:
        return _launch_direct(executable, args, cwd, _api=_api)
    except DetachedLaunchError:
        return (_wmi or _launch_wmi)(executable, args, cwd)


def verified_child(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--session", type=int, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("script_args", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    arguments = options.script_args[1:] if options.script_args[:1] == ["--"] else options.script_args
    report = {"pid": os.getpid(), "detached": not process_in_job(),
              "sameSession": current_session_id() == options.session,
              "sameProfile": profile_fingerprint() == options.profile}
    if not options.report.is_file():
        return 1
    options.report.write_text(json.dumps(report), encoding="utf-8")
    if not all(report[key] for key in ("detached", "sameSession", "sameProfile")):
        return 1
    if not arguments or not Path(arguments[0]).is_absolute():
        return 1
    sys.argv = arguments
    runpy.run_path(arguments[0], run_name="__main__")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--verified-child"]:
        return verified_child(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("script_args", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    arguments = options.script_args[1:] if options.script_args[:1] == ["--"] else options.script_args
    try:
        print(json.dumps(launch_detached(options.executable, arguments, options.cwd), ensure_ascii=True))
        return 0
    except (OSError, ValueError, DetachedLaunchError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
