"""Native toggle policy tests with invented observations; no Codex is accessed."""
from dataclasses import dataclass, replace
import io
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest import mock

import native_visibility as visibility


@dataclass(frozen=True)
class FakeObservation:
    running: bool = True
    overlay_open: bool = True
    pid: int = 101
    full_selected: bool = True
    selection_known: bool = True
    selected_avatar_id: str = "custom:bailong-full"
    native_pet_visible: bool = True
    overlay_hwnd: int | None = 202
    requested_open: bool | None = True
    app_started_at: float | None = None


class FakeMask:
    def __init__(self):
        self.calls = []
        self.restore_calls = 0
        self.closed = False
        self.current = {"hwnd": None, "pid": None, "masked": False, "error": None}
        self.fail = False

    def mask(self, hwnd, pid):
        self.calls.append((hwnd, pid))
        self.current = {"hwnd": hwnd, "pid": pid, "masked": not self.fail,
                        "error": "test-failure" if self.fail else None}
        return not self.fail

    def restore(self):
        self.restore_calls += 1
        self.current = {"hwnd": None, "pid": None, "masked": False, "error": None}
        return True

    def status(self):
        return dict(self.current)

    def close(self):
        self.closed = True
        self.restore()


class FakeProcess:
    pid = 303
    def __init__(self):
        self.stdout = io.BytesIO(b'{"acted":true,"method":"native-ui"}')
        self.result = None
        self.terminated = False

    def poll(self):
        return self.result

    def terminate(self):
        self.terminated = True
        self.result = -1

    def wait(self, timeout=None):
        return self.result


class NativeVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bailong-native-toggle-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.clock = mock.patch.object(visibility.time, "time", return_value=10000.0)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        writer = visibility._write
        def timed_write(path, value):
            writer(path, value)
            os.utime(path, (10000.0, 10000.0))
        writer_patch = mock.patch.object(visibility, "_write", side_effect=timed_write)
        writer_patch.start()
        self.addCleanup(writer_patch.stop)
        self.mask = FakeMask()
        self.launched = []
        def launch(command, base):
            process = FakeProcess()
            self.launched.append((command, process))
            return process
        self.controller = visibility.NativeMiniController(
            self.base, mock.Mock(), launcher=launch,
            command_builder=lambda base, action, observation: (action, observation.pid, observation.selected_avatar_id),
            mask=self.mask,
        )
        self.addCleanup(self.controller.close)
        self.observation = FakeObservation()

    def request(self, requested_visible, **overrides):
        value = {"id": "request-" + str(len(self.launched)), "at": 10000.0,
                 "pid": self.observation.pid, "selectedAvatarId": self.observation.selected_avatar_id,
                 "visible": requested_visible}
        value.update(overrides)
        visibility._write(self.base / visibility.REQUEST_NAME, value)

    def test_visible_native_is_masked_without_automatic_hide(self):
        for now in (1, 2, 10):
            self.controller.tick(self.observation, now)
        self.assertEqual(self.launched, [])
        self.assertEqual(self.mask.calls, [(202, 101)] * 3)
        self.assertEqual(self.controller.phase, "following-native-toggle")

    def test_cold_start_waits_only_for_proven_no_input_readiness_and_is_bounded(self):
        hidden = replace(self.observation, overlay_open=False, requested_open=True)
        self.controller.tick(hidden, 1)
        for now in (2, 5, 8, 11, 14, 17, 20, 23, 26, 29, 32):
            process = self.launched[-1][1]
            process.stdout = io.BytesIO(b'{"acted":false,"reason":"main-window-not-ready"}')
            process.result = 0
            self.controller.tick(hidden, now)
            self.controller.tick(hidden, now + 1.1)
        self.assertIsNone(self.controller.status()["desired"])
        count = len(self.launched)
        self.controller.tick(hidden, 100)
        self.assertEqual(len(self.launched), count)

    def test_successful_scoped_show_ack_masks_its_new_window_before_observer_uia(self):
        hidden = replace(self.observation, overlay_open=False, overlay_hwnd=None)
        self.controller.tick(hidden, 1)
        process = self.launched[0][1]
        process.stdout = io.BytesIO(b'{"acted":true,"method":"global-hotkey","openedOverlayHwnd":909}')
        process.result = 0
        self.controller.tick(hidden, 2)
        self.assertIn((909, 101), self.mask.calls)

    def test_native_hide_is_followed_without_automatic_reopen(self):
        self.controller.tick(self.observation, 1)
        hidden = replace(self.observation, overlay_open=False, requested_open=False)
        self.controller.tick(hidden, 2)
        self.controller.tick(hidden, 50)
        self.assertEqual(self.launched, [])
        self.assertTrue(self.mask.status()["masked"])

    def test_new_codex_session_reopens_after_manual_hide_or_exit(self):
        self.controller.tick(self.observation, 1)
        hidden = replace(self.observation, overlay_open=False, requested_open=False)
        self.controller.tick(hidden, 2)
        self.assertEqual(self.launched, [])
        self.controller.tick(replace(hidden, running=False, pid=None), 3)
        self.controller.tick(replace(hidden, pid=102), 4)
        self.assertEqual(self.launched[0][0][0], "Show")
        self.assertNotIn("-OnlyWhenPersistedOpen", self.launched[0][0])
        self.launched[0][1].result = 0
        self.controller.tick(replace(hidden, pid=102), 5)
        self.controller.tick(replace(hidden, pid=102), 20)
        self.assertEqual(len(self.launched), 1)

    def test_replacement_gui_reopens_even_if_offline_interval_was_not_observed(self):
        self.controller.tick(self.observation, 1)
        hidden = replace(self.observation, overlay_open=False, requested_open=False)
        self.controller.tick(replace(hidden, pid=102), 2)
        self.assertEqual(self.launched[0][0][0], "Show")

    def test_learning_or_temporarily_missing_start_time_does_not_reopen_same_gui(self):
        hidden = replace(self.observation, overlay_open=False, requested_open=False)
        self.controller.tick(hidden, 1)
        self.controller.tick(replace(hidden, app_started_at=100), 2)
        self.controller.tick(hidden, 3)
        self.assertEqual(self.launched, [])
        self.controller.tick(replace(hidden, app_started_at=200), 4)
        self.assertEqual(self.launched[0][0][0], "Show")

    def test_other_pet_in_new_gui_is_not_replaced_by_full_companion(self):
        self.controller.tick(self.observation, 1)
        other = replace(self.observation, pid=102, full_selected=False, selected_avatar_id="other", overlay_open=False)
        self.controller.tick(other, 2)
        self.assertEqual(self.launched, [])

    def test_persistent_open_intent_restores_once_and_respects_later_hide(self):
        hidden = replace(self.observation, overlay_open=False, requested_open=True)
        self.controller.tick(hidden, 1)
        self.assertEqual(self.launched[0][0][0], "Show")
        self.controller.tick(hidden, 2)
        self.assertEqual(len(self.launched), 1)
        self.launched[0][1].result = 0
        self.controller.tick(self.observation, 3)
        self.controller.tick(replace(hidden, requested_open=False), 4)
        self.controller.tick(replace(hidden, requested_open=False), 30)
        self.assertEqual(len(self.launched), 1)
        self.assertIsNone(self.controller.status()["desired"])

    def test_false_or_unknown_persistent_intent_does_not_show(self):
        for intent in (False, None):
            self.controller.tick(replace(self.observation, overlay_open=False, requested_open=intent), 1)
        self.assertEqual(self.launched, [])

    def test_explicit_installation_shows_once_even_if_previous_native_intent_was_hidden(self):
        controller = visibility.NativeMiniController(
            self.base, mock.Mock(), launcher=self.controller.launcher,
            command_builder=self.controller.command_builder, mask=FakeMask(), show_on_start=True)
        self.addCleanup(controller.close)
        hidden = replace(self.observation, overlay_open=False, requested_open=False)
        controller.tick(hidden, 1)
        self.assertEqual(self.launched[0][0][0], "Show")
        self.assertNotIn("-OnlyWhenPersistedOpen", self.launched[0][0])
        self.launched[0][1].result = 0
        controller.tick(hidden, 2)
        controller.tick(hidden, 20)
        self.assertEqual(len(self.launched), 1)

    def test_acknowledged_show_is_not_replayed_if_open_was_not_sampled(self):
        hidden = replace(self.observation, overlay_open=False, requested_open=True)
        self.controller.tick(hidden, 1)
        self.launched[0][1].result = 0
        self.controller.tick(hidden, 2)
        self.controller.tick(hidden, 20)
        self.assertEqual(len(self.launched), 1)

    def test_explicit_native_hide_cancels_unobserved_startup_show(self):
        hidden = replace(self.observation, overlay_open=False, requested_open=True)
        self.controller.tick(hidden, 1)
        # The UI can show/hide between observer samples; the persistent false
        # intent must still cancel startup recovery instead of reopening it.
        self.launched[0][1].result = 0
        hidden = replace(hidden, requested_open=False)
        self.controller.tick(hidden, 2)
        self.controller.tick(hidden, 5)
        self.assertEqual(len(self.launched), 1)
        self.assertIsNone(self.controller.status()["desired"])

    def test_toolbar_hide_and_show_are_explicit_native_actions(self):
        self.controller.tick(self.observation, 1)
        self.request(False)
        self.controller.tick(self.observation, 2)
        self.assertEqual(self.launched[0][0][0], "Hide")
        self.launched[0][1].result = 0
        hidden = replace(self.observation, overlay_open=False, requested_open=False)
        self.controller.tick(hidden, 3)
        self.request(True)
        self.controller.tick(hidden, 4)
        self.assertEqual([item[0][0] for item in self.launched], ["Hide", "Show"])

    def test_opposite_request_cancels_old_helper_before_it_can_hide_later(self):
        self.controller.tick(self.observation, 1)
        self.request(False, id="hide")
        self.controller.tick(self.observation, 2)
        pending_hide = self.launched[0][1]
        self.request(True, id="show")
        self.controller.tick(self.observation, 2.2)
        self.assertTrue(pending_hide.terminated)
        self.assertIsNone(self.controller.process)
        self.assertIsNone(self.controller.status()["desired"])
        self.controller.tick(self.observation, 30)
        self.assertEqual(len(self.launched), 1)

    def test_hung_helper_expires_without_replaying_or_blocking_new_request(self):
        self.controller.tick(self.observation, 1)
        self.request(False)
        self.controller.tick(self.observation, 2)
        process = self.launched[0][1]
        self.controller.tick(self.observation, 8.1)
        self.assertTrue(process.terminated)
        self.assertEqual(self.controller.last_result["reason"], "helper-timeout-no-replay")
        self.controller.tick(self.observation, 20)
        self.assertEqual(len(self.launched), 1)
        self.request(False, id="new-user-hide")
        self.controller.tick(self.observation, 21)
        self.assertEqual(len(self.launched), 2)

    def test_ambiguous_delivery_never_replays_automatic_show(self):
        hidden = replace(self.observation, overlay_open=False, requested_open=True)
        self.controller.tick(hidden, 1)
        process = self.launched[0][1]
        process.stdout = io.BytesIO(b'{"acted":false,"method":"uia-invoke","reason":"invoke-failed-or-uncertain"}')
        process.result = 1
        self.controller.tick(hidden, 2)
        self.controller.tick(hidden, 30)
        self.assertEqual(len(self.launched), 1)
        self.assertIsNone(self.controller.status()["desired"])

    def test_changed_host_or_selection_cancels_obsolete_helper(self):
        for change in ({"running": False}, {"pid": 102}, {"full_selected": False}):
            self.controller.tick(self.observation, 1)
            self.request(False, id="hide-" + str(change))
            self.controller.tick(self.observation, 2)
            process = self.launched[-1][1]
            self.controller.tick(replace(self.observation, **change), 2.1)
            self.assertTrue(process.terminated)

    def test_real_owned_helper_timeout_stops_process_without_touching_ui(self):
        # A disposable Python child stands in for a blocked UIA provider. It
        # receives no handles, keys, app paths or access to the real desktop.
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                   stdout=subprocess.PIPE,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        self.controller.process = process
        self.controller.pending_action = "Hide"
        self.controller._process_started = 1.0
        self.controller._process_selection = (101, "custom:bailong-full")
        self.controller._desired = False
        self.controller.tick(self.observation, 8.0)
        self.assertIsNotNone(process.poll())
        self.assertIsNone(self.controller.process)
        self.assertIsNone(self.controller.status()["desired"])

    def test_request_requires_fresh_matching_pid_selection_and_boolean(self):
        hidden = replace(self.observation, overlay_open=False, requested_open=False)
        self.controller.tick(hidden, 1)
        invalid = [{"at": 9990.0}, {"at": 10001.0}, {"pid": 999},
                   {"selectedAvatarId": "custom:other"}, {"visible": "true"}]
        for index, overrides in enumerate(invalid):
            self.request(True, id="bad-" + str(index), **overrides)
            self.controller.tick(hidden, 2 + index)
        self.assertEqual(self.launched, [])
        self.request(True, id="good")
        self.controller.tick(hidden, 20)
        self.assertEqual(self.launched[0][0][0], "Show")

    def test_old_request_file_is_not_replayed_on_controller_start(self):
        self.request(False, id="previous-run")
        controller = visibility.NativeMiniController(self.base, mock.Mock(),
            launcher=mock.Mock(), command_builder=mock.Mock(), mask=FakeMask())
        self.addCleanup(controller.close)
        controller.tick(self.observation, 1)
        controller.launcher.assert_not_called()

    def test_switch_away_or_disconnected_restores_mask_immediately(self):
        for other in (replace(self.observation, full_selected=False, selected_avatar_id="custom:other"),
                      replace(self.observation, running=False)):
            self.controller.tick(self.observation, 1)
            count = self.mask.restore_calls
            self.controller.tick(other, 2)
            self.assertEqual(self.mask.restore_calls, count + 1)
            self.assertFalse(self.mask.status()["masked"])
        self.assertEqual(self.launched, [])

    def test_short_none_or_unknown_keeps_mask_without_any_action(self):
        self.controller.tick(self.observation, 1)
        count = self.mask.restore_calls
        self.controller.tick(None, 1.1)
        self.controller.tick(replace(self.observation, selection_known=False), 1.5)
        self.assertEqual(self.mask.restore_calls, count)
        self.assertTrue(self.mask.status()["masked"])
        self.assertEqual(self.controller.phase, "waiting-for-fresh-selection")
        self.assertEqual(self.launched, [])
        self.controller.tick(self.observation, 1.6)
        self.assertEqual(self.mask.restore_calls, count)

    def test_continuous_unknown_expires_without_refreshing_grace_deadline(self):
        self.controller.tick(self.observation, 1)
        count = self.mask.restore_calls
        for now in (1.1, 1.7, 2.9):
            self.controller.tick(None, now)
            self.assertEqual(self.mask.restore_calls, count)
        self.controller.tick(None, 3.01)
        self.assertEqual(self.mask.restore_calls, count + 1)
        self.assertFalse(self.mask.status()["masked"])

    def test_explicit_identity_or_none_choice_overrides_unknown_grace(self):
        for change in ({"pid": 999}, {"selected_avatar_id": "custom:other"}, {"native_pet_visible": False}):
            self.controller.tick(self.observation, 1)
            count = self.mask.restore_calls
            self.controller.tick(replace(self.observation, selection_known=False, **change), 1.1)
            self.assertEqual(self.mask.restore_calls, count + 1)

    def test_hard_stale_or_generation_invalidity_never_uses_grace(self):
        for reason in ("stale", "generation", "invalid", "oversize", "future", "observer-stopped"):
            self.controller.tick(self.observation, 1)
            count = self.mask.restore_calls
            self.controller.tick(None, 1.1, source_failure=reason)
            self.assertEqual(self.mask.restore_calls, count + 1)

    def test_observer_generation_change_releases_old_mask_and_gate(self):
        self.controller.set_generation("old")
        self.controller.tick(self.observation, 1)
        self.controller.set_generation("new")
        self.assertFalse(self.mask.status()["masked"])
        self.controller.tick(None, 1.1)
        self.assertIsNone(self.controller._last_trusted_at)
        value = visibility._read(self.base / visibility.STATE_NAME)
        self.assertEqual(value["observerGeneration"], "new")

    def test_mask_counters_report_at_heartbeat_not_each_increment(self):
        self.mask.current.update(maskCount=0, reapplyCount=0, restoreCount=0)
        original_status = self.mask.status
        counts = {"maskCount": 1, "reapplyCount": 0, "restoreCount": 0}
        self.mask.status = lambda: {**original_status(), **counts}
        with mock.patch.object(visibility, "_write", wraps=visibility._write) as writer:
            self.controller.tick(self.observation, 1)
            first = writer.call_count
            counts["reapplyCount"] = 100
            self.controller.tick(self.observation, 1.5)
            self.assertEqual(writer.call_count, first)
            self.controller.tick(self.observation, 3.01)
            self.assertEqual(writer.call_count, first + 1)

    def test_missing_hwnd_does_not_guess_or_mask_other_windows(self):
        self.controller.tick(replace(self.observation, overlay_hwnd=None), 1)
        self.assertEqual(self.mask.calls, [])

    def test_report_gate_requires_fresh_successful_matching_mask(self):
        self.controller.tick(self.observation, 1)
        state = {"pid": 101, "overlayHwnd": 202}
        self.assertTrue(visibility.mask_ready(self.base, state))
        self.assertFalse(visibility.mask_ready(self.base, {**state, "pid": 102}))
        self.assertFalse(visibility.mask_ready(self.base, {**state, "overlayHwnd": 203}))
        report = visibility._read(self.base / visibility.STATE_NAME)
        for stamp in (9993.0, 10001.0):
            visibility._write(self.base / visibility.STATE_NAME, {**report, "at": stamp})
            self.assertFalse(visibility.mask_ready(self.base, state))
        self.mask.fail = True
        self.controller.tick(self.observation, 4)
        self.assertFalse(visibility.mask_ready(self.base, state))

    def test_request_writer_binds_opaque_selection_and_missing_pid_rejects(self):
        self.assertFalse(visibility.request_visibility(self.base, True, {}))
        self.assertFalse((self.base / visibility.REQUEST_NAME).exists())
        state = {"pid": 101, "selectedAvatarId": "custom:bailong-full"}
        self.assertTrue(visibility.request_visibility(self.base, False, state))
        value = visibility._read(self.base / visibility.REQUEST_NAME)
        self.assertEqual(value["pid"], 101)
        self.assertEqual(value["selectedAvatarId"], state["selectedAvatarId"])
        self.assertIs(value["visible"], False)

    def test_close_restores_and_publishes_closed_mask_gate(self):
        self.controller.tick(self.observation, 1)
        self.controller.close()
        self.assertTrue(self.mask.closed)
        self.assertFalse(visibility.mask_ready(self.base, {"pid": 101, "overlayHwnd": 202}))


if __name__ == "__main__":
    unittest.main()
