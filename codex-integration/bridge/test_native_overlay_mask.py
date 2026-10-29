"""Pure mask ownership/region tests; no real application windows are touched."""
import time
import unittest

from native_overlay_mask import NativeOverlayMask


class FakeApi:
    def __init__(self):
        self.windows = {100: {"pid": 7, "verified": True, "region": None, "cookie": 0},
                        200: {"pid": 7, "verified": True, "region": (0, 0, 20, 20), "cookie": 0}}
        self.handles = {}
        self.next_handle = 1
        self.empty_calls = []
        self.restore_calls = []
        self.fail_empty = False
        self.fail_restore = False
        self.fail_cookie = False

    def same_process(self, hwnd, pid):
        return hwnd in self.windows and self.windows[hwnd]["pid"] == pid

    def matches(self, hwnd, pid):
        return self.same_process(hwnd, pid) and self.windows[hwnd]["verified"]

    def cookie(self, hwnd):
        return self.windows.get(hwnd, {}).get("cookie", 0)

    def set_cookie(self, hwnd, value):
        if self.fail_cookie:
            return False
        self.windows[hwnd]["cookie"] = value
        return True

    def clear_cookie(self, hwnd):
        self.windows[hwnd]["cookie"] = 0

    def capture_region(self, hwnd):
        value = self.windows[hwnd]["region"]
        if value is None:
            return None, 0
        handle = self.next_handle
        self.next_handle += 1
        self.handles[handle] = value
        return handle, 1 if value == () else 2

    def delete_region(self, handle):
        if handle is not None:
            del self.handles[handle]

    def set_empty(self, hwnd):
        self.empty_calls.append(hwnd)
        if self.fail_empty:
            return False
        self.windows[hwnd]["region"] = ()
        return True

    def restore_region(self, hwnd, region):
        self.restore_calls.append(hwnd)
        if self.fail_restore:
            return False
        self.windows[hwnd]["region"] = self.handles.pop(region) if region is not None else None
        return True


class MaskTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.mask = NativeOverlayMask(self.api, watch_interval=None)
        self.addCleanup(self.mask.close)

    def test_no_region_restored_exactly_without_visibility_or_geometry_calls(self):
        self.assertTrue(self.mask.mask(100, 7))
        self.assertTrue(self.mask.is_masked(100, 7))
        self.assertTrue(self.mask.restore())
        self.assertIsNone(self.api.windows[100]["region"])
        self.assertFalse(self.api.windows[100]["cookie"])
        self.assertFalse(self.api.handles)

    def test_existing_region_and_shape_changes_are_restored(self):
        self.assertTrue(self.mask.mask(200, 7))
        self.assertTrue(self.mask.restore())
        self.assertEqual(self.api.windows[200]["region"], (0, 0, 20, 20))
        self.assertTrue(self.mask.mask(200, 7))
        self.api.windows[200]["region"] = (0, 0, 80, 90)
        self.assertTrue(self.mask.mask(200, 7))
        self.assertTrue(self.mask.restore())
        self.assertEqual(self.api.windows[200]["region"], (0, 0, 80, 90))
        self.assertFalse(self.api.handles)

    def test_repeated_mask_does_not_call_set_window_region_again(self):
        for _ in range(5):
            self.assertTrue(self.mask.mask(100, 7))
        self.assertEqual(self.api.empty_calls, [100])

    def test_diagnostic_counters_distinguish_reapply_from_restore_cycles(self):
        self.mask.mask(100, 7)
        self.mask.mask(100, 7)
        self.assertEqual(self.mask.status()["maskCount"], 1)
        self.assertEqual(self.mask.status()["reapplyCount"], 0)
        self.api.windows[100]["region"] = (0, 0, 50, 50)
        self.mask.mask(100, 7)
        self.assertEqual(self.mask.status()["reapplyCount"], 1)
        self.mask.restore()
        self.assertEqual(self.mask.status()["restoreCount"], 1)
        self.mask.mask(100, 7)
        self.assertEqual(self.mask.status()["maskCount"], 2)

    def test_unverified_pid_main_window_and_invalid_target_are_rejected(self):
        self.api.windows[200]["verified"] = False
        for hwnd, pid in ((100, 8), (200, 7), (0, 7), (None, None)):
            self.assertFalse(self.mask.mask(hwnd, pid))
        self.assertEqual(self.api.empty_calls, [])

    def test_retarget_restores_before_masking_new_window(self):
        self.mask.mask(200, 7)
        self.mask.mask(100, 7)
        self.assertEqual(self.api.restore_calls, [200])
        self.assertEqual(self.api.windows[200]["region"], (0, 0, 20, 20))
        self.assertTrue(self.mask.is_masked(100, 7))

    def test_window_reused_by_same_pid_without_cookie_is_not_restored(self):
        self.mask.mask(200, 7)
        self.api.windows[200] = {"pid": 7, "verified": True, "region": (9, 9, 99, 99), "cookie": 0}
        self.assertTrue(self.mask.restore())
        self.assertEqual(self.api.windows[200]["region"], (9, 9, 99, 99))
        self.assertEqual(self.api.restore_calls, [])
        self.assertFalse(self.api.handles)

    def test_foreign_mask_owner_is_not_overwritten(self):
        self.api.windows[100]["cookie"] = 444
        self.assertFalse(self.mask.mask(100, 7))
        self.assertEqual(self.api.empty_calls, [])
        self.assertEqual(self.api.windows[100]["cookie"], 444)

    def test_mask_and_owner_failures_leave_original_and_no_handle_leaks(self):
        self.api.fail_cookie = True
        self.assertFalse(self.mask.mask(200, 7))
        self.api.fail_cookie = False
        self.api.fail_empty = True
        self.assertFalse(self.mask.mask(200, 7))
        self.assertFalse(self.api.windows[200]["cookie"])
        self.assertEqual(self.api.windows[200]["region"], (0, 0, 20, 20))
        self.assertFalse(self.api.handles)

    def test_failed_restore_can_be_retried_without_losing_saved_region(self):
        self.mask.mask(200, 7)
        self.api.fail_restore = True
        self.assertFalse(self.mask.restore())
        self.assertTrue(self.mask.is_masked())
        self.api.fail_restore = False
        self.assertTrue(self.mask.restore())
        self.assertEqual(self.api.windows[200]["region"], (0, 0, 20, 20))

    def test_app_replaced_region_is_not_overwritten_on_release(self):
        self.mask.mask(100, 7)
        self.api.windows[100]["region"] = (1, 2, 100, 200)
        self.assertTrue(self.mask.restore())
        self.assertEqual(self.api.windows[100]["region"], (1, 2, 100, 200))
        self.assertEqual(self.api.restore_calls, [])

    def test_owned_region_can_restore_even_if_topmost_style_changed(self):
        self.mask.mask(200, 7)
        self.api.windows[200]["verified"] = False
        self.assertTrue(self.mask.restore())
        self.assertEqual(self.api.windows[200]["region"], (0, 0, 20, 20))

    def test_keeper_repairs_known_window_only_and_close_restores(self):
        mask = NativeOverlayMask(self.api, watch_interval=0.005)
        self.addCleanup(mask.close)
        mask.mask(100, 7)
        self.api.windows[100]["region"] = (0, 0, 50, 50)
        deadline = time.monotonic() + 1
        while not mask.is_masked() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(mask.is_masked())
        self.assertTrue(mask.close())
        self.assertEqual(self.api.windows[100]["region"], (0, 0, 50, 50))
        self.assertFalse(mask.mask(100, 7))


if __name__ == "__main__":
    unittest.main()
