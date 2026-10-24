"""One-click local installer for the compact Bailong package (stdlib only)."""
from __future__ import annotations

import argparse
import copy
import ctypes
from ctypes import wintypes
from datetime import datetime
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
import uuid

LOCAL_ID = "custom:bailong-full"
MUTEX = r"Local\BailongCodexBridgeSupervisorV1"
CLOUD_ID = re.compile(r"pet_[A-Za-z0-9_-]+\Z")
MAX_CONFIG = 1024 * 1024


class InstallError(RuntimeError):
    pass


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
    for relative in ("runtime/python.exe", "runtime/pythonw.exe", "BailongPet.exe",
                     "runtime/python313.dll", "runtime/DLLs/_sqlite3.pyd",
                     "_internal/Qt6Core.dll", "_internal/PySide6/QtCore.cp313-win_amd64.pyd",
                     "_internal/PIL/_imaging.cp313-win_amd64.pyd", "_internal/PIL/_webp.cp313-win_amd64.pyd",
                     "codex-integration/bridge/bridge_supervisor.py", "codex-integration/bridge/bridge_pet.py",
                     "codex-integration/bridge/bridge-config.json", "codex-integration/clean-assets/素材清单.json"):
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
        api.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            return bool(api.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size))) and os.path.normcase(buffer.value) == os.path.normcase(str(executable))
        finally:
            api.CloseHandle(handle)

    def launch(self, root, args):
        from detached_launcher import launch_detached
        return launch_detached(root / "runtime/pythonw.exe", args, root)


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
    current = snapshot(root)
    if runtime.process_matches(current.get("supervisorPid"), root / "runtime/pythonw.exe"):
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
            return read_small(startup_path) == desired
        except (OSError, InstallError):
            return False

    deadline = time.monotonic() + maximum_seconds
    while time.monotonic() < deadline:
        if not still_requested():
            return True  # Cancelled by disable or a newer install; this is not an error.
        current = snapshot(root)
        if runtime.process_matches(current.get("supervisorPid"), root / "runtime/pythonw.exe"):
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
    targets = {
        home / "pets/bailong-full/pet.json": manifest,
        home / "pets/bailong-full/spritesheet.webp": image,
        config_path: config,
        startup_path: startup_bytes(root),
    }
    originals = {path: read_small(path, 32 * 1024 * 1024) for path in targets}
    if originals[config_path] != before_config or originals[startup_path] != old_startup:
        raise InstallError("Codex 设置正在变化，请稍后再运行安装；未修改任何文件。")
    changed = [path for path in targets if originals[path] != targets[path]]
    result = {"ok": True, "checkOnly": check, "filesChanged": len(changed),
              "settingsChanged": config != before_config, "activation": "not-requested"}
    if check:
        return result
    backup = home / "pet-install-backups/bailong-full" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    written = []
    try:
        for index, path in enumerate(changed):
            if read_small(path, 32 * 1024 * 1024) != originals[path]:
                raise InstallError("安装时文件已变化，已停止并恢复本次写入。")
            if originals[path] is not None:
                atomic_write(backup / f"{index}-{path.name}", originals[path])
            atomic_write(path, targets[path])
            written.append(path)
    except Exception:
        for path in reversed(written):
            if read_small(path, 32 * 1024 * 1024) == targets[path]:
                if originals[path] is None:
                    path.unlink()
                else:
                    atomic_write(path, originals[path])
        raise
    result["activation"] = activate(root, old, runtime or WindowsRuntime())
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
    root = safe_path(Path(__file__).parent)
    try:
        if os.name != "nt":
            raise InstallError("此安装包需要 Windows x64。")
        if options.finish:
            from detached_launcher import process_in_job, launch_detached
            if process_in_job():
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
            return 0
        result = install(root, Path.home(), current_startup(), check=options.check)
        if options.check:
            print(json.dumps(result, ensure_ascii=True))
        else:
            print("安装完成：白龙已设为默认显示，并已启用随 Codex 启动。")
            if result["activation"] == "waiting-for-existing-pet":
                print("当前旧版继续运行；正常关闭 Codex 后自动切换，无需再执行命令。")
            print("若 Codex 列表尚未刷新，请在当前任务结束后正常关闭并重新打开 Codex。")
            print("请保留此解压目录，运行文件和登录启动项仍使用它。")
        return 0
    except Exception as error:
        message = str(error) if isinstance(error, InstallError) else "安装未完成；请检查完整解压、目录写入权限及配置格式。"
        if options.finish:
            atomic_write(root / "install-status.txt", (message + "\n").encode("utf-8"))
        else:
            print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
