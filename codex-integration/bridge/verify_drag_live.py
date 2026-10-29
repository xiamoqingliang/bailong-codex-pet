"""Bounded drag check for the full pet and its own attached control bar."""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import time

from mini_position import get_window_rect

BASE = Path(__file__).resolve().parent
api = ctypes.WinDLL('user32', use_last_error=True)
api.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
api.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
api.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
api.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
api.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
api.WindowFromPoint.argtypes = [wintypes.POINT]
api.WindowFromPoint.restype = wintypes.HWND
api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
api.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]


def read(name):
    return json.loads((BASE / name).read_text('utf-8-sig'))


def anchor():
    state = json.loads((Path.home() / '.codex/.codex-global-state.json').read_text('utf-8'))
    return state['electron-avatar-overlay-bounds']


def main():
    initial = read('pet-status.json')
    observed = read('codex-state.json')
    before = get_window_rect(initial['windowId'])
    anchor_before = anchor()
    bar_before = get_window_rect(initial['toolbarWindowId'])
    assert initial['visible'] and initial['toolbarVisible'] and initial['fullSelected'], 'Full pet is hidden; no input sent.'
    assert not api.GetAsyncKeyState(1) & 0x8000, 'Mouse already held; no input sent.'
    old = wintypes.POINT()
    api.GetCursorPos(ctypes.byref(old))
    start = (before[0] + before[2] // 2, before[1] + round(before[3] * 0.46))
    interfered = False
    pressed = False
    try:
        api.SetCursorPos(*start)
        time.sleep(0.1)
        owner = wintypes.DWORD()
        api.GetWindowThreadProcessId(api.WindowFromPoint(wintypes.POINT(*start)), ctypes.byref(owner))
        assert owner.value == initial['pid'], 'Pet not under pointer; no press sent.'
        api.mouse_event(2, 0, 0, 0, 0)
        pressed = True
        time.sleep(0.12)
        last = start
        for index in range(1, 13):
            actual = wintypes.POINT()
            api.GetCursorPos(ctypes.byref(actual))
            if abs(actual.x - last[0]) + abs(actual.y - last[1]) > 12:
                interfered = True
                raise RuntimeError('User moved pointer; test stopped.')
            last = (start[0] - 15 * index, start[1] - 5 * index)
            api.SetCursorPos(*last)
            time.sleep(0.07)
    finally:
        if pressed:
            api.mouse_event(4, 0, 0, 0, 0)
            time.sleep(0.1)
        if not interfered:
            api.SetCursorPos(old.x, old.y)
    time.sleep(1.8)
    current = read('pet-status.json')
    after = get_window_rect(current['windowId'])
    bar_after = get_window_rect(current['toolbarWindowId'])
    anchor_after = anchor()
    delta = [after[0] - before[0], after[1] - before[1]]
    mini_delta = [bar_after[0] - bar_before[0], bar_after[1] - bar_before[1]]
    result = {
        'requestedDelta': [-180, -60], 'petDelta': delta,
        'dedicatedToolbarDelta': mini_delta,
        'nativeMiniAnchorUnchanged': all(anchor_before[key] == anchor_after[key] for key in ('x', 'y')),
        'toolbarGapPixels': bar_after[1] - after[1] - after[3],
    }
    result['passed'] = result['nativeMiniAnchorUnchanged'] and all(
        abs(value - expected) <= 4
        for values in (delta, mini_delta)
        for value, expected in zip(values, [-180, -60])
    )
    (BASE / 'drag-live-test.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result))
    assert result['passed'], 'Drag synchronization live test failed.'


if __name__ == '__main__':
    main()
