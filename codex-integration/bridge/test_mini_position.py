"""Pure and mocked checks: no real windows are inspected or moved."""
import ctypes
import unittest
from unittest.mock import patch

import mini_position as position


class ToolbarTranslationTests(unittest.TestCase):
    def test_150_percent_screen_uses_physical_delta_without_rescaling(self):
        result = position.translated_toolbar_rect(
            (1800, 300, 750, 600), (1950, 375, 750, 600),
            {'x': 1920, 'y': 820, 'width': 496, 'height': 60})
        self.assertEqual(result, {'x': 2070, 'y': 895, 'width': 496, 'height': 60})

    def test_negative_monitor_coordinates_and_fractional_toolbar(self):
        result = position.translated_toolbar_rect(
            {'x': 100, 'y': 20, 'width': 800, 'height': 500},
            (-1700, -500, 800, 500), (135.5, 451.25, 496, 60))
        self.assertEqual(result, {'x': -1664.5, 'y': -68.75, 'width': 496, 'height': 60})

    def test_window_resize_waits_for_fresh_layout(self):
        for live in ((0, 0, 801, 500), (0, 0, 800, 501), (0, 0, 1200, 750)):
            self.assertIsNone(position.translated_toolbar_rect((0, 0, 800, 500), live, (20, 400, 496, 60)))

    def test_invalid_or_incomplete_geometry_is_rejected(self):
        for value in (None, {}, (0, 0, 0, 50), (0, 0, 50, -1),
                      (float('nan'), 0, 50, 50), (0, float('inf'), 50, 50),
                      ('10', 0, 50, 50), (True, 0, 50, 50)):
            with self.subTest(value=value):
                self.assertIsNone(position.translated_toolbar_rect(value, (0, 0, 50, 50), (0, 0, 10, 10)))


class FakeNative:
    def __init__(self):
        self.exists = True
        self.visible = True
        self.pid = 1234
        self.name = 'Chrome_WidgetWin_1'
        self.style = 0x88
        self.context_calls = []
        self.moves = []
        self.rect = (-1200, 100, -400, 600)
        self.read_success = True
        self.set_success = True
        self.context_success = True
        self.raise_on_move = False

    def IsWindow(self, hwnd):
        return self.exists

    def IsWindowVisible(self, hwnd):
        return self.visible

    def GetWindowThreadProcessId(self, hwnd, pid):
        pid._obj.value = self.pid
        return 101

    def GetClassNameW(self, hwnd, name, count):
        name.value = self.name
        return len(self.name)

    def GetWindowLongW(self, hwnd, index):
        return self.style

    def SetThreadDpiAwarenessContext(self, context):
        value = context.value if isinstance(context, ctypes.c_void_p) else context
        self.context_calls.append(value)
        return 0x42 if self.context_success else None

    def GetWindowRect(self, hwnd, rect):
        rect._obj.left, rect._obj.top, rect._obj.right, rect._obj.bottom = self.rect
        return self.read_success

    def SetWindowPos(self, *arguments):
        if self.raise_on_move:
            raise OSError('simulated handle failure')
        self.moves.append(arguments)
        return self.set_success


class NativeIsolationTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeNative()
        self.native_patch = patch.object(position, '_native', return_value=self.api)
        self.native_patch.start()
        self.addCleanup(self.native_patch.stop)
        self.state = {'overlayHwnd': 0x100001234, 'pid': 1234,
                      'overlayWindowRect': {'x': -1200, 'y': 100, 'width': 800, 'height': 500},
                      'toolbarRect': {'x': -1100, 'y': 500, 'width': 496, 'height': 60}}

    def assert_context_restored(self):
        self.assertEqual(self.api.context_calls, [ctypes.c_void_p(-4).value, 0x42])

    def test_read_physical_rect_restores_calling_threads_dpi_context(self):
        self.assertEqual(position.get_window_rect(self.state['overlayHwnd']), (-1200, 100, 800, 500))
        self.assert_context_restored()
        self.assertEqual(self.api.moves, [])

    def test_handle_pid_class_visibility_and_styles_must_all_match(self):
        self.assertTrue(position.validate_overlay(self.state['overlayHwnd'], 1234))
        for attr, value in (('exists', False), ('visible', False), ('pid', 5678),
                            ('name', 'OtherWindow'), ('style', 0x08), ('style', 0x80)):
            with self.subTest(attr=attr, value=value):
                original = getattr(self.api, attr)
                setattr(self.api, attr, value)
                self.assertFalse(position.move_overlay_by(self.state, 90, 60))
                setattr(self.api, attr, original)
        self.assertEqual(self.api.moves, [])

    def test_move_keeps_focus_size_and_z_order_and_restores_dpi_context(self):
        self.assertTrue(position.move_overlay_by(self.state, 90, -60))
        self.assertEqual(self.api.moves, [(self.state['overlayHwnd'], None, -1110, 40, 0, 0, 0x215)])
        self.assert_context_restored()

    def test_failed_move_also_restores_dpi_context(self):
        self.api.raise_on_move = True
        self.assertFalse(position.move_overlay_by(self.state, 10, 10))
        self.assert_context_restored()

    def test_failed_dpi_switch_never_reads_or_moves_in_virtualized_coordinates(self):
        self.api.context_success = False
        self.assertIsNone(position.get_window_rect(self.state['overlayHwnd']))
        self.assertFalse(position.move_overlay_by(self.state, 10, 10))
        self.assertEqual(self.api.moves, [])

    def test_missing_window_rect_does_not_move(self):
        self.api.read_success = False
        self.assertFalse(position.move_overlay_by(self.state, 10, 10))
        self.assertEqual(self.api.moves, [])
        self.assert_context_restored()

    def test_effective_toolbar_uses_live_delta(self):
        self.api.rect = (-1100, 150, -300, 650)
        self.assertEqual(position.effective_toolbar_rect(self.state),
                         {'x': -1000, 'y': 550, 'width': 496, 'height': 60})
        self.assert_context_restored()

    def test_effective_toolbar_rejects_reused_handle(self):
        self.api.pid = 9999
        self.assertIsNone(position.effective_toolbar_rect(self.state))
        self.assertEqual(self.api.context_calls, [])

    def test_invalid_handles_and_deltas_are_never_moved(self):
        for handle in (None, True, -1, 0, '123', 2 ** 65):
            self.assertFalse(position.move_overlay_by({**self.state, 'overlayHwnd': handle}, 10, 10))
        for delta in (float('inf'), float('nan'), None, True, '1'):
            self.assertFalse(position.move_overlay_by(self.state, delta, 10))
        self.assertEqual(self.api.moves, [])


if __name__ == '__main__':
    unittest.main()
