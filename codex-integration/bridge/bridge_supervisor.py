"""Own the observer/animation worker without changing or terminating Codex.

Run with pythonw.exe for the background bridge; --status and --stop work with
python.exe. Only the three stop files below belong to this supervisor.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import time

from bridge_policy import full_mode_selected
from native_visibility import (BoundedStateFile, NativeMiniController, request_visibility,
                               publish_observer_generation, valid_observer_state)


BASE = Path(__file__).resolve().parent
MUTEX_NAME = r"Local\BailongCodexBridgeSupervisorV1"
STOP_NAMES = ("supervisor.stop", "pet.stop", "observer.stop")
STATE_MAX_AGE = 15.0
POLL_SECONDS = 0.1
WORKER_RETRY_SECONDS = 30.0
MAX_WORKER_RETRIES = 3
_OBSERVATION_READER = BoundedStateFile(max_age=STATE_MAX_AGE, validator=valid_observer_state)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Observation:
    running: bool
    overlay_open: bool
    pid: int | None
    full_selected: bool = False
    selection_known: bool = False
    selected_avatar_id: str | None = None
    native_pet_visible: bool | None = None
    overlay_hwnd: int | None = None
    requested_open: bool | None = None
    verified_at: float | None = field(default=None, compare=False)
    app_started_at: float | None = None

    @property
    def visible(self) -> bool:
        return self.running and self.full_selected and self.overlay_open


def read_observation(path: Path, now: float, minimum_mtime: float = 0.0,
                     *, generation=None, monotonic_now=None) -> Observation | None:
    """An unreadable, incomplete or old state cannot request a pet launch."""
    try:
        data = _OBSERVATION_READER.read(path, now=now, monotonic_now=monotonic_now,
                                        minimum_mtime=minimum_mtime, generation=generation)
        if data is None:
            return None
        pid = data.get("pid")
        if data["running"] and (type(pid) is not int or pid <= 0):
            return None
        selected = data.get("selectedAvatarId")
        native_visible = data.get("nativePetVisible")
        selection_known = (data.get("selectionKnown") is True
                           and (selected is None or isinstance(selected, str))
                           and type(native_visible) is bool)
        hwnd = data.get("overlayHwnd")
        hwnd = hwnd if type(hwnd) is int and hwnd > 0 else None
        return Observation(data["running"], data["overlayOpen"], pid if data["running"] else None,
                           full_mode_selected(data), selection_known,
                           selected if isinstance(selected, str) else None,
                           native_visible if type(native_visible) is bool else None, hwnd,
                           data.get('requestedOpen') if type(data.get('requestedOpen')) is bool else None,
                           _OBSERVATION_READER.verified_at,
                           data.get('appStartedAt') if type(data.get('appStartedAt')) in (int, float) else None)
    except (OSError, ValueError, TypeError):
        return None


class WorkerPolicy:
    """Separate lifecycle decisions from Windows process management for testing."""

    def __init__(self) -> None:
        self.last_open: bool | None = None
        self.last_pid: int | None = None
        self.last_started_at: float | None = None
        self.host_was_absent = False
        self.last_full_selected: bool | None = None
        self.last_native_open: bool | None = None
        self.generation = 0
        self.attempted = False
        self.manual_suppressed = False
        self.crash_retries = 0
        self.retry_at: float | None = None
        self.visible = False

    def observe(self, observation: Observation | None) -> None:
        self.visible = observation is not None and observation.visible
        if observation is None:
            # Staleness is not a user hide/show action and must not undo a quit.
            return
        if not observation.running:
            self.host_was_absent = True
            return
        started = observation.app_started_at
        new_host = (observation.pid != self.last_pid or self.host_was_absent or
                    started is not None and self.last_started_at is not None
                    and started != self.last_started_at)
        if new_host:
            self.last_full_selected = self.last_native_open = self.last_open = None
        # A failed selection projection is uncertainty, not a user changing
        # pets or clicking Show. Never let its recovery undo a manual exit.
        selected_again = (observation.selection_known and observation.full_selected
                          and self.last_full_selected is False)
        user_show = (observation.selection_known and observation.visible
                     and self.last_native_open is False)
        if new_host or selected_again or user_show:
            self.generation += 1
            self.attempted = False
            self.manual_suppressed = False
            self.crash_retries = 0
            self.retry_at = None
        self.last_pid = observation.pid
        if new_host or started is not None:
            self.last_started_at = started
        self.host_was_absent = False
        if observation.selection_known:
            self.last_open = observation.visible
            self.last_native_open = observation.overlay_open
            self.last_full_selected = observation.full_selected

    def should_launch(self, now: float) -> bool:
        return self.visible and not self.manual_suppressed and (
            not self.attempted or (self.retry_at is not None and now >= self.retry_at)
        )

    def started(self) -> int:
        self.attempted = True
        self.retry_at = None
        return self.generation

    def exited(self, returncode: int, now: float, worker_generation: int,
               *, exit_reason: str = "user", exit_host_pid: int | None = None) -> None:
        if worker_generation != self.generation:
            return
        if (returncode == 0 and exit_host_pid is not None and self.last_pid is not None
                and exit_host_pid != self.last_pid):
            # An old widget may finish its asynchronous close after the new
            # Codex process appeared and the supervisor adopted its generation.
            # That retirement must not suppress or hide the new host's pet.
            self.attempted = False
            self.manual_suppressed = False
            self.retry_at = None
            return
        if returncode == 0 and exit_reason == "codex-closed":
            self.attempted = False
            self.manual_suppressed = False
            self.retry_at = None
            return
        if returncode == 0 and exit_reason == "supervisor-stop":
            # Shutdown is not a user Quit. Keep this stopped generation idle;
            # a new supervisor or host cycle decides when to launch again.
            self.attempted = True
            self.manual_suppressed = False
            self.retry_at = None
            return
        if returncode == 0:
            self.manual_suppressed = True
            self.retry_at = None
        elif self.crash_retries < MAX_WORKER_RETRIES:
            self.crash_retries += 1
            self.retry_at = now + WORKER_RETRY_SECONDS
        else:
            self.retry_at = None


@dataclass(frozen=True)
class WorkerExit:
    reason: str
    host_pid: int | None = None
    verified: bool = False

    def should_hide(self, observation: Observation | None) -> bool:
        return bool(self.verified and self.reason == "user" and observation
                    and observation.visible and self.host_pid == observation.pid)


def read_worker_exit(path: Path, worker_pid: int, returncode: int,
                     minimum_mtime: float = 0.0, *, closing: bool = False) -> WorkerExit:
    """Read only the just-exited worker's final status, never a stale PID file."""
    fallback = WorkerExit("error" if returncode else "unknown")
    data = BoundedStateFile(max_age=None).read(path, minimum_mtime=minimum_mtime)
    if not data or type(data.get("pid")) is not int or data["pid"] != worker_pid:
        return fallback
    phase = "closing" if closing else ("stopped" if returncode == 0 else "error")
    stamp = data.get("updatedAt")
    if (data.get("phase") != phase or type(stamp) not in (int, float)
            or stamp < minimum_mtime or stamp > time.time() + 2):
        return fallback
    reason = data.get("exitReason")
    if reason not in ("user", "codex-closed", "supervisor-stop", "error"):
        return fallback
    if (returncode == 0) == (reason == "error"):
        return fallback
    host_pid = data.get("exitHostPid")
    if type(host_pid) is not int or host_pid <= 0:
        host_pid = None
    return WorkerExit(reason, host_pid, True)




