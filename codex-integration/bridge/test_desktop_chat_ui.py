"""Exercise original Qt signals/history with a synthetic Desktop owner only."""
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from desktop_chat_link import make_session_class
from test_desktop_chat_link import FakeClock, Ipc


class OriginalUiLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from bridge_loader import load_original_module
        load_original_module()
        import chat_backend
        import chat_ui
        from PySide6.QtWidgets import QApplication, QWidget
        from PySide6.QtCore import QThread
        cls.backend, cls.ui = chat_backend, chat_ui
        cls.app = QApplication.instance() or QApplication(["desktop-chat-ui-test", "-platform", "offscreen"])
        cls.QThread = QThread

        class Pet(QWidget):
            def __init__(self):
                super().__init__()
                self.results = []
                self.task_bubbles_disabled = False
                self.bubble = SimpleNamespace(is_task_bubble=False)
            def say(self, *args): pass
            def task_started(self): pass
            def task_finished(self): pass
            def task_progress(self, *args): pass
            def task_completed(self, text, details): self.results.append(text)
            def task_result(self, *args): self.results.append("failed")
        cls.Pet = Pet

    def test_real_qthread_signals_update_original_history_on_gui_thread(self):
        clock, opened, observed = FakeClock(), [], []
        ipc = Ipc(clock)
        namespace = SimpleNamespace(CodexSession=self.backend.CodexSession,
                                    TaskCancelled=self.backend.TaskCancelled,
                                    VERSION=self.backend.VERSION, PERSONA=self.backend.PERSONA)
        linked = make_session_class(namespace, client_factory=lambda: ipc,
                                    open_thread=opened.append, clock=clock, sleep=clock.sleep)
        app, QThread = self.app, self.QThread

        class Controller(self.ui.ChatController):
            def receive(self, kind, value):
                if kind == "output":
                    observed.append((value, QThread.currentThread() == app.thread()))
                super().receive(kind, value)

        with tempfile.TemporaryDirectory(prefix="BailongChatMock-") as temporary:
            pet = self.Pet()
            controller = Controller(pet, directory=temporary)
            try:
                controller.active = controller.store.new(Path(temporary) / "work")
                controller.active["threadId"] = "thread-1"
                with patch.object(self.ui, "CodexSession", linked), patch("quota.find_codex", return_value="mock-codex"):
                    controller.submit("synthetic user input; never sent to a model")
                    for _ in range(100):
                        self.app.processEvents()
                        time.sleep(0.01)
                        if not controller.busy:
                            break
                self.assertFalse(controller.busy)
                self.assertEqual(observed, [("live output", True), ("final output", True)])
                self.assertEqual(controller.active["threadId"], "thread-1")
                self.assertEqual(controller.active["turns"][-1]["result"], "final output")
                self.assertEqual(controller.active["turns"][-1]["status"], "completed")
                self.assertEqual(pet.results, ["final output"])
                self.assertEqual(opened, ["thread-1"])
                self.assertEqual(len([call for call in ipc.calls if call[0] == "start_turn"]), 1)
            finally:
                controller.shutdown()
                controller.window.deleteLater()
                controller.composer.deleteLater()
                pet.deleteLater()
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
