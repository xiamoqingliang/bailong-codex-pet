"""Real original Qt visibility/close methods, with no assets or network loaded."""
import os
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from pet_lifecycle import PetLifecycle


class PetLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from bridge_loader import load_original_module
        cls.original = original = load_original_module()
        from PySide6.QtWidgets import QWidget
        cls.app = original.QApplication.instance() or original.QApplication(["lifecycle-test", "-platform", "offscreen"])

        class LeanOriginal(original.Pet):
            def __init__(self):
                QWidget.__init__(self)
                self.manual_hidden = self.fullscreen_hidden = False
                self.hidden_since = None
                self.started = time.monotonic()
                self.mode = "idle"
                self.current = None
                self.worker = self.chat = None
                self.tick = original.QTimer(self)
                self.visibility_timer = original.QTimer(self)
                self.click_timer = original.QTimer(self)
                self.bubble_timer = original.QTimer(self)
                self.dock_click_timer = original.QTimer(self)
                self.desktop_sync = SimpleNamespace(stop=lambda: None)
                self.tray = QWidget()
                self.bubble = original.Bubble()
                self.toolbar = QWidget()
                self.activity_cards = QWidget()
                self.tray_menu = QWidget()
                self.messages = []
            def ensure_on_screen(self): pass
            def position_bubble(self): pass
            def say(self, text, ms=4000):
                self.messages.append(text)
                original.Pet.say(self, text, ms)

        class Linked(PetLifecycle, LeanOriginal):
            def __init__(self):
                super().__init__()
                self.host_pid = 101
                self.native_sync_ready = True
                self.state = {"running": True, "pid": 101, "overlayOpen": True}
                self.requests = []
                self.closing_records = []
                self.finished = 0
            def _read_bridge_state(self): return self.state
            def _native_can_show(self, state): return bool(state and state.get("overlayOpen"))
            def _request_native_visibility(self, visible, state): self.requests.append(visible)
            def _publish_closing(self): self.closing_records.append(self.exit_reason)
            def _finish_exit(self): self.finished += 1

        cls.LeanOriginal, cls.Linked = LeanOriginal, Linked

    def setUp(self):
        patcher = mock.patch.object(self.original.QApplication, "quit")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pet = self.Linked()
        self.pet.show()
        self.addCleanup(self.dispose)

    def dispose(self):
        for timer in (self.pet.tick, self.pet.visibility_timer, self.pet.click_timer, self.pet.bubble_timer):
            timer.stop()
        for window in (self.pet, self.pet.tray, self.pet.bubble, self.pet.toolbar,
                       self.pet.activity_cards, self.pet.tray_menu):
            window.hide()
            window.deleteLater()
        if self.pet.chat:
            self.pet.chat.composer.deleteLater()
            self.pet.chat.window.deleteLater()
        self.app.processEvents()

    def test_original_refuses_hide_without_tray_but_native_hide_succeeds(self):
        self.original.Pet.set_manual_hidden(self.pet, True)
        self.assertTrue(self.pet.isVisible())
        self.assertFalse(self.pet.manual_hidden)
        self.assertIn("托盘暂时没准备好", self.pet.messages[-1])
        self.pet.messages.clear()
        self.pet.apply_native_hidden(True)
        self.assertFalse(self.pet.isVisible())
        self.assertFalse(self.pet.bubble.isVisible())
        self.assertTrue(self.pet.manual_hidden)
        self.assertEqual(self.pet.messages, [])

    def test_local_hide_stays_hidden_on_old_open_sample_and_show_recovers_immediately(self):
        self.pet.set_manual_hidden(True)
        self.assertFalse(self.pet.isVisible())
        self.assertFalse(self.pet.allow_native_visible(self.pet.state, True))
        self.pet.set_manual_hidden(False)
        self.assertTrue(self.pet.isVisible())
        self.assertTrue(self.pet.allow_native_visible(self.pet.state, True))
        self.assertEqual(self.pet.requests, [False, True])
        self.assertEqual(self.pet.messages, [])

    def test_native_hide_ack_allows_later_explicit_show(self):
        self.pet.set_manual_hidden(True)
        self.assertFalse(self.pet.allow_native_visible(dict(self.pet.state, overlayOpen=False), False))
        self.assertTrue(self.pet.allow_native_visible(self.pet.state, True))
        self.pet.apply_native_hidden(False)
        self.assertTrue(self.pet.isVisible())

    def test_deferred_original_close_cannot_reshow_any_ui(self):
        from PySide6.QtWidgets import QWidget
        attempts = []
        def shutdown():
            attempts.append(1)
            return len(attempts) > 1
        self.pet.chat = SimpleNamespace(composer=QWidget(), window=QWidget(),
                                       enabled=True, closing=False, shutdown=shutdown)
        for window in (self.pet.tray, self.pet.toolbar, self.pet.activity_cards,
                       self.pet.chat.composer, self.pet.chat.window):
            window.show()
        self.pet.visibility_timer.start(10)
        self.pet.close()
        self.assertEqual(self.pet.exit_reason, "user")
        self.assertEqual(self.pet.finished, 0)
        self.assertFalse(self.pet.visibility_timer.isActive())
        self.pet.host_pid = 202
        for _ in range(3):
            self.pet.apply_native_hidden(False)
            self.pet.apply_visibility()
            self.pet.sync_dialogue_visibility()
            self.pet.request_exit("codex-closed")
        for window in (self.pet, self.pet.tray, self.pet.bubble, self.pet.toolbar,
                       self.pet.activity_cards, self.pet.chat.composer, self.pet.chat.window):
            self.assertFalse(window.isVisible())
        deadline = time.monotonic() + 1
        while self.pet.finished == 0 and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.assertEqual(self.pet.finished, 1)
        self.assertEqual(attempts, [1, 1])
        self.assertEqual(self.pet.requests, [False])
        self.assertEqual(self.pet.exit_host_pid, 101)
        self.assertEqual(self.pet.closing_records, ["user"])
        self.assertEqual(self.pet.messages, [])

    def test_host_exit_hides_everything_and_finishes_without_tray(self):
        self.pet.bubble.message("fixture")
        self.pet.toolbar.show()
        self.pet.request_exit("codex-closed")
        self.assertEqual(self.pet.finished, 1)
        self.assertFalse(self.pet.isVisible())
        self.assertFalse(self.pet.toolbar.isVisible())
        self.assertFalse(self.pet.bubble.isVisible())
        self.assertEqual(self.pet.requests, [])
        self.assertEqual(self.pet.exit_reason, "codex-closed")

    def test_status_file_collision_does_not_prevent_exit(self):
        with mock.patch.object(self.pet, "_publish_closing", side_effect=PermissionError), \
             mock.patch.object(self.pet, "_request_native_visibility", side_effect=PermissionError):
            self.pet.close()
        self.assertEqual(self.pet.finished, 1)
        self.assertFalse(self.pet.isVisible())


if __name__ == "__main__":
    unittest.main()
