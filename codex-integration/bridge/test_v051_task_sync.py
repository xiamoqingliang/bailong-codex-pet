"""Replay synthetic Desktop task snapshots through the v051 synchronizer."""
import os
from types import SimpleNamespace
import unittest


class TaskSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from bridge_loader import load_original_module
        original = load_original_module()
        cls.app = original.QApplication.instance() or original.QApplication(['sync-test'])
        from desktop_task_sync import DesktopTaskSync
        class Sync(DesktopTaskSync):
            def excluded(self): return set()
            def history(self, snapshot, record): pass
        cls.Sync = Sync

    def setUp(self):
        from PySide6.QtCore import QObject
        class Pet(QObject):
            def __init__(self):
                super().__init__()
                self.settings = SimpleNamespace(value=lambda *a, **k: True)
                self.chat = None
                self.events = []
            def task_started(self): self.events.append(('started',))
            def task_finished(self): self.events.append(('finished',))
            def task_completed(self, text, details): self.events.append(('completed', text))
            def task_progress(self, title, text, details): self.events.append(('progress', text))
        self.pet = Pet()
        self.now = 1000.0
        self.rows = []
        self.reader = SimpleNamespace(snapshots=lambda excluded: self.rows)
        self.sync = self.Sync(self.pet, reader=self.reader, clock=lambda: self.now)
        self.addCleanup(self.sync.stop)

    def row(self, key='thread-1/turn-1', status='running', messages=None):
        return {'key': key, 'thread': key.split('/')[0], 'status': status, 'started': 1000,
                'messages': messages or [], 'final': '测试完成' if status == 'completed' else '',
                'error': None, 'alerts': [], 'step': '测试步骤'}

    def test_same_progress_and_completion_do_not_repeat(self):
        self.rows = [self.row()]
        self.sync._poll()
        self.now += 10
        self.rows = [self.row(messages=['公开进展'])]
        self.sync._poll()
        before = list(self.pet.events)
        self.sync._poll()
        self.assertEqual(self.pet.events, before)
        self.rows = [self.row(status='completed')]
        self.sync._poll()
        self.sync._poll()
        self.assertEqual(len([event for event in self.pet.events if event[0] == 'completed']), 1)

    def test_existing_completion_is_not_replayed_on_startup(self):
        self.rows = [self.row(status='completed')]
        self.sync._poll()
        self.assertEqual(self.pet.events, [])

    def test_finishing_one_of_two_tasks_keeps_the_other_running(self):
        self.rows = [self.row(), self.row('thread-2/turn-1')]
        self.sync._poll()
        self.rows = [self.row(status='completed'), self.row('thread-2/turn-1')]
        self.sync._poll()
        self.assertNotIn(('finished',), self.pet.events)
        self.assertEqual(len(self.sync.running), 1)

    def test_no_progress_reminders_are_throttled_for_five_minutes(self):
        self.rows = [self.row()]
        self.sync._poll()
        before = len(self.pet.events)
        self.now += 299
        self.sync._poll()
        self.assertEqual(len(self.pet.events), before)
        self.now += 2
        self.sync._poll()
        self.assertEqual(len(self.pet.events), before + 1)
        self.sync._poll()
        self.assertEqual(len(self.pet.events), before + 1)
