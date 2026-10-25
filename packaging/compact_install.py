"""One-click local installer for the compact Bailong package (stdlib only)."""
from __future__ import annotations

import argparse
import copy
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import tempfile
import time
import tomllib
import traceback
import uuid

LOCAL_ID = "custom:bailong-full"
MUTEX = r"Local\BailongCodexBridgeSupervisorV1"
CLOUD_ID = re.compile(r"pet_[A-Za-z0-9_-]+\Z")
MAX_CONFIG = 1024 * 1024
DESKTOP_KEYS = ("selected-avatar-id", "avatar-overlay-pet-visible")
UNINSTALL_MARKER = "uninstall.request"
REQUIRED_BRIDGE_FILES = (
    "bridge_loader.py bridge_pet.py pet_lifecycle.py bridge_policy.py bridge_supervisor.py "
    "bailong_toolbar.py activity_animation.py activity_cards.py activity_runtime.py activity_source.py "
    "activity_ui_hints.ps1 codex_observer.ps1 native_mini_control.ps1 native_menu_shield.cs "
    "native_overlay_mask.py native_visibility.py desktop_ipc.py desktop_chat_adapter.py "
    "desktop_chat_link.py detached_launcher.py asset_source.py bridge-config.json"
).split()


class InstallError(RuntimeError):
    pass


class ActivationError(InstallError):
    """Registration succeeded, but the companion did not confirm startup."""


def error_details(error: Exception) -> dict:
    """Report useful local diagnostics without serializing config or arguments."""
    details = {"type": type(error).__name__}
    for key in ("winerror", "errno"):
        value = getattr(error, key, None)
        if type(value) is int:
            details[key] = value
    frames = traceback.extract_tb(error.__traceback__)
    details["frames"] = [{"file": Path(frame.filename).name, "line": frame.lineno,
                           "function": frame.name} for frame in frames[-8:]]
    if error.__cause__ is not None:
        details["cause"] = error_details(error.__cause__)
    return details


def describe_error(error: Exception) -> str:
    if isinstance(error, InstallError):
        return str(error)
    # Launcher messages contain only fixed explanations and numeric Windows
    # errors; arbitrary exception text may contain configuration or user input.
    launcher_error = getattr(sys.modules.get("detached_launcher"), "DetachedLaunchError", None)
    if isinstance(launcher_error, type) and isinstance(error, launcher_error):
        return "Windows 后台启动检查失败：" + str(error)
    if isinstance(error, tomllib.TOMLDecodeError):
        return "Codex 配置不是有效的 TOML，未修改配置。"
    if isinstance(error, UnicodeError):
        return "安装所需文本不是有效的 UTF-8，请检查配置或重新完整解压。"
    if isinstance(error, PermissionError):
        message = "无法写入安装或配置目录，文件可能被占用或没有写入权限。"
    elif isinstance(error, FileNotFoundError):
        message = "缺少安装所需文件，请完整解压后运行安装入口。"
    elif isinstance(error, OSError):
        message = "Windows 文件或进程操作失败。"
    else:
        message = "安装程序遇到异常。"
    details = error_details(error)
    codes = [details["type"]]
    codes.extend(f"{key}={details[key]}" for key in ("winerror", "errno") if key in details)
    if details["frames"]:
        frame = details["frames"][-1]
        codes.append(f'{frame["file"]}:{frame["line"]}')
    return message + "（" + "; ".join(codes) + "）"


