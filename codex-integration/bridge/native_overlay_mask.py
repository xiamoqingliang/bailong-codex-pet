"""Reversibly clip a verified native Mini without changing its show/hide state.

Only the HWND identified by the observer is eligible. No window is discovered,
moved, resized, hidden, closed or restyled here. An empty Win32 region suppresses
both drawing and hit testing while WS_VISIBLE and Electron's isVisible remain
under Codex's control. The caller must restore when leaving full-pet mode.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import secrets
import threading


PROPERTY_NAME = "BailongNativeMiniRegionOwnerV1"


class Win32RegionApi:
    def __init__(self, window_class="Chrome_WidgetWin_1"):
        if os.name != "nt":
            raise OSError("Native Mini masking requires Windows")
        self.window_class = window_class
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        self.user.IsWindow.argtypes = (wintypes.HWND,)
        self.user.IsWindow.restype = wintypes.BOOL
        self.user.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        self.user.GetClassNameW.restype = ctypes.c_int
        self.user.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
        self.user.GetWindowLongW.restype = ctypes.c_long
        self.user.GetPropW.argtypes = (wintypes.HWND, wintypes.LPCWSTR)
        self.user.GetPropW.restype = wintypes.HANDLE
        self.user.SetPropW.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.HANDLE)
        self.user.SetPropW.restype = wintypes.BOOL
        self.user.RemovePropW.argtypes = (wintypes.HWND, wintypes.LPCWSTR)
        self.user.RemovePropW.restype = wintypes.HANDLE
        self.user.GetWindowRgn.argtypes = (wintypes.HWND, wintypes.HANDLE)
        self.user.GetWindowRgn.restype = ctypes.c_int
        self.user.SetWindowRgn.argtypes = (wintypes.HWND, wintypes.HANDLE, wintypes.BOOL)
        self.user.SetWindowRgn.restype = ctypes.c_int
        self.gdi.CreateRectRgn.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)
        self.gdi.CreateRectRgn.restype = wintypes.HANDLE
        self.gdi.DeleteObject.argtypes = (wintypes.HANDLE,)
        self.gdi.DeleteObject.restype = wintypes.BOOL

    def same_process(self, hwnd, pid):
        if type(hwnd) is not int or hwnd <= 0 or type(pid) is not int or pid <= 0:
            return False
        if not self.user.IsWindow(hwnd):
            return False
        actual_pid = wintypes.DWORD()
        self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(actual_pid))
        return actual_pid.value == pid

    def matches(self, hwnd, pid):
        if not self.same_process(hwnd, pid):
            return False
        name = ctypes.create_unicode_buffer(256)
        if not self.user.GetClassNameW(hwnd, name, len(name)) or name.value != self.window_class:
            return False
        extended = self.user.GetWindowLongW(hwnd, -20) & 0xFFFFFFFF
        style = self.user.GetWindowLongW(hwnd, -16) & 0xFFFFFFFF
        # Native Mini is a topmost tool window, not a captioned main/child window.
        return extended & 0x88 == 0x88 and not style & (0x40000000 | 0x00C00000)

    def cookie(self, hwnd):
        return int(self.user.GetPropW(hwnd, PROPERTY_NAME) or 0)

    def set_cookie(self, hwnd, value):
        return bool(self.user.SetPropW(hwnd, PROPERTY_NAME, value))

    def clear_cookie(self, hwnd):
        self.user.RemovePropW(hwnd, PROPERTY_NAME)

    def capture_region(self, hwnd):
        region = self.gdi.CreateRectRgn(0, 0, 0, 0)
        if not region:
            raise OSError("CreateRectRgn failed")
        kind = self.user.GetWindowRgn(hwnd, region)
        if kind == 0:
            self.delete_region(region)
            return None, 0  # A valid HWND with no explicit window region.
        return region, kind

    def delete_region(self, region):
        if region:
            self.gdi.DeleteObject(region)

    def set_empty(self, hwnd):
        region = self.gdi.CreateRectRgn(0, 0, 0, 0)
        if not region:
            return False
        if self.user.SetWindowRgn(hwnd, region, True):
            return True  # Region ownership transfers to Windows on success.
        self.delete_region(region)
        return False

    def restore_region(self, hwnd, region):
        # A successful call transfers a non-null saved region to Windows.
        return bool(self.user.SetWindowRgn(hwnd, region, True))


class NativeOverlayMask:
    """Own at most one observer-verified Mini region; safe to query from threads.

    mask(hwnd, pid) is called only while the dedicated full-pet entry is selected.
    restore() must be called on selection exit, stale source, or shutdown.
    A lightweight in-process keeper reapplies the mask if Codex resets the region
    during layout. It never widens the target beyond the known HWND.
    """
    def __init__(self, api=None, watch_interval=0.02):
        self.api = api or Win32RegionApi()
        self.watch_interval = watch_interval
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._token = secrets.randbits(31) or 1
        self._hwnd = None
        self._pid = None
        self._saved_region = None
        self._owned = False
        self._desired = False
        self._error = None
        self._closed = False
        self._mask_count = 0
        self._reapply_count = 0
        self._restore_count = 0

    def _start_keeper(self):
        if self.watch_interval is not None and self._thread is None:
            self._thread = threading.Thread(target=self._keep, name="bailong-mini-mask", daemon=True)
            self._thread.start()

    def _keep(self):
        while not self._stop.wait(self.watch_interval):
            with self._lock:
                if self._desired and self._owned:
                    try:
                        self._ensure()
                    except Exception as error:
                        self._error = type(error).__name__

    def _valid_owner(self):
        return (self._owned and self.api.same_process(self._hwnd, self._pid)
                and self.api.cookie(self._hwnd) == self._token)

    def _forget(self):
        self.api.delete_region(self._saved_region)
        self._hwnd = self._pid = self._saved_region = None
        self._owned = self._desired = False

    def _ensure(self):
        if not self._valid_owner():
            self._forget()
            self._error = "target-gone-or-replaced"
            return False
        current, kind = self.api.capture_region(self._hwnd)
        if kind == 1:
            self.api.delete_region(current)
            self._error = None
            return True
        # Preserve a new region requested by the app, e.g. after a resize. This
        # prevents restoring a stale size-specific shape when the user switches.
        self.api.delete_region(self._saved_region)
        self._saved_region = current
        if not self.api.set_empty(self._hwnd):
            self._error = "set-region-failed"
            return False
        self._error = None
        self._reapply_count += 1
        return True

    def mask(self, hwnd, pid):
        with self._lock:
            if self._closed or not self.api.matches(hwnd, pid):
                self._error = "target-not-verified"
                return False
            if self._owned and (hwnd, pid) == (self._hwnd, self._pid):
                self._desired = True
                return self._ensure()
            if self._owned and not self.restore():
                return False
            if self.api.cookie(hwnd):
                self._error = "window-owned-by-another-mask"
                return False
            saved, _ = self.api.capture_region(hwnd)
            if not self.api.set_cookie(hwnd, self._token):
                self.api.delete_region(saved)
                self._error = "set-owner-failed"
                return False
            if not self.api.set_empty(hwnd):
                if self.api.cookie(hwnd) == self._token:
                    self.api.clear_cookie(hwnd)
                self.api.delete_region(saved)
                self._error = "set-region-failed"
                return False
            self._hwnd, self._pid, self._saved_region = hwnd, pid, saved
            self._owned = self._desired = True
            self._mask_count += 1
            self._error = None
            self._start_keeper()
            return True

    def restore(self):
        with self._lock:
            self._desired = False
            if not self._owned:
                return True
            if not self._valid_owner():
                self._forget()
                return True  # Never modify a destroyed/reused/foreign HWND.
            current, kind = self.api.capture_region(self._hwnd)
            self.api.delete_region(current)
            if kind != 1:
                # The application has already replaced our empty region.
                self.api.clear_cookie(self._hwnd)
                self._forget()
                self._error = None
                return True
            if not self.api.restore_region(self._hwnd, self._saved_region):
                self._error = "restore-region-failed"
                return False
            self._saved_region = None  # OS now owns it; do not DeleteObject.
            self._restore_count += 1
            self.api.clear_cookie(self._hwnd)
            self._forget()
            self._error = None
            return True

    def is_masked(self, hwnd=None, pid=None):
        with self._lock:
            if hwnd is not None and (hwnd, pid) != (self._hwnd, self._pid):
                return False
            if not self._valid_owner():
                return False
            region, kind = self.api.capture_region(self._hwnd)
            self.api.delete_region(region)
            return kind == 1

    def status(self):
        with self._lock:
            return {"hwnd": self._hwnd, "pid": self._pid, "masked": self.is_masked(),
                    "desired": self._desired, "error": self._error,
                    "maskCount": self._mask_count, "reapplyCount": self._reapply_count,
                    "restoreCount": self._restore_count}

    def close(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        with self._lock:
            restored = self.restore()
            self._closed = True
            return restored
