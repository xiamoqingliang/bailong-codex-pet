"""Selection policy for the full Bailong companion; never change Codex settings.

The native Mini's show/hide state also controls our pet and toolbar. Its drawing
region is masked by the supervisor while full mode is selected.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time
import tomllib


BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / "bridge-config.json"
FULL_LOCAL_ID = "custom:bailong-full"
MAX_FILE_BYTES = 1024 * 1024
CONFIG_CACHE_SECONDS = 2.0
_AVATAR_ID = re.compile(r"(?:custom:[A-Za-z0-9][A-Za-z0-9._-]*|pet_[A-Za-z0-9_-]+)\Z")
_CLOUD_ID = re.compile(r"pet_[A-Za-z0-9_-]+\Z")
_cached_path: Path | None = None
_cached_stamp: tuple[int, int] | None = None
_cached_at = 0.0
_cached_ids: frozenset[str] = frozenset()


def _configured_ids() -> frozenset[str]:
    """Cache successful reads briefly; any failure discards the prior result."""
    global _cached_path, _cached_stamp, _cached_at, _cached_ids
    now = time.monotonic()
    path = Path(CONFIG_FILE)
    try:
        info = path.stat()
        if not 0 < info.st_size <= MAX_FILE_BYTES:
            raise ValueError("invalid bridge config size")
        stamp = (info.st_mtime_ns, info.st_size)
        if (path == _cached_path and stamp == _cached_stamp
                and 0 <= now - _cached_at < CONFIG_CACHE_SECONDS):
            return _cached_ids
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        values = data.get("fullAvatarIds") if isinstance(data, dict) else None
        if (not isinstance(values, list) or not 1 <= len(values) <= 32
                or not all(isinstance(value, str) and _AVATAR_ID.fullmatch(value)
                           for value in values)):
            raise ValueError("fullAvatarIds must contain exact custom or cloud pet IDs")
        result = frozenset(values)
        _cached_path, _cached_stamp, _cached_at, _cached_ids = path, stamp, now, result
        return result
    except (OSError, UnicodeError, ValueError, TypeError):
        _cached_path, _cached_stamp, _cached_at, _cached_ids = None, None, now, frozenset()
        return frozenset()


def full_mode_selected(state: object) -> bool:
    """Return true only for a known, enabled dedicated full-Bailong selection."""
    if (not isinstance(state, dict) or state.get("selectionKnown") is not True
            or state.get("nativePetVisible") is not True):
        return False
    selected = state.get("selectedAvatarId")
    if not isinstance(selected, str):
        return False
    configured = _configured_ids()
    if not configured:
        return False
    if selected in configured:
        return True
    # Accept only the observer's exact migration of our configured local slug.
    # The observer strips account identifiers and all unrelated migration keys.
    migrated = state.get("fullBailongMigratedIds")
    return bool(FULL_LOCAL_ID in configured and isinstance(migrated, list)
                and _CLOUD_ID.fullmatch(selected)
                and any(isinstance(value, str) and value == selected for value in migrated))


def full_mode_visible(state: object) -> bool:
    """A native Hide also hides the full companion."""
    return bool(isinstance(state, dict) and state.get("running") is True
                and state.get("overlayOpen") is True and full_mode_selected(state))


def read_desktop_selection(path: Path) -> dict[str, object]:
    """Project just the two relevant TOML keys; malformed reads fail closed."""
    unknown = {"selectionKnown": False, "selectedAvatarId": None, "nativePetVisible": None}
    try:
        info = path.stat()
        if info.st_size > MAX_FILE_BYTES:
            return unknown
        with path.open("rb") as source:
            data = tomllib.load(source)
        desktop = data.get("desktop", {})
        if not isinstance(desktop, dict):
            return unknown
        selected = desktop.get("selected-avatar-id")
        visible = desktop.get("avatar-overlay-pet-visible", True)
        if selected is not None and (not isinstance(selected, str) or not selected):
            return unknown
        if type(visible) is not bool:
            return unknown
        return {"selectionKnown": True, "selectedAvatarId": selected, "nativePetVisible": visible}
    except (OSError, UnicodeError, ValueError, TypeError):
        return unknown


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, required=True,
                        help="read only the desktop pet selection from this TOML file")
    args = parser.parse_args(argv)
    print(json.dumps(read_desktop_selection(args.selection), ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