def report_failure(root: Path, error: Exception, *, finish=False, persist=True) -> str:
    message = describe_error(error)
    if persist:
        document = {"schemaVersion": 1, "ok": False,
                    "at": datetime.now(timezone.utc).isoformat(),
                    "operation": "deferred-activation" if finish else "install",
                    "registrationCompleted": isinstance(error, ActivationError),
                    "message": message, "error": error_details(error)}
        try:
            atomic_write(root / "install-diagnostic.json",
                         (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            atomic_write(root / "install-status.txt", (message + "\n").encode("utf-8"))
            message += "\n详细原因已保存在 data/install-diagnostic.json。"
        except (OSError, InstallError):
            message += "\n诊断文件也无法写入；请保留此窗口中的错误信息。"
    if sys.stderr is not None:
        print(message, file=sys.stderr)
    return message


def safe_path(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    if str(path).startswith("\\\\"):
        raise InstallError("安装仅支持本机普通磁盘目录。")
    for parent in (path, *path.parents):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise InstallError("安装路径不能经过符号链接或目录联接。")
    return path


def read_small(path: Path, maximum=MAX_CONFIG) -> bytes | None:
    safe_path(path)
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > maximum:
        raise InstallError("安装所需文件类型或大小异常，未修改配置。")
    return path.read_bytes()


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def choose_avatar(config: dict, global_bytes: bytes | None) -> str:
    """Only preserve an already-selected full-pet ID; never guess an account."""
    selected = config.get("desktop", {}).get("selected-avatar-id")
    if not isinstance(selected, str) or not CLOUD_ID.fullmatch(selected):
        return LOCAL_ID
    try:
        document = json.loads(global_bytes.decode("utf-8-sig")) if global_bytes else {}
        maps = document.get("electron-persisted-atom-state", {}).get("migrated-cloud-pet-ids-v1", {})
        if isinstance(maps, dict) and any(
            isinstance(mapping, dict) and mapping.get(LOCAL_ID) == selected
            for mapping in maps.values()
        ):
            return selected
    except (ValueError, AttributeError, UnicodeError):
        pass
    return LOCAL_ID


def update_config(raw: bytes | None, selected: str) -> bytes:
    raw = raw or b""
    try:
        source = raw.decode("utf-8-sig")
        before = tomllib.loads(source)
    except (ValueError, UnicodeError) as error:
        raise InstallError("Codex 配置不是有效的 UTF-8 TOML，未修改。") from error
    desktop = before.get("desktop", {})
    if not isinstance(desktop, dict):
        raise InstallError("Codex desktop 配置不是设置表，未修改。")
    changes = {"selected-avatar-id": selected, "avatar-overlay-pet-visible": True}
    expected = copy.deepcopy(before)
    expected.setdefault("desktop", {}).update(changes)
    if expected == before:
        return raw
    newline = "\r\n" if "\r\n" in source else "\n"
    lines = source.splitlines(keepends=True)
    table = re.compile(r'''^\s*\[\s*(?:desktop|"desktop"|'desktop')\s*\]\s*(?:#.*)?$''')
    starts = [i for i, line in enumerate(lines) if table.fullmatch(line.rstrip("\r\n"))]
    if not starts:
        if "desktop" in before:
            raise InstallError("当前 desktop 使用特殊 TOML 写法；为保留其他设置，安装未改写。")
        text = source + (newline if source and not source.endswith(("\n", "\r")) else "")
        text += newline + "[desktop]" + newline
        text += f"selected-avatar-id = {json.dumps(selected)}{newline}avatar-overlay-pet-visible = true{newline}"
    else:
        start = starts[0]
        end = next((i for i in range(start + 1, len(lines)) if re.match(r"^\s*\[", lines[i])), len(lines))
        remaining = dict(changes)
        for index in range(start + 1, end):
            for key in tuple(remaining):
                pattern = re.compile(r"^\s*(?:" + re.escape(key) + r'|"' + re.escape(key) + r'"|\'' + re.escape(key) + r"')\s*=")
                if pattern.match(lines[index]):
                    value = remaining.pop(key)
                    lines[index] = f"{key} = {json.dumps(value)}{newline}"
        insert = [f"{key} = {json.dumps(value)}{newline}" for key, value in remaining.items()]
        if insert and end > 0 and not lines[end - 1].endswith(("\n", "\r")):
            lines[end - 1] += newline
        lines[end:end] = insert
        text = "".join(lines)
    try:
        after = tomllib.loads(text)
    except ValueError as error:
        raise InstallError("无法安全修改此 TOML 写法，原配置保持不变。") from error
    if after != expected:
        raise InstallError("配置保留校验失败，原配置保持不变。")
    return (b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b"") + text.encode("utf-8")


def desktop_before(config: dict) -> dict:
    desktop = config.get("desktop", {})
    result = {}
    for key in DESKTOP_KEYS:
        present = key in desktop
        value = desktop.get(key)
        if present and (type(value) is not (str if key == "selected-avatar-id" else bool)):
            raise InstallError("宠物设置的类型无效，未修改。")
        result[key] = {"present": present, "value": value if present else None}
    return result


def receipt_before(raw: bytes | None, config: dict) -> dict:
    if raw is None:
        return desktop_before(config)
    try:
        def require(condition):
            if not condition:
                raise ValueError("Invalid installation receipt")
        value = json.loads(raw.decode("utf-8-sig"))
        require(type(value.get("schemaVersion")) is int and value["schemaVersion"] == 1)
        require(value.get("edition") == "full")
        require(isinstance(value.get("dataRoot"), str) and Path(value["dataRoot"]).is_absolute())
        safe_path(Path(value["dataRoot"]))
        require(isinstance(value.get("avatarId"), str))
        require(value["avatarId"] == LOCAL_ID or CLOUD_ID.fullmatch(value["avatarId"]))
        require(all(isinstance(value["files"][name], str) and re.fullmatch(r"[a-fA-F0-9]{64}", value["files"][name])
                    for name in ("pet.json", "spritesheet.webp")))
        before = value["desktopBefore"]
        for key in DESKTOP_KEYS:
            field = before[key]
            require(type(field["present"]) is bool)
            if field["present"]:
                require(type(field["value"]) is (str if key == "selected-avatar-id" else bool))
            else:
                require(field.get("value") is None)
        # Keep the original choice through repeated installs and folder moves.
        return {key: {"present": before[key]["present"], "value": before[key]["value"]} for key in DESKTOP_KEYS}
    except (ValueError, UnicodeError, AssertionError, KeyError, TypeError, AttributeError, InstallError) as error:
        raise InstallError("已有的白龙安装记录无效，未修改文件。") from error


def startup_bytes(root: Path) -> bytes:
    pythonw = root / "runtime/pythonw.exe"
    supervisor = root / "codex-integration/bridge/bridge_supervisor.py"
    command = f'"{pythonw}" "{supervisor}"'
    quote = lambda value: value.replace('"', '""')
    return ("Option Explicit\r\nDim shell\r\nSet shell = CreateObject(\"WScript.Shell\")\r\n"
            f'shell.CurrentDirectory = "{quote(str(root))}"\r\n'
            f'shell.Run "{quote(command)}", 0, False\r\n').encode("utf-16")


def parse_old_startup(raw: bytes | None) -> tuple[Path, Path] | None:
    if not raw:
        return None
    try:
        text = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
        lines = text.splitlines()
        if len(lines) != 5 or lines[:3] != ["Option Explicit", "Dim shell", 'Set shell = CreateObject("WScript.Shell")']:
            return None
        directory = re.fullmatch(r'shell\.CurrentDirectory = "((?:[^"]|"")*)"', lines[3])
        run = re.fullmatch(r'shell\.Run "((?:[^"]|"")*)", 0, False', lines[4])
        if not directory or not run:
            return None
        command = run.group(1).replace('""', '"')
        paths = re.fullmatch(r'"([^"\r\n]+)" "([^"\r\n]+)"', command)
        if not paths:
            return None
        root = safe_path(Path(directory.group(1).replace('""', '"')))
        pythonw, script = map(Path, paths.groups())
        if not pythonw.is_absolute() or not script.is_absolute() or pythonw.name.lower() != "pythonw.exe":
            return None
        if safe_path(script) != root / "codex-integration/bridge/bridge_supervisor.py":
            return None
        return root, safe_path(pythonw)
    except (ValueError, UnicodeError, OSError, InstallError):
        return None


def validate_old(old: tuple[Path, Path] | None, root: Path) -> tuple[Path, Path] | None:
    if old is None:
        return None
    previous, interpreter = old
    try:
        safe_path(previous)
        script = read_small(previous / "codex-integration/bridge/bridge_supervisor.py") or b""
        if not interpreter.is_file() or not (previous / "BailongPet.exe").is_file():
            return None
        if sha(previous / "BailongPet.exe") != sha(root / "BailongPet.exe"):
            return None
        if b"BailongCodexBridgeSupervisorV1" not in script or b'"supervisor.stop"' not in script:
            return None
        return previous, interpreter
    except (OSError, InstallError):
        return None


def atomic_write(path: Path, content: bytes):
    safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".bailong-install-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def validate_package(root: Path):
    required = ("runtime/python.exe", "runtime/pythonw.exe", "BailongPet.exe", "uninstall.ps1",
                     "runtime/python313.dll", "runtime/DLLs/_sqlite3.pyd",
                     "_internal/Qt6Core.dll", "_internal/PySide6/QtCore.cp313-win_amd64.pyd",
                     "_internal/PIL/_imaging.cp313-win_amd64.pyd", "_internal/PIL/_webp.cp313-win_amd64.pyd",
                     "codex-integration/bridge/bridge_supervisor.py", "codex-integration/bridge/bridge_pet.py",
                     "codex-integration/bridge/bridge-config.json", "codex-integration/clean-assets/素材清单.json")
    required += tuple("codex-integration/bridge/" + name for name in REQUIRED_BRIDGE_FILES)
    for relative in required:
        path = safe_path(root / relative)
        if not path.is_file():
            raise InstallError("安装包不完整，请完整解压后再运行唯一安装入口。")
    asset_root = root / "codex-integration/clean-assets"
    asset_raw = read_small(asset_root / "素材清单.json")
    try:
        asset_manifest = json.loads(asset_raw.decode("utf-8-sig"))
        validation = json.loads(read_small(asset_root / "repair-validation.json").decode("utf-8-sig"))
        if validation.get("animations") != 25 or validation.get("frames") != 2529 or validation.get("manifestSha256") != hashlib.sha256(asset_raw).hexdigest():
            raise ValueError("invalid repair metadata")
        records = []
        def visit(value):
            if isinstance(value, dict):
                if "file" in value:
                    records.append(value)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(asset_manifest)
        if len(records) != 25:
            raise ValueError("missing animation records")
        for record in records:
            for key in ("file", "playbackFile"):
                if key not in record:
                    continue
                relative = Path(record[key])
                if relative.is_absolute() or relative.drive or ".." in relative.parts:
                    raise ValueError("invalid animation path")
                animation = safe_path(asset_root / relative)
                if not animation.is_relative_to(asset_root) or not animation.is_file() or animation.stat().st_size == 0:
                    raise ValueError("missing animation file")
    except (ValueError, TypeError, AttributeError, UnicodeError) as error:
        raise InstallError("去噪动画不完整或清单异常，请重新完整解压。") from error
    entry = root / "codex-integration/full-entry"
    manifest = read_small(entry / "pet.json")
    try:
        value = json.loads(manifest.decode("utf-8-sig"))
    except (ValueError, AttributeError, UnicodeError) as error:
        raise InstallError("白龙入口清单无效。") from error
    if (value.get("displayName") != "白龙完整版" or value.get("spriteVersionNumber") != 1
            or value.get("spritesheetPath") != "spritesheet.webp"):
        raise InstallError("白龙入口清单与完整版不符。")
    image = read_small(entry / "spritesheet.webp", 32 * 1024 * 1024)
    if not image or len(image) < 12 or image[:4] != b"RIFF" or image[8:12] != b"WEBP" or struct.unpack("<I", image[4:8])[0] + 8 != len(image):
        raise InstallError("白龙图集文件无效。")
    return manifest, image


class WindowsRuntime:
    def mutex_open(self):
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenMutexW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
        api.OpenMutexW.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = api.OpenMutexW(0x100000, False, MUTEX)
        if handle:
            api.CloseHandle(handle)
            return True
        return False

    def process_matches(self, pid, executable):
        if type(pid) is not int or pid <= 0:
            return False
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        api.OpenProcess.restype = wintypes.HANDLE
        api.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        api.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        api.GetExitCodeProcess.restype = wintypes.BOOL
        api.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            exit_code = wintypes.DWORD()
            return (bool(api.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)))
                    and os.path.normcase(buffer.value) == os.path.normcase(str(executable))
                    and bool(api.GetExitCodeProcess(handle, ctypes.byref(exit_code)))
                    and exit_code.value == 259)  # STILL_ACTIVE, not a retained exited handle.
        finally:
            api.CloseHandle(handle)

    def launch(self, root, args):
        from detached_launcher import launch_detached
        started_at = time.time()
        result = launch_detached(root / "runtime/pythonw.exe", args, root)
        if args and Path(args[0]) == root / "codex-integration/bridge/bridge_supervisor.py":
            wait_for_supervisor(root, result, self, started_at=started_at)
        return result


def wait_for_supervisor(root: Path, launch: dict, runtime, *, started_at: float,
                        timeout: float = 15.0):
    """Require a live supervisor reporting that it has started its observer."""
    pid = launch.get("pid") if isinstance(launch, dict) else None
    if type(pid) is not int or pid <= 0:
        raise InstallError("后台启动未返回有效进程标识，无法确认助手已运行。")
    bridge = root / "codex-integration/bridge"
    deadline = time.monotonic() + timeout
    dead_since = None
    while True:
        if (root / UNINSTALL_MARKER).exists():
            raise InstallError("检测到卸载请求，已停止等待后台启动。")
        state = snapshot(root)
        try:
            fresh = (bridge / "bridge-status.json").stat().st_mtime >= started_at - 1.0
        except OSError:
            fresh = False
        same_pid = fresh and state.get("supervisorPid") == pid
        # Two installers may both reach CreateProcess before the singleton is
        # acquired. The loser exits normally; the winner at this exact package
        # may confirm readiness for both installers.
        if supervisor_ready(state) and state.get("supervisorPid") != pid:
            if runtime.process_matches(state["supervisorPid"], root / "runtime/pythonw.exe"):
                return
        if same_pid and state.get("phase") == "stopped":
            raise InstallError("后台助手在初始化期间退出；请查看 data/codex-integration/bridge/supervisor.log。")
        # Only consume a launch error for the exact process just launched.
        try:
            failure_path = bridge / "launch-error.json"
            if failure_path.stat().st_mtime >= started_at - 1.0:
                failure = json.loads(read_small(failure_path, 65536).decode("utf-8-sig"))
                if isinstance(failure, dict) and failure.get("pid") == pid:
                    kind = failure.get("errorType")
                    kind = kind if isinstance(kind, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", kind) else "StartupError"
                    raise InstallError(f"后台助手初始化失败（{kind}）；详见 data/codex-integration/bridge/launch-error.json。")
        except (OSError, ValueError, AttributeError):
            pass
        alive = runtime.process_matches(pid, root / "runtime/pythonw.exe")
        now = time.monotonic()
        if not alive:
            dead_since = now if dead_since is None else dead_since
            # A concurrent winner may own the mutex but not have emitted its
            # first status yet. Give it bounded time to confirm this root.
            if now >= deadline or (now - dead_since >= 2.0 and not runtime.mutex_open()):
                raise InstallError("后台助手启动后立即退出，尚未确认安装完成；请查看 data/codex-integration/bridge 下的诊断文件。")
        if alive and same_pid and supervisor_ready(state):
            return
        if now >= deadline:
            raise InstallError("等待后台助手启动确认超时，尚未显示安装成功；请查看 data/codex-integration/bridge 下的诊断文件。")
        time.sleep(.1)


def supervisor_ready(state: dict) -> bool:
    return (state.get("independentProcess") is True
            and type(state.get("supervisorPid")) is int and state["supervisorPid"] > 0
            and state.get("phase") in ("waiting-for-observer", "waiting-for-codex-pet", "visible", "hidden", "manually-closed")
            and type(state.get("observerPid")) is int and state["observerPid"] > 0)


def snapshot(root: Path):
    path = root / "codex-integration/bridge/bridge-status.json"
    try:
        if time.time() - path.stat().st_mtime > 15:
            return {}
        value = json.loads(read_small(path, 65536).decode("utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, AttributeError, InstallError):
        return {}


def safe_to_switch(old, runtime):
    previous, interpreter = old
    state = snapshot(previous)
    if not runtime.process_matches(state.get("supervisorPid"), interpreter):
        return False
    if state.get("workerPid") is not None or state.get("phase") not in ("waiting-for-codex-pet", "manually-closed"):
        return False
    intent = previous / "codex-integration/bridge/codex-state.json"
    try:
        if time.time() - intent.stat().st_mtime > 15:
            return False
        value = json.loads(read_small(intent, 65536).decode("utf-8-sig"))
        return value.get("running") is False
    except (OSError, ValueError, AttributeError, InstallError):
        return False


def activate(root: Path, old, runtime):
    if (root / UNINSTALL_MARKER).exists():
        return "cancelled-by-uninstall"
    current = snapshot(root)
    if runtime.process_matches(current.get("supervisorPid"), root / "runtime/pythonw.exe"):
        wait_for_supervisor(root, {"pid": current["supervisorPid"]}, runtime, started_at=time.time() - 15)
        return "already-running"
    if not runtime.mutex_open():
        runtime.launch(root, [str(root / "codex-integration/bridge/bridge_supervisor.py"), "--show-on-start"])
        return "started"
    arguments = [str(root / "install.py"), "--finish"]
    if old:
        arguments.extend(["--old-root", str(old[0]), "--old-pythonw", str(old[1])])
    runtime.launch(root, arguments)
    return "waiting-for-existing-pet"


def finish_when_idle(root: Path, old, runtime, maximum_seconds=43200, startup_dir=None):
    old = validate_old(old, root)
    startup_path = (startup_dir or current_startup()) / "BailongCodexBridge.vbs"
    desired = startup_bytes(root)

    def still_requested():
        try:
            return not (root / UNINSTALL_MARKER).exists() and read_small(startup_path) == desired
        except (OSError, InstallError):
            return False

    deadline = time.monotonic() + maximum_seconds
    while time.monotonic() < deadline:
        if not still_requested():
            return True  # Cancelled by disable or a newer install; this is not an error.
        current = snapshot(root)
        if runtime.process_matches(current.get("supervisorPid"), root / "runtime/pythonw.exe"):
            wait_for_supervisor(root, {"pid": current["supervisorPid"]}, runtime, started_at=time.time() - 15)
            return True
        if not runtime.mutex_open():
            if not still_requested():
                return True
            runtime.launch(root, [str(root / "codex-integration/bridge/bridge_supervisor.py"), "--show-on-start"])
            return True
        if old and safe_to_switch(old, runtime):
            time.sleep(2)
            if still_requested() and safe_to_switch(old, runtime):
                # Same graceful protocol as --stop; never executes an old script.
                atomic_write(old[0] / "codex-integration/bridge/supervisor.stop", b"installer idle migration\n")
        time.sleep(2)
    return False


def install(root: Path, user_home: Path, startup_dir: Path, *, check=False, runtime=None):
    root, user_home, startup_dir = map(safe_path, (root, user_home, startup_dir))
    if root == user_home or root == startup_dir:
        raise InstallError("安装包目录不能与用户配置目录重合。")
    manifest, image = validate_package(root)
    home = user_home / ".codex"
    config_path = home / "config.toml"
    before_config = read_small(config_path)
    parsed = tomllib.loads((before_config or b"").decode("utf-8-sig"))
    global_bytes = read_small(home / ".codex-global-state.json", 32 * 1024 * 1024)
    selected = choose_avatar(parsed, global_bytes)
    config = update_config(before_config, selected)
    startup_path = startup_dir / "BailongCodexBridge.vbs"
    old_startup = read_small(startup_path)
    old = validate_old(parse_old_startup(old_startup), root)
    receipt_path = home / "pet-install-receipts/bailong-full.json"
    old_receipt = read_small(receipt_path)
    receipt = {"schemaVersion": 1, "edition": "full", "dataRoot": str(root), "avatarId": selected,
               "files": {"pet.json": hashlib.sha256(manifest).hexdigest(),
                         "spritesheet.webp": hashlib.sha256(image).hexdigest()},
               "desktopBefore": receipt_before(old_receipt, parsed),
               "desktopInstalled": {"selected-avatar-id": selected, "avatar-overlay-pet-visible": True}}
    receipt_bytes = (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    uninstall_marker = root / UNINSTALL_MARKER
    old_marker = read_small(uninstall_marker, 65536)
    targets = {
        home / "pets/bailong-full/pet.json": manifest,
        home / "pets/bailong-full/spritesheet.webp": image,
        config_path: config,
        startup_path: startup_bytes(root),
        receipt_path: receipt_bytes,
    }
    originals = {path: read_small(path, 32 * 1024 * 1024) for path in targets}
    if (originals[config_path] != before_config or originals[startup_path] != old_startup
            or originals[receipt_path] != old_receipt):
        raise InstallError("Codex 设置正在变化，请稍后再运行安装；未修改任何文件。")
    changed = [path for path in targets if originals[path] != targets[path]]
    result = {"ok": True, "checkOnly": check, "filesChanged": len(changed),
              "settingsChanged": config != before_config, "activation": "not-requested"}
    if check:
        return result
    backup = home / "pet-install-backups/bailong-full" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    written = []
    removed_marker = False
    try:
        for index, path in enumerate(changed):
            if read_small(path, 32 * 1024 * 1024) != originals[path]:
                raise InstallError("安装时文件已变化，已停止并恢复本次写入。")
            if originals[path] is not None:
                atomic_write(backup / f"{index}-{path.name}", originals[path])
            atomic_write(path, targets[path])
            written.append(path)
        if read_small(uninstall_marker, 65536) != old_marker:
            raise InstallError("检测到同时进行的卸载，已取消本次安装。")
        if old_marker is not None:
            uninstall_marker.unlink()
            removed_marker = True
    except Exception:
        for path in reversed(written):
            if read_small(path, 32 * 1024 * 1024) == targets[path]:
                if originals[path] is None:
                    path.unlink()
                else:
                    atomic_write(path, originals[path])
        if removed_marker and not uninstall_marker.exists():
            atomic_write(uninstall_marker, old_marker)
        raise
    try:
        result["activation"] = activate(root, old, runtime or WindowsRuntime())
    except Exception as error:
        raise ActivationError("宠物文件和启动设置已安装，但后台助手未能确认启动。\n" + describe_error(error)) from error
    return result


def current_startup():
    buffer = ctypes.create_unicode_buffer(32768)
    # CSIDL_STARTUP; respects redirected Windows profile folders.
    result = ctypes.WinDLL("shell32").SHGetFolderPathW(None, 7, None, 0, buffer)
    if result:
        raise InstallError("无法定位当前 Windows 用户的登录启动目录。")
    return Path(buffer.value)


class ActivationGuard:
    """Suppress duplicate hidden migration waiters for the same extracted package."""
    def __init__(self, root):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        self.api.CreateMutexW.restype = wintypes.HANDLE
        self.api.CloseHandle.argtypes = (wintypes.HANDLE,)
        suffix = hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()[:24]
        self.handle = self.api.CreateMutexW(None, False, "Local\\BailongCompactInstall-" + suffix)
        self.acquired = bool(self.handle) and ctypes.get_last_error() != 183
        if not self.handle:
            raise InstallError("无法创建安装迁移状态。")

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--finish", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--old-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--old-pythonw", type=Path, help=argparse.SUPPRESS)
    options = parser.parse_args()
    root = Path(__file__).parent
    try:
        root = safe_path(root)
        if os.name != "nt":
            raise InstallError("此安装包需要 Windows x64。")
        if options.finish:
            if (root / UNINSTALL_MARKER).exists():
                return 0
            from detached_launcher import process_requires_detach, launch_detached
            if process_requires_detach():
                launch_detached(root / "runtime/pythonw.exe", [str(root / "install.py"), *sys.argv[1:]], root)
                return 0
            old = (options.old_root, options.old_pythonw) if options.old_root and options.old_pythonw else None
            guard = ActivationGuard(root)
            try:
                if not guard.acquired:
                    return 0
                finished = finish_when_idle(root, old, WindowsRuntime())
            finally:
                guard.close()
            if not finished:
                atomic_write(root / "install-status.txt", "旧桌宠仍在运行；新版本将在下次登录 Windows 时启用。\n".encode("utf-8"))
            elif not (root / UNINSTALL_MARKER).exists():
                atomic_write(root / "install-status.txt", "后台迁移处理已完成。\n".encode("utf-8"))
            return 0
        result = install(root, Path.home(), current_startup(), check=options.check)
        if options.check:
            print(json.dumps(result, ensure_ascii=True))
        else:
            if result["activation"] == "cancelled-by-uninstall":
                raise ActivationError("宠物文件曾完成安装，但检测到同时进行的卸载，后台启动已取消。")
            print("安装完成：白龙已设为默认显示，并已启用随 Codex 启动。")
            if result["activation"] == "waiting-for-existing-pet":
                print("当前旧版继续运行；正常关闭 Codex 后自动切换，无需再执行命令。")
            print("若 Codex 列表尚未刷新，请在当前任务结束后正常关闭并重新打开 Codex。")
            print("请保留此解压目录。删除前先运行“一键卸载白龙.cmd”，等待卸载窗口结束。")
        return 0
    except Exception as error:
        report_failure(root, error, finish=options.finish, persist=not options.check)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