class WindowsMutex:
    def __init__(self) -> None:
        self.handle = None
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        self.api.CreateMutexW.restype = wintypes.HANDLE
        self.api.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.api.CloseHandle.restype = wintypes.BOOL

    def acquire(self) -> bool:
        self.handle = self.api.CreateMutexW(None, False, MUTEX_NAME)
        error = ctypes.get_last_error()
        if not self.handle:
            raise ctypes.WinError(error)
        if error == 183:  # ERROR_ALREADY_EXISTS
            self.close()
            return False
        return True

    def close(self) -> None:
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def process_alive(pid: object) -> bool:
    if type(pid) is not int or pid <= 0 or os.name != "nt":
        return False
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    api.OpenProcess.restype = wintypes.HANDLE
    api.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    api.GetExitCodeProcess.restype = wintypes.BOOL
    api.CloseHandle.argtypes = (wintypes.HANDLE,)
    api.CloseHandle.restype = wintypes.BOOL
    handle = api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(api.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        api.CloseHandle(handle)


def setup_logger(base: Path) -> logging.Logger:
    logger = logging.getLogger("bailong.bridge.supervisor")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = RotatingFileHandler(base / "supervisor.log", maxBytes=65536, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def write_status(base: Path, status: dict, logger: logging.Logger) -> None:
    temporary = base / (".bridge-status.%d.tmp" % os.getpid())
    try:
        temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for attempt in range(3):
            try:
                os.replace(temporary, base / "bridge-status.json")
                return
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(0.025)
    except OSError as error:
        logger.warning("status write failed: %s", type(error).__name__)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def pythonw_path() -> Path:
    executable = Path(sys.executable).resolve()
    pythonw = executable.with_name("pythonw.exe")
    if not pythonw.is_file():
        raise FileNotFoundError("pythonw.exe must be next to the running interpreter")
    return pythonw


def python_path() -> Path:
    executable = Path(sys.executable).resolve()
    python = executable.with_name("python.exe")
    if not python.is_file():
        raise FileNotFoundError("python.exe must be next to the running interpreter")
    return python


def powershell_path() -> Path:
    return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"


def launch_hidden(command: list[str], base: Path) -> subprocess.Popen:
    return subprocess.Popen(
        command, cwd=str(base), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )


def launch_native_control(command: list[str], base: Path) -> subprocess.Popen:
    return subprocess.Popen(
        command, cwd=str(base), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )


def observer_command(base: Path) -> list[str]:
    return [str(powershell_path()), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(base / "codex_observer.ps1"), "-StatePath", str(base / "codex-state.json"),
            "-StopFile", str(base / "observer.stop"), "-PythonPath", str(python_path())]


def native_control_command(base: Path, action: str, observation: Observation) -> list[str]:
    if (action not in ("Hide", "Show") or not observation.running or not observation.selection_known
            or not observation.pid or type(observation.native_pet_visible) is not bool
            or (action == "Hide" and not observation.full_selected)):
        raise ValueError("native action requires an exact, compatible selection snapshot")
    command = [str(powershell_path()), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
               str(base / "native_mini_control.ps1"), "-Action", action, "-Pid", str(observation.pid),
               "-ExpectedSelectedAvatarId", observation.selected_avatar_id or "",
               "-ExpectedNativePetVisible", "true" if observation.native_pet_visible else "false",
               "-PythonPath", str(python_path()),
               "-ConfigPath", str(Path.home() / ".codex" / "config.toml")]
    if observation.overlay_hwnd:
        command.extend(["-OverlayHwnd", str(observation.overlay_hwnd)])
    return command


def signal_stop(base: Path, name: str) -> None:
    (base / name).write_text(utc_now() + "\n", encoding="utf-8")


def run_supervisor(base: Path = BASE, *, show_on_start=False) -> int:
    uninstall_marker = base.parents[1] / "uninstall.request"
    if uninstall_marker.exists():
        return 0
    mutex = WindowsMutex()
    acquired = mutex.acquire()
    # An explicit re-enable may arrive while the previous owner is still
    # shutting down. Wait for ownership instead of silently losing that start.
    deadline = time.monotonic() + 45.0
    while not acquired and (base / 'supervisor.stop').exists() and time.monotonic() < deadline:
        time.sleep(0.25)
        acquired = mutex.acquire()
    if not acquired:
        return 1 if (base / 'supervisor.stop').exists() else 0
    # Do not erase stop requests while a previous owner is still waiting on
    # its original engine's graceful chat/thread shutdown.
    try:
        previous = json.loads((base / 'bridge-status.json').read_text(encoding='utf-8-sig'))
        pending = previous.get('pendingChildPids', []) if isinstance(previous, dict) else []
    except (OSError, ValueError):
        pending = []
    while any(process_alive(pid) for pid in pending) and time.monotonic() < deadline:
        time.sleep(0.25)
    if any(process_alive(pid) for pid in pending):
        mutex.close()
        return 1
    logger = setup_logger(base)
    observer = worker = None
    worker_generation = 0
    worker_started_at = 0.0
    worker_closing = None
    last_worker_exit = None
    policy = WorkerPolicy()
    old_control = previous.get('nativeControl') if isinstance(previous, dict) else None
    old_host = old_control.get('lastHost') if isinstance(old_control, dict) else None
    if not (isinstance(old_host, list) and len(old_host) == 2 and type(old_host[0]) is int
            and old_host[0] > 0 and (old_host[1] is None or type(old_host[1]) in (int, float))):
        old_pid = previous.get('codexPid') if isinstance(previous, dict) else None
        old_host = [old_pid, None] if type(old_pid) is int and old_pid > 0 else None
    native_control = NativeMiniController(base, logger, launch_native_control, native_control_command,
                                          show_on_start=show_on_start,
                                          previous_host=tuple(old_host) if old_host else None)
    last_observation = None
    observer_failures = 0
    observer_retry_at = 0.0
    observer_started = 0.0
    next_status_at = 0.0
    from detached_launcher import process_in_job
    status = {"supervisorPid": os.getpid(), "workerPid": None, "observerPid": None,
              "startedAt": utc_now(), "phase": "starting", "independentProcess": not process_in_job()}
    minimum_mtime = time.time()
    observer_generation = None
    exit_code = 0
    try:
        if uninstall_marker.exists():
            return 0
        for name in STOP_NAMES:
            (base / name).unlink(missing_ok=True)
        worker_command = [str(pythonw_path()), str(base / "bridge_pet.py")]
        if not (base / "bridge_pet.py").is_file():
            raise FileNotFoundError("bridge_pet.py is missing")
        if not (base / "codex_observer.ps1").is_file():
            raise FileNotFoundError("codex_observer.ps1 is missing")
        logger.info("supervisor started pid=%d", os.getpid())
        observer_generation = publish_observer_generation(base, minimum_mtime)
        native_control.set_generation(observer_generation)
        while not (base / "supervisor.stop").exists() and not uninstall_marker.exists():
            now = time.monotonic()
            if observer is not None:
                result = observer.poll()
                if result is not None:
                    logger.warning("observer exited code=%d", result)
                    observer = None
                    observer_failures += 1
                    observer_retry_at = now + min(30.0, 2.0 ** min(observer_failures, 5))
                    minimum_mtime = time.time()
                    observer_generation = publish_observer_generation(base, minimum_mtime)
                    native_control.set_generation(observer_generation)
                elif now - observer_started >= 60:
                    observer_failures = 0
            if observer is None and now >= observer_retry_at:
                try:
                    observer = launch_hidden(observer_command(base), base)
                    observer_started = now
                    logger.info("observer started pid=%d", observer.pid)
                except OSError as error:
                    observer_failures += 1
                    observer_retry_at = now + min(30.0, 2.0 ** min(observer_failures, 5))
                    logger.warning("observer launch failed: %s", type(error).__name__)

            observation = read_observation(base / "codex-state.json", time.time(), minimum_mtime,
                                           generation=observer_generation, monotonic_now=now)
            if observer is None:
                observation = None
            policy.observe(observation)
            native_control.tick(observation, now, source_failure=(
                "observer-stopped" if observer is None else _OBSERVATION_READER.reason))
            if observation != last_observation:
                logger.info("observation fresh=%s running=%s nativeOverlay=%s fullSelected=%s codexPid=%s", observation is not None,
                            observation.running if observation else None,
                            observation.overlay_open if observation else None,
                            observation.full_selected if observation else None,
                            observation.pid if observation else None)
                last_observation = observation
            if worker is not None:
                result = worker.poll()
                if result is not None:
                    exit_info = read_worker_exit(base / "pet-status.json", worker.pid, result, worker_started_at)
                    logger.info("worker exited code=%d reason=%s verified=%s hostPid=%s",
                                result, exit_info.reason, exit_info.verified, exit_info.host_pid)
                    last_worker_exit = {"code": result, "reason": exit_info.reason,
                                        "verified": exit_info.verified, "hostPid": exit_info.host_pid}
                    worker = None
                    policy.exited(result, now, worker_generation,
                                  exit_reason=exit_info.reason, exit_host_pid=exit_info.host_pid)
                    if result == 0 and worker_closing is None and exit_info.should_hide(observation):
                        request_visibility(base, False, {"pid": observation.pid,
                            "appStartedAt": observation.app_started_at,
                            "selectedAvatarId": observation.selected_avatar_id})
                    worker_closing = None
                else:
                    if worker_closing is None:
                        intent = read_worker_exit(base / "pet-status.json", worker.pid, 0,
                                                  worker_started_at, closing=True)
                        if intent.verified:
                            worker_closing = intent
                            policy.exited(0, now, worker_generation,
                                          exit_reason=intent.reason, exit_host_pid=intent.host_pid)
                            logger.info("worker closing reason=%s hostPid=%s", intent.reason, intent.host_pid)
                            if intent.should_hide(observation):
                                request_visibility(base, False, {"pid": observation.pid,
                                    "appStartedAt": observation.app_started_at,
                                    "selectedAvatarId": observation.selected_avatar_id})
                    if worker_closing is None and observation is not None and observation.visible:
                        # Only an active worker adopts a new Show/host cycle.
                        # A closing worker cannot become that cycle's owner.
                        worker_generation = policy.started()
            if worker is None and policy.should_launch(now):
                worker_generation = policy.started()
                worker_closing = None
                try:
                    worker_started_at = time.time()
                    worker = launch_hidden(worker_command, base)
                    logger.info("worker started pid=%d generation=%d", worker.pid, worker_generation)
                except OSError as error:
                    policy.exited(1, now, worker_generation)
                    logger.warning("worker launch failed: %s", type(error).__name__)
            if now >= next_status_at:
                if observation is None:
                    phase = "waiting-for-observer"
                elif worker is not None:
                    phase = "visible" if observation.visible else "hidden"
                elif policy.manual_suppressed:
                    phase = "manually-closed"
                elif policy.retry_at is not None:
                    phase = "worker-retry-wait"
                elif policy.attempted and policy.crash_retries >= MAX_WORKER_RETRIES:
                    phase = "worker-retry-exhausted"
                else:
                    phase = "waiting-for-codex-pet"
                status.update(updatedAt=utc_now(), phase=phase, observerPid=observer.pid if observer else None,
                              workerPid=worker.pid if worker else None, observerFresh=observation is not None,
                              codexPid=observation.pid if observation else None,
                              overlayOpen=observation.overlay_open if observation else False,
                              nativeOverlayOpen=observation.overlay_open if observation else False,
                              fullSelected=observation.full_selected if observation else False,
                              fullVisible=observation.visible if observation else False,
                              nativeControl=native_control.status(),
                              lastWorkerExit=last_worker_exit,
                              workerCrashRetries=policy.crash_retries)
                write_status(base, status, logger)
                next_status_at = now + 2.0
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        logger.info("supervisor interrupted")
    except Exception as error:
        exit_code = 1
        logger.exception("supervisor failed: %s", type(error).__name__)
    finally:
        logger.info("requesting graceful child shutdown")
        try:
            native_control.close()
        except OSError:
            logger.warning("native Mini region restoration failed")
        for name in ("pet.stop", "observer.stop"):
            try:
                signal_stop(base, name)
            except OSError as error:
                logger.warning("stop write failed %s: %s", name, type(error).__name__)
        deadline = time.monotonic() + 15.0
        # The native UI helper is one-shot; let it exit naturally as well.
        # There is no persistent UI watcher and no native process is killed.
        children = [child for child in (worker, observer, native_control.process) if child is not None]
        while any(child.poll() is None for child in children) and time.monotonic() < deadline:
            time.sleep(0.2)
        remaining = [child.pid for child in children if child.poll() is None]
        if remaining:
            logger.warning("graceful shutdown still pending for child pids=%s; no process killed", remaining)
        native_control._poll(time.monotonic())
        status.update(updatedAt=utc_now(), phase="stopped", stoppedAt=utc_now(), exitCode=exit_code,
                      workerPid=worker.pid if worker is not None and worker.poll() is None else None,
                      observerPid=observer.pid if observer is not None and observer.poll() is None else None,
                      pendingChildPids=remaining, overlayOpen=False, nativeOverlayOpen=False,
                      fullSelected=False, fullVisible=False, nativeControl=native_control.status())
        write_status(base, status, logger)
        logger.info("supervisor stopped")
        mutex.close()
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    options = parser.add_mutually_exclusive_group()
    options.add_argument("--status", action="store_true", help="print last status and whether its supervisor PID is alive")
    options.add_argument("--stop", action="store_true", help="request graceful bridge shutdown")
    parser.add_argument("--show-on-start", action="store_true", help="show the selected full pet once after an explicit installation")
    args = parser.parse_args(argv)
    if args.status:
        try:
            status = json.loads((BASE / "bridge-status.json").read_text(encoding="utf-8-sig"))
            if not isinstance(status, dict):
                raise ValueError("invalid status")
        except (OSError, ValueError):
            status = {"phase": "not-started", "supervisorPid": None}
        try:
            stamp = datetime.fromisoformat(status.get("updatedAt", "")).timestamp()
            status["statusFresh"] = 0 <= time.time() - stamp <= STATE_MAX_AGE
        except (TypeError, ValueError):
            status["statusFresh"] = False
        status["supervisorAlive"] = (status.get("phase") != "stopped" and status["statusFresh"]
                                     and process_alive(status.get("supervisorPid")))
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0
    if args.stop:
        signal_stop(BASE, "supervisor.stop")
        print("Bridge stop requested; the supervisor will stop its children gracefully.")
        return 0
    if (BASE.parents[1] / "uninstall.request").exists():
        return 0
    if os.name != "nt":
        parser.error("the bridge supervisor requires Windows")
    from detached_launcher import process_in_job, launch_detached, DetachedLaunchError
    try:
        if process_in_job():
            arguments = sys.argv[1:] if argv is None else argv
            result = launch_detached(pythonw_path(), [str(BASE / "bridge_supervisor.py"), *arguments], BASE.parents[1])
            if sys.stdout is not None:
                print(json.dumps(result, ensure_ascii=True))
            return 0
    except (OSError, ValueError, DetachedLaunchError) as error:
        (BASE / "launch-error.json").write_text(json.dumps({"phase": "launch-error", "reason": str(error),
            "at": utc_now()}, ensure_ascii=True), encoding="utf-8")
        if sys.stderr is not None:
            print(str(error), file=sys.stderr)
        return 1
    return run_supervisor(show_on_start=args.show_on_start)


if __name__ == "__main__":
    raise SystemExit(main())
