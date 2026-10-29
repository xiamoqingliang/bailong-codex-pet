"""Real Windows runtime/launcher/CMD uninstall cycle in an isolated Temp tree.

Only the installer and uninstaller are production code. The three companion
processes and migration waiter are inert fixtures: no Codex, UI, chat or network
is opened. The global live bridge mutex is never queried by this fixture.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))
import compact_install as setup
from package_commands import uninstall_command


SUPERVISOR = r'''
import json,os,subprocess,sys,time
from pathlib import Path
from detached_launcher import process_requires_detach
BASE=Path(__file__).resolve().parent
ROOT=BASE.parents[1]
MUTEX_NAME="BailongCodexBridgeSupervisorV1"  # fixture marker only; never acquired
for name in ("supervisor.stop","pet.stop","observer.stop"):
    (BASE/name).unlink(missing_ok=True)
counter=ROOT/'fixture-launch-count.txt'
counter.write_text(str(int(counter.read_text())+1 if counter.exists() else 1))
pythonw=ROOT/'runtime/pythonw.exe'
worker=subprocess.Popen([str(pythonw),str(BASE/'bridge_pet.py')],cwd=BASE,
    creationflags=subprocess.CREATE_NO_WINDOW)
powershell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
observer=subprocess.Popen([str(powershell),'-NoProfile','-ExecutionPolicy','Bypass','-File',
    str(BASE/'codex_observer.ps1'),'-StopFile',str(BASE/'observer.stop'),'-ReadyFile',str(BASE/'observer-ready.json')],
    cwd=BASE,creationflags=subprocess.CREATE_NO_WINDOW)
def write(phase):
    status={'supervisorPid':os.getpid(),'workerPid':worker.pid,'observerPid':observer.pid,
        'phase':phase,'independentProcess':not process_requires_detach(),'fixtureOnly':True,'updatedAt':time.time()}
    temporary=BASE/'fixture-status.tmp'
    temporary.write_text(json.dumps(status),encoding='utf-8')
    for attempt in range(5):
        try:os.replace(temporary,BASE/'bridge-status.json');break
        except PermissionError:
            if attempt==4:raise
            time.sleep(.01)
while not (BASE/'supervisor.stop').exists() and not (ROOT/'uninstall.request').exists():
    write('waiting-for-codex-pet')
    time.sleep(.1)
for name in ('pet.stop','observer.stop'):(BASE/name).write_text('fixture shutdown')
worker.wait(timeout=8);observer.wait(timeout=8)
write('stopped')
'''

WORKER = r'''
import json,os,time
from pathlib import Path
BASE=Path(__file__).resolve().parent
while not (BASE/'pet.stop').exists():
    (BASE/'pet-status.json').write_text(json.dumps({'pid':os.getpid(),'phase':'running',
        'chatBusy':False,'fixtureOnly':True,'updatedAt':time.time()}),encoding='utf-8')
    time.sleep(.1)
'''

OBSERVER = r'''
param([string]$StopFile,[string]$ReadyFile)
$ErrorActionPreference='Stop'
[IO.File]::WriteAllText($ReadyFile,(@{pid=$PID;fixtureOnly=$true}|ConvertTo-Json -Compress))
while(-not (Test-Path -LiteralPath $StopFile)){Start-Sleep -Milliseconds 50}
'''

WAITER = r'''
import json,os,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parent
assert sys.argv[1:]==['--finish']
(ROOT/'fixture-waiter.json').write_text(json.dumps({'pid':os.getpid(),'fixtureOnly':True}),encoding='utf-8')
while not (ROOT/'uninstall.request').exists():time.sleep(.1)
'''


def process_arguments(pid):
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    query = "$idToCheck=[int][Console]::In.ReadToEnd(); $p=Get-CimInstance Win32_Process -Filter ('ProcessId='+$idToCheck); [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([string]$p.CommandLine))"
    result = subprocess.run([str(powershell), "-NoProfile", "-Command", query], input=str(pid).encode(),
        capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError("Could not verify fixture process command")
    command = base64.b64decode(result.stdout.strip()).decode("utf-8")
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel = ctypes.WinDLL("kernel32")
    kernel.LocalFree.argtypes = (wintypes.HANDLE,)
    count = ctypes.c_int()
    arguments = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not arguments:
        raise RuntimeError("Could not parse fixture process command")
    try:
        return [arguments[index] for index in range(count.value)]
    finally:
        kernel.LocalFree(ctypes.cast(arguments, wintypes.HANDLE))


class ProcessHandle:
    """A creation-bound handle; cleanup never targets a stale numeric PID."""
    def __init__(self, pid, expected_executable, expected_script):
        self.pid = pid
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        self.api.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        self.api.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self.api.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self.api.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.handle = self.api.OpenProcess(0x100000 | 0x1000 | 1, False, pid)
        if not self.handle:
            raise RuntimeError("Could not bind fixture process handle")
        executable = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(executable))
        if not self.api.QueryFullProcessImageNameW(self.handle, 0, executable, ctypes.byref(length)) or \
                os.path.normcase(executable.value) != os.path.normcase(str(expected_executable)):
            self.close()
            raise RuntimeError("Unexpected fixture process executable")
        try:
            arguments = process_arguments(pid)
            if not self.alive() or os.path.normcase(str(expected_script)) not in {os.path.normcase(value) for value in arguments}:
                raise RuntimeError("Unexpected fixture process script or creation identity")
        except Exception:
            self.close()
            raise

    def alive(self):
        return bool(self.handle) and self.api.WaitForSingleObject(self.handle, 0) == 0x102

    def stop_owned_fixture(self):
        if self.alive():
            self.api.TerminateProcess(self.handle, 77)
            self.api.WaitForSingleObject(self.handle, 3000)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class FixtureRuntime(setup.WindowsRuntime):
    def __init__(self):
        self.launches = []

    def mutex_open(self):
        return False  # Isolate from the real user's existing bridge singleton.

    def launch(self, root, args):
        result = super().launch(root, args)
        self.launches.append(result)
        return result


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def wait(check, message, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError(message)


def hash_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def assert_fixture_path(path, fixture):
    path, fixture = path.resolve(), fixture.resolve()
    temporary = Path(tempfile.gettempdir()).resolve()
    assert fixture.parent == temporary and fixture.name.startswith("BailongFullInstallCycle-")
    assert path == fixture or path.is_relative_to(fixture)
    return path


def prepare(fixture, runtime_source):
    package = fixture / "白龙 完整版"
    data = package / "data"
    data.mkdir(parents=True)
    shutil.copytree(runtime_source, data / "runtime", ignore=shutil.ignore_patterns("__pycache__"))
    for relative in ("BailongPet.exe", "_internal/Qt6Core.dll",
                     "_internal/PySide6/QtCore.cp313-win_amd64.pyd",
                     "_internal/PIL/_imaging.cp313-win_amd64.pyd", "_internal/PIL/_webp.cp313-win_amd64.pyd"):
        path = data / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture, never executed")
    bridge = data / "codex-integration/bridge"
    bridge.mkdir(parents=True)
    for name in setup.REQUIRED_BRIDGE_FILES:
        (bridge / name).write_text("# Fixture only; not executed.\n", encoding="utf-8")
    (bridge / "bridge-config.json").write_text("{}", encoding="utf-8")
    for name, content in (("bridge_supervisor.py", SUPERVISOR), ("bridge_pet.py", WORKER),
                          ("codex_observer.ps1", OBSERVER)):
        (bridge / name).write_text(textwrap.dedent(content), encoding="utf-8-sig" if name.endswith(".ps1") else "utf-8")
    shutil.copy2(ROOT / "codex-integration/bridge/detached_launcher.py", bridge / "detached_launcher.py")
    (data / "install.py").write_text(textwrap.dedent(WAITER), encoding="utf-8")
    shutil.copy2(ROOT / "packaging/uninstall_pet.ps1", data / "uninstall.ps1")
    (package / "一键卸载白龙.cmd").write_bytes(uninstall_command("full"))
    entries = data / "codex-integration/full-entry"
    entries.mkdir()
    for name in ("pet.json", "spritesheet.webp"):
        shutil.copy2(ROOT / "codex-integration/full-entry" / name, entries / name)
    shutil.copy2(ROOT / "codex-integration/bridge/asset_source.py", bridge / "asset_source.py")
    (data / "tools").mkdir(exist_ok=True)
    (data / "tools/ffmpeg.exe").write_bytes(b"fixture decoder")
    assets = data / "outputs/白龙桌宠素材"
    assets.mkdir(parents=True)
    records = [{"file": f"animation-{index}.webm"} for index in range(35)]
    manifest = json.dumps({"animations": records}).encode()
    (assets / "素材清单.json").write_bytes(manifest)
    (assets / "repair-validation.json").write_text(json.dumps({"animations": 35, "rgbExactAfterDecoding": True, "alphaExactAfterDecoding": True,
        "manifestSha256": hashlib.sha256(manifest).hexdigest(), "alphaOnly": True,
        "rgbExactAfterEncoding": True, "originalFilesUnchanged": True}), encoding="utf-8")
    for item in records:
        (assets / item["file"]).write_bytes(b"\x1aE\xdf\xa3" + b"fixture, never decoded")
    return package, data, bridge


def capture_processes(bridge, data, handles):
    def ready():
        status = read_json(bridge / "bridge-status.json")
        observer = read_json(bridge / "observer-ready.json")
        worker = read_json(bridge / "pet-status.json")
        return (status if status.get("fixtureOnly") and observer.get("fixtureOnly") and worker.get("fixtureOnly")
                and status.get("workerPid") == worker.get("pid") and status.get("observerPid") == observer.get("pid") else None)
    status = wait(ready, "Fixture companion did not become ready")
    assert status["independentProcess"] is True
    for key in ("supervisorPid", "workerPid", "observerPid"):
        executable = (Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
                      if key == "observerPid" else data / "runtime/pythonw.exe")
        script = {"supervisorPid": "bridge_supervisor.py", "workerPid": "bridge_pet.py", "observerPid": "codex_observer.ps1"}[key]
        if status[key] not in {handle.pid for handle in handles}:
            handles.append(ProcessHandle(status[key], executable, bridge / script))
    return status


def exclusive_dll_access_is_blocked(path):
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                               wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = api.CreateFileW(str(path), 0xC0000000, 0, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        return ctypes.get_last_error() in (5, 32, 33)
    api.CloseHandle(handle)
    return False


def uninstall(package, home, startup, check=False):
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if check:
        command = [str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                   str(package / "data/uninstall.ps1"), "-DataRoot", str(package / "data"), "-Edition", "full",
                   "-CodexHome", str(home), "-StartupDirectory", str(startup), "-CheckOnly"]
        result = subprocess.run(command, capture_output=True, timeout=30, cwd=package.parent,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        # All paths below are generated inside this test's fixed Temp prefix;
        # no shell metacharacters or user-supplied commands are interpolated.
        payload = {"entry": str(package / "一键卸载白龙.cmd"), "home": str(home), "startup": str(startup)}
        runner = r'''
$ErrorActionPreference='Stop'
$value=[Console]::In.ReadToEnd() | ConvertFrom-Json
$command='"'+$value.entry+'" -CodexHome "'+$value.home+'" -StartupDirectory "'+$value.startup+'"'
'' | & $env:ComSpec /d /c $command
exit $LASTEXITCODE
'''
        result = subprocess.run([str(powershell), "-NoProfile", "-Command", runner],
            input=json.dumps(payload).encode(), capture_output=True, timeout=40, cwd=package,
            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise AssertionError("Isolated uninstaller failed: " + result.stderr.decode("utf-8", errors="replace")[-4000:])
    lines = result.stdout.decode("utf-8", errors="replace").splitlines()
    return next(json.loads(line) for line in lines if line.lstrip().startswith('{"Status":'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-source", type=Path,
        default=ROOT / "runtime")
    parser.add_argument("--output", type=Path, default=ROOT / "analysis/install-uninstall/full-cycle-result.json")
    options = parser.parse_args()
    if os.name != "nt":
        raise RuntimeError("Windows integration test")
    fixture = Path(tempfile.mkdtemp(prefix="BailongFullInstallCycle-"))
    assert_fixture_path(fixture, fixture)
    handles = []
    data = bridge = None
    report = {}
    try:
        package, data, bridge = prepare(fixture, options.runtime_source.resolve())
        # Production WindowsRuntime imports the exact production launcher copy
        # from the fixture, so a WMI wrapper is also rooted in that fixture.
        sys.path.insert(0, str(bridge))
        sys.modules.pop("detached_launcher", None)
        runtime = FixtureRuntime()
        user = fixture / "隔离 用户"
        home = user / ".codex"
        startup = user / "Startup"
        result = setup.install(data, user, startup, check=True, runtime=runtime)
        assert result["checkOnly"] and not user.exists() and not runtime.launches
        report["checkOnlyZeroWrites"] = True
        home.mkdir(parents=True)
        original_config = ("# Keep user settings\r\nmodel = 'fixture-model'\r\n[desktop]\r\n"
            "selected-avatar-id = \"fixture-original\"\r\navatar-overlay-pet-visible = false\r\n"
            "theme = 'dark' # keep this comment\r\n[projects.'D:/fixture-project']\r\ntrust_level = 'trusted'\r\n").encode("utf-8-sig")
        config = home / "config.toml"
        config.write_bytes(original_config)
        auth = home / "auth.json"
        auth.write_text('{"fixture":"preserve"}', encoding="utf-8")
        history = user / "BailongPet/conversations.json"
        history.parent.mkdir()
        history.write_text('{"fixtureHistory":"preserve"}', encoding="utf-8")
        other = home / "pets/other/pet.json"
        other.parent.mkdir(parents=True)
        other.write_text('{"fixtureOtherPet":"preserve"}', encoding="utf-8")
        preserved = {path: hash_file(path) for path in (auth, history, other)}

        result = setup.install(data, user, startup, runtime=runtime)
        assert result["activation"] == "started" and len(runtime.launches) == 1
        handles.append(ProcessHandle(runtime.launches[0]["pid"], data / "runtime/pythonw.exe", bridge / "bridge_supervisor.py"))
        status = capture_processes(bridge, data, handles)
        receipt_path = home / "pet-install-receipts/bailong-full.json"
        receipt = read_json(receipt_path)
        assert receipt["dataRoot"] == str(data) and receipt["edition"] == "full"
        assert receipt["desktopBefore"]["selected-avatar-id"] == {"present": True, "value": "fixture-original"}
        assert receipt["desktopBefore"]["avatar-overlay-pet-visible"] == {"present": True, "value": False}
        report["independentHelperStarted"] = status["independentProcess"]
        report["launcherMethod"] = runtime.launches[0]["method"]
        report["loadedRuntimeInitiallyLocked"] = exclusive_dll_access_is_blocked(data / "runtime/python313.dll")
        assert report["loadedRuntimeInitiallyLocked"]
        stamp = receipt_path.stat().st_mtime_ns
        before = hash_file(receipt_path)
        result = setup.install(data, user, startup, runtime=runtime)
        assert result["activation"] == "already-running" and result["filesChanged"] == 0
        assert len(runtime.launches) == 1 and receipt_path.stat().st_mtime_ns == stamp and hash_file(receipt_path) == before
        assert (data / "fixture-launch-count.txt").read_text() == "1"
        report["repeatInstallOneHelperAndNoWrites"] = True

        from detached_launcher import launch_detached
        waiter = launch_detached(data / "runtime/pythonw.exe", [str(data / "install.py"), "--finish"], data)
        wait(lambda: read_json(data / "fixture-waiter.json").get("pid") == waiter["pid"], "Fixture waiter did not start")
        handles.append(ProcessHandle(waiter["pid"], data / "runtime/pythonw.exe", data / "install.py"))
        before_check = {path: hash_file(path) for path in (config, receipt_path, startup / "BailongCodexBridge.vbs")}
        checked = uninstall(package, home, startup, check=True)
        assert checked["Status"] == "CheckOnly" and checked["OwnedProcessCount"] == 4
        assert not (data / "uninstall.request").exists()
        assert all(path.is_file() and hash_file(path) == digest for path, digest in before_check.items())
        assert all(handle.alive() for handle in handles)
        report["uninstallCheckOnlyZeroWrites"] = True

        result = uninstall(package, home, startup)
        assert result["Status"] == "Uninstalled" and result["StartupRemoved"] and result["ReceiptRemoved"]
        assert result["PetFilesRemoved"] == 2 and result["OwnProcessesReleased"]
        wait(lambda: all(not handle.alive() for handle in handles), "Fixture processes still running")
        assert config.read_bytes() == original_config
        assert not receipt_path.exists() and not (startup / "BailongCodexBridge.vbs").exists()
        assert not (home / "pets/bailong-full").exists()
        assert all(hash_file(path) == digest for path, digest in preserved.items())
        report.update(realCmdUninstallPassed=True, companionAndWaiterExited=True,
                      originalSettingsRestored=True, unrelatedConfigAndUserDataPreserved=True)

        # A reinstall clears the persistent cancellation marker, and the same
        # production uninstaller can clean this new installation again.
        result = setup.install(data, user, startup, runtime=runtime)
        assert result["activation"] == "started" and not (data / "uninstall.request").exists()
        old_pids = {handle.pid for handle in handles}
        handles.append(ProcessHandle(runtime.launches[-1]["pid"], data / "runtime/pythonw.exe", bridge / "bridge_supervisor.py"))
        wait(lambda: read_json(bridge / "bridge-status.json").get("supervisorPid") not in old_pids and
             read_json(bridge / "bridge-status.json").get("phase") == "waiting-for-codex-pet", "Reinstall did not start a new helper")
        capture_processes(bridge, data, handles)
        assert len(runtime.launches) == 2
        result = uninstall(package, home, startup)
        assert result["Status"] == "Uninstalled"
        wait(lambda: all(not handle.alive() for handle in handles), "Reinstalled fixture processes still running")
        assert config.read_bytes() == original_config
        assert all(hash_file(path) == digest for path, digest in preserved.items())
        report["reinstallAfterUninstallPassed"] = True

        for handle in handles:
            handle.close()
        shutil.rmtree(assert_fixture_path(package, fixture))
        assert not package.exists()
        report.update(packageDirectoryActuallyDeleted=True, realUserHomeTouched=False,
                      actualCodexTouched=False, modelMessagesSent=0,
                      productionInstallerSha256=hash_file(ROOT / "packaging/compact_install.py"),
                      productionUninstallerSha256=hash_file(ROOT / "packaging/uninstall_pet.ps1"))
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report), flush=True)
    finally:
        if data is not None and data.exists():
            (assert_fixture_path(data, fixture) / "uninstall.request").write_text("fixture cleanup")
        if bridge is not None and bridge.exists():
            for name in ("supervisor.stop", "pet.stop", "observer.stop"):
                (assert_fixture_path(bridge, fixture) / name).write_text("fixture cleanup")
        # Even a failure before status/handle binding must give the inert
        # fixture children time to read their stop files before deleting them.
        if data is not None and data.exists() and not handles:
            time.sleep(2)
        deadline = time.monotonic() + 10
        while any(handle.alive() for handle in handles) and time.monotonic() < deadline:
            time.sleep(.1)
        for handle in handles:
            handle.stop_owned_fixture()
            handle.close()
        if fixture.exists():
            shutil.rmtree(assert_fixture_path(fixture, fixture))


if __name__ == "__main__":
    main()
