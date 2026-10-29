"""Load BailongPet's original Python code without changing its EXE or assets.

Use this in a fresh Python 3.13 process before importing PySide6, PIL or psutil:

    from bridge_loader import load_original_module
    pet = load_original_module()
    # pet.Pet is the original QWidget class; pet.main() owns its normal lifecycle.

Only application bytecode is copied into this bridge directory. Bundled third
party Python modules are read from the EXE in memory; native modules and DLLs
are loaded from the original _internal directory. This deliberately does not
set sys.frozen, sys._MEIPASS, or change sys.executable.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import marshal
import os
from pathlib import Path
import struct
import sys
import types
import zlib


BRIDGE_DIRECTORY = Path(__file__).resolve().parent
DEFAULT_ROOT = BRIDGE_DIRECTORY.parents[1]
PROJECT_MODULES = (
    "pet", "autostart", "chat_backend", "chat_ui", "desktop_visibility",
    "quota", "selftest", "version", "movement", "video_frames",
    "desktop_task_sync", "platform_support", "mac_autostart",
)
BUNDLED_PACKAGE_ROOTS = frozenset((
    "PIL", "PySide6", "shiboken6", "psutil", "defusedxml",
))
_COOKIE_MAGIC = b"MEI\014\013\012\013\016"
_COOKIE_FORMAT = "!8sIIII64s"
_COOKIE_SIZE = struct.calcsize(_COOKIE_FORMAT)
_runtime = None


class OriginalArchive:
    """Small read-only reader for this package's PyInstaller CArchive and PYZ."""

    def __init__(self, executable: Path):
        self.executable = executable.resolve()
        self.data = self.executable.read_bytes()
        cookie_offset = self.data.rfind(_COOKIE_MAGIC)
        if cookie_offset < 0:
            raise RuntimeError("BailongPet.exe has no supported PyInstaller archive")
        cookie = self.data[cookie_offset:cookie_offset + _COOKIE_SIZE]
        if len(cookie) != _COOKIE_SIZE:
            raise RuntimeError("The PyInstaller cookie is truncated")
        _, total, toc_offset, toc_length, self.python_version, _ = struct.unpack(
            _COOKIE_FORMAT, cookie
        )
        self.start = cookie_offset + _COOKIE_SIZE - total
        if self.start < 0:
            raise RuntimeError("The PyInstaller archive bounds are invalid")
        toc = self.data[
            self.start + toc_offset:self.start + toc_offset + toc_length
        ]
        if len(toc) != toc_length:
            raise RuntimeError("The PyInstaller table of contents is truncated")
        self.entries = {}
        cursor = 0
        while cursor < len(toc):
            if len(toc) - cursor < 18:
                raise RuntimeError("A PyInstaller archive entry is truncated")
            size, offset, packed, unpacked, compressed, kind = struct.unpack(
                "!IIIIBc", toc[cursor:cursor + 18]
            )
            if size < 18 or cursor + size > len(toc):
                raise RuntimeError("A PyInstaller archive entry is invalid")
            name = toc[cursor + 18:cursor + size].rstrip(b"\0").decode("utf-8")
            self.entries[name] = (offset, packed, unpacked, compressed, kind)
            cursor += size
        self.pyz = self.read_entry("PYZ.pyz")
        if self.pyz[:4] != b"PYZ\0":
            raise RuntimeError("The embedded Python module archive is invalid")
        if self.pyz[4:8] != importlib.util.MAGIC_NUMBER:
            raise RuntimeError("Run the bridge with the same Python bytecode version as BailongPet")
        pyz_toc_offset = struct.unpack("!I", self.pyz[8:12])[0]
        if not 12 <= pyz_toc_offset < len(self.pyz):
            raise RuntimeError("The embedded module table offset is invalid")
        self.modules = dict(marshal.loads(self.pyz[pyz_toc_offset:]))

    def read_entry(self, name: str) -> bytes:
        offset, packed, unpacked, compressed, _ = self.entries[name]
        payload = self.data[self.start + offset:self.start + offset + packed]
        if len(payload) != packed:
            raise RuntimeError(f"Truncated original entry: {name}")
        if compressed:
            payload = zlib.decompress(payload)
        if len(payload) != unpacked:
            raise RuntimeError(f"Incorrect size of original entry: {name}")
        return payload

    def module_code(self, name: str) -> types.CodeType:
        # The top-level script is authoritative for the original application.
        if name == "pet":
            payload = self.read_entry("pet")
        else:
            _, offset, length = self.modules[name]
            payload = zlib.decompress(self.pyz[offset:offset + length])
        code = marshal.loads(payload)
        if not isinstance(code, types.CodeType):
            raise RuntimeError(f"The original module is not Python code: {name}")
        return code


class _OriginalModuleLoader(importlib.abc.Loader):
    def __init__(self, runtime, name: str, package: bool, origin: Path):
        self.runtime = runtime
        self.name = name
        self.package = package
        self.origin = origin

    def create_module(self, spec):
        return None

    def get_filename(self, fullname):
        return str(self.origin)

    def get_code(self, fullname):
        return self.runtime.archive.module_code(fullname)

    def is_package(self, fullname):
        return self.package

    def exec_module(self, module):
        # The original app derives asset paths from __file__.parents[1]. Keep
        # that directory contract without touching the actual app directory.
        module.__file__ = str(self.origin)
        if self.name in PROJECT_MODULES:
            module.__cached__ = str(self.runtime.code_directory / f"{self.name}.pyc")
        if self.package:
            module.__path__ = [str(self.origin.parent)]
        exec(self.get_code(self.name), module.__dict__)


