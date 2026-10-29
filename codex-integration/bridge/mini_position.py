"""Move only an observer-identified Codex Mini window, in physical pixels.

The observer supplies the HWND, owning PID and geometry. No process scan,
conversation access, synthetic input or Codex file changes are involved here.
All DPI-context changes are confined to the calling thread and restored before
returning, including on failures, so Qt keeps its own coordinate conventions.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from functools import lru_cache
import math
from numbers import Real


_OVERLAY_STYLE = 0x00000008 | 0x00000080  # WS_EX_TOPMOST | WS_EX_TOOLWINDOW
_MOVE_FLAGS = 0x0001 | 0x0004 | 0x0010 | 0x0200  # NOSIZE, NOZORDER, NOACTIVATE, NOOWNERZORDER
_RECT_KEYS = ('x', 'y', 'width', 'height')


@lru_cache(maxsize=1)
def _native():
    api = ctypes.WinDLL('user32', use_last_error=True)
    api.IsWindow.argtypes = [wintypes.HWND]
    api.IsWindow.restype = wintypes.BOOL
    api.IsWindowVisible.argtypes = [wintypes.HWND]
    api.IsWindowVisible.restype = wintypes.BOOL
    api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    api.GetWindowThreadProcessId.restype = wintypes.DWORD
    api.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    api.GetClassNameW.restype = ctypes.c_int
    # EXSTYLE is a 32-bit value even on 64-bit Windows; no pointer is read here.
    api.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    api.GetWindowLongW.restype = wintypes.LONG
    api.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    api.GetWindowRect.restype = wintypes.BOOL
    api.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    api.SetWindowPos.restype = wintypes.BOOL
    api.SetThreadDpiAwarenessContext.argtypes = [wintypes.HANDLE]
    api.SetThreadDpiAwarenessContext.restype = wintypes.HANDLE
    return api


@contextmanager
def _physical_coordinates(api):
    previous = api.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    try:
        # A null return means the change failed: do not use virtualized pixels.
        yield bool(previous)
    finally:
        if previous:
            api.SetThreadDpiAwarenessContext(previous)


def _valid_id(value, bits):
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 2 ** bits


def _rect(value):
    if isinstance(value, dict):
        parts = tuple(value.get(key) for key in _RECT_KEYS)
    elif isinstance(value, (tuple, list)) and len(value) == 4:
        parts = tuple(value)
    else:
        return None
    if not all(isinstance(part, Real) and not isinstance(part, bool)
               and math.isfinite(part) for part in parts):
        return None
    if parts[2] <= 0 or parts[3] <= 0:
        return None
    return parts


def _read_rect(api, hwnd):
    rect = wintypes.RECT()
    if not api.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return _rect((rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top))


def get_window_rect(hwnd):
    """Return a window's ``(x, y, width, height)`` in physical pixels."""
    if not _valid_id(hwnd, ctypes.sizeof(ctypes.c_void_p) * 8):
        return None
    try:
        api = _native()
        with _physical_coordinates(api) as physical:
            return _read_rect(api, hwnd) if physical else None
    except (AttributeError, OSError, TypeError, ValueError, OverflowError):
        return None


def validate_overlay(hwnd, expected_pid):
    """Check the observed handle still belongs to the visible Codex overlay."""
    if not _valid_id(hwnd, ctypes.sizeof(ctypes.c_void_p) * 8) or not _valid_id(expected_pid, 32):
        return False
    try:
        api = _native()
        if not api.IsWindow(hwnd) or not api.IsWindowVisible(hwnd):
            return False
        pid = wintypes.DWORD()
        if not api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) or pid.value != expected_pid:
            return False
        name = ctypes.create_unicode_buffer(256)
        if not api.GetClassNameW(hwnd, name, len(name)) or name.value != 'Chrome_WidgetWin_1':
            return False
        return api.GetWindowLongW(hwnd, -20) & _OVERLAY_STYLE == _OVERLAY_STYLE
    except (AttributeError, OSError, TypeError, ValueError, OverflowError):
        return False


def move_overlay_by(state, dx, dy):
    """Translate the verified Mini by physical pixels without changing focus."""
    if not isinstance(state, dict):
        return False
    if not all(isinstance(value, Real) and not isinstance(value, bool)
               and math.isfinite(value) for value in (dx, dy)):
        return False
    hwnd, pid = state.get('overlayHwnd'), state.get('pid')
    if not validate_overlay(hwnd, pid):
        return False
    try:
        api = _native()
        with _physical_coordinates(api) as physical:
            if not physical:
                return False
            current = _read_rect(api, hwnd)
            if current is None:
                return False
            x, y = round(current[0] + dx), round(current[1] + dy)
            if not all(-(2 ** 31) <= value < 2 ** 31 for value in (x, y)):
                return False
            if (x, y) == current[:2]:
                return True
            return bool(api.SetWindowPos(hwnd, None, x, y, 0, 0, _MOVE_FLAGS))
    except (AttributeError, OSError, TypeError, ValueError, OverflowError):
        return False


def translated_toolbar_rect(observed_window, live_window, toolbar):
    """Correct stale UIA toolbar geometry using a window's live displacement.

    All inputs must already use physical pixels, including negative desktop
    coordinates. A resized window may have reflowed or crossed a DPI boundary;
    wait for another observer sample instead of guessing its new toolbar offset.
    """
    observed, live, bar = _rect(observed_window), _rect(live_window), _rect(toolbar)
    if observed is None or live is None or bar is None or observed[2:] != live[2:]:
        return None
    return dict(zip(_RECT_KEYS, (bar[0] + live[0] - observed[0],
                                 bar[1] + live[1] - observed[1], bar[2], bar[3])))


def effective_toolbar_rect(state):
    """Return current toolbar geometry, compensating for observer latency."""
    if not isinstance(state, dict):
        return None
    hwnd, pid = state.get('overlayHwnd'), state.get('pid')
    if not validate_overlay(hwnd, pid):
        return None
    live = get_window_rect(hwnd)
    if live is None:
        return None
    return translated_toolbar_rect(state.get('overlayWindowRect'), live, state.get('toolbarRect'))
