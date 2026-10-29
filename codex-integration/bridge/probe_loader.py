"""Offline smoke probe. Creates no Pet window and reads no account/history files."""

from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    # This must be set before importing Qt or constructing QApplication.
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from bridge_loader import load_original_module, runtime_details

    pet = load_original_module()
    from PySide6 import QtCore, QtGui, QtNetwork, QtWidgets
    from PIL import Image
    import PIL
    import psutil

    application = QtWidgets.QApplication(["bailong-bridge-probe", "-platform", "offscreen"])
    application.setQuitOnLastWindowClosed(False)
    # Import the dialogue code, without constructing its controllers/stores.
    for name in ("chat_backend", "chat_ui", "desktop_visibility", "quota", "version"):
        importlib.import_module(name)
    manifest = json.loads((pet.ASSETS / "素材清单.json").read_text(encoding="utf-8"))
    idle = manifest["idle"]
    asset_relative = idle.get("playbackFile", idle["file"].replace(".webm", ".webp"))
    asset_path = pet.ASSETS / asset_relative
    with Image.open(asset_path) as asset:
        first_frame = asset.convert("RGBA")
        frame = QtGui.QImage(
            first_frame.tobytes(), first_frame.width, first_frame.height,
            QtGui.QImage.Format.Format_RGBA8888,
        ).copy()
        pixmap = QtGui.QPixmap.fromImage(frame)
        asset_details = {
            "path": str(asset_path),
            "size": list(asset.size),
            "frame_count": getattr(asset, "n_frames", 1),
            "qt_pixmap_valid": not pixmap.isNull(),
        }
    result = runtime_details()
    result.update({
        "ok": True,
        "pet_module_file": pet.__file__,
        "pet_root": str(pet.ROOT),
        "pet_assets": str(pet.ASSETS),
        "pet_widget_class": f"{pet.Pet.__module__}.{pet.Pet.__name__}",
        "application_platform": application.platformName(),
        "top_level_window_count": len(application.topLevelWidgets()),
        "qt_version": QtCore.qVersion(),
        "qt_core_binary": QtCore.__file__,
        "qt_plugin_paths": QtCore.QCoreApplication.libraryPaths(),
        "pillow_version": PIL.__version__,
        "pillow_binary": Image.core.__file__,
        "psutil_version": psutil.__version__,
        "psutil_binary": psutil._psplatform.cext.__file__,
        "asset_probe": asset_details,
        "same_module_on_second_load": load_original_module() is pet,
        "sys_frozen": bool(getattr(sys, "frozen", False)),
    })
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output_path = args.output.resolve()
        bridge_root = Path(__file__).resolve().parent
        if not output_path.is_relative_to(bridge_root):
            raise ValueError("Probe reports must remain in the bridge directory")
        output_path.write_text(output, encoding="utf-8")
    print(output)
    application.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