class _OriginalFinder(importlib.abc.MetaPathFinder):
    def __init__(self, runtime):
        self.runtime = runtime

    def find_spec(self, fullname, path=None, target=None):
        application_module = fullname in PROJECT_MODULES
        bundled_module = fullname.split(".")[0] in BUNDLED_PACKAGE_ROOTS
        if not application_module and not bundled_module:
            return None
        if fullname not in self.runtime.archive.modules and fullname != "pet":
            # Missing Python modules may be native PYDs. PathFinder uses the
            # package's original _internal path for those extensions.
            return None
        if application_module:
            is_package = False
            origin = self.runtime.root / "app" / f"{fullname}.py"
        else:
            kind = self.runtime.archive.modules[fullname][0]
            is_package = kind == 1
            relative = Path(*fullname.split("."))
            origin = self.runtime.internal / relative
            origin = origin / "__init__.py" if is_package else origin.with_suffix(".py")
        loader = _OriginalModuleLoader(self.runtime, fullname, is_package, origin)
        return importlib.util.spec_from_loader(
            fullname, loader, origin=str(origin), is_package=is_package
        )


class BridgeRuntime:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.internal = self.root / "_internal"
        self.code_directory = BRIDGE_DIRECTORY / "original-code"
        self.archive = OriginalArchive(self.root / "BailongPet.exe")
        expected_version = sys.version_info.major * 100 + sys.version_info.minor
        if self.archive.python_version != expected_version or struct.calcsize("P") != 8:
            raise RuntimeError("This BailongPet package requires its matching 64-bit Python 3.13 runtime")
        if not self.internal.is_dir():
            raise RuntimeError("The original _internal directory is missing")
        self.dll_directories = []
        self.finder = _OriginalFinder(self)

    def extract_project_code(self):
        self.code_directory.mkdir(parents=True, exist_ok=True)
        manifest = {
            "source": str(self.archive.executable),
            "source_sha256": hashlib.sha256(self.archive.data).hexdigest(),
            "python_version": self.archive.python_version,
            "modules": {},
        }
        for name in PROJECT_MODULES:
            marshalled = marshal.dumps(self.archive.module_code(name))
            # Timestamp-based .pyc with a zero timestamp/source size. The
            # bridge's finder loads original archive code, not these cache files.
            contents = importlib.util.MAGIC_NUMBER + struct.pack("<III", 0, 0, 0) + marshalled
            destination = self.code_directory / f"{name}.pyc"
            if not destination.is_file() or destination.read_bytes() != contents:
                destination.write_bytes(contents)
            manifest["modules"][name] = {
                "file": destination.name,
                "sha256": hashlib.sha256(contents).hexdigest(),
            }
        manifest_path = self.code_directory / "manifest.json"
        contents = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        if not manifest_path.is_file() or manifest_path.read_text(encoding="utf-8") != contents:
            manifest_path.write_text(contents, encoding="utf-8")

    def install(self):
        conflicts = [
            name for name in sys.modules
            if name in PROJECT_MODULES or name.split(".")[0] in BUNDLED_PACKAGE_ROOTS
        ]
        if conflicts:
            raise RuntimeError(
                "Load the original application in a fresh process before importing "
                "its packages; already imported: " + ", ".join(sorted(conflicts)[:12])
            )
        self.extract_project_code()
        for directory in (
            self.internal,
            self.internal / "PySide6",
            self.internal / "shiboken6",
            self.internal / "PIL",
        ):
            if directory.is_dir() and hasattr(os, "add_dll_directory"):
                self.dll_directories.append(os.add_dll_directory(str(directory)))
        plugin_directory = self.internal / "PySide6" / "plugins"
        os.environ["QT_PLUGIN_PATH"] = str(plugin_directory)
        os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(plugin_directory / "platforms")
        sys.meta_path.insert(0, self.finder)


def load_original_module(root: str | Path | None = None):
    """Return the original ``pet`` module without constructing its GUI.

    ``module.Pet`` is the original QWidget class. ``module.main()`` retains
    its original tray, animation, dialogue, single-instance and exit behavior.
    Load once per fresh process; repeated calls with the same root are safe.
    """
    global _runtime
    root_path = Path(root).resolve() if root is not None else DEFAULT_ROOT
    if _runtime is None:
        candidate = BridgeRuntime(root_path)
        candidate.install()
        _runtime = candidate
    elif _runtime.root != root_path:
        raise RuntimeError("A different original application is already loaded")
    return importlib.import_module("pet")


def runtime_details():
    """Return non-sensitive diagnostics after ``load_original_module()``."""
    if _runtime is None:
        raise RuntimeError("The original application has not been loaded")
    return {
        "root": str(_runtime.root),
        "original_executable": str(_runtime.archive.executable),
        "original_executable_sha256": hashlib.sha256(_runtime.archive.data).hexdigest(),
        "internal": str(_runtime.internal),
        "code_directory": str(_runtime.code_directory),
        "python_version": sys.version,
        "python_executable": sys.executable,
    }
