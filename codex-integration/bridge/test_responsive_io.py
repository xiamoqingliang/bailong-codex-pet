"""Bounded I/O, failure reporting, and final-save regression tests."""
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication
from responsive_io import HistoryWriter, install_reader

app = QApplication.instance() or QApplication([])


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.controller = QObject()
        self.controller.dirty = False
        path = Path(self.temporary.name) / 'history.json'
        store = SimpleNamespace(path=path, conversations=[{'reply': 'first'}])
        store.save = lambda: path.write_text(json.dumps(store.conversations), encoding='utf-8')
        self.controller.store = store
        self.writer = HistoryWriter(self.controller)
        self.addCleanup(self.writer.close)

    def test_coalesces_latest_mutation_and_flushes_on_exit(self):
        writer, store = self.writer, self.controller.store
        writer.request()
        store.conversations[0]['reply'] = 'latest'
        writer.request()
        writer.future.result(timeout=5)
        writer.poll()
        writer.future.result(timeout=5)
        writer.poll()
        self.assertEqual(json.loads(store.path.read_text())[0]['reply'], 'latest')
        writer.request()
        store.conversations.append({'reply': 'exit'})
        self.controller.dirty = True
        writer.close()
        self.assertEqual(len(json.loads(store.path.read_text())), 2)

    def test_failed_write_surfaces_and_retries(self):
        with patch.object(self.writer, 'write', side_effect=OSError('disk full')):
            self.writer.request()
            try:
                self.writer.future.result(timeout=5)
            except OSError:
                pass
            self.writer.poll()
        self.assertTrue(self.controller.dirty)
        with self.assertRaisesRegex(OSError, 'disk full'):
            self.writer.request()
        self.writer.request()
        self.writer.future.result(timeout=5)
        self.writer.poll()
        self.assertEqual(json.loads(self.controller.store.path.read_text())[0]['reply'], 'first')


class ReaderTests(unittest.TestCase):
    def test_pending_context_change_failure_and_recovery(self):
        gate = threading.Event()
        class OriginalReader:
            fail = False
            def snapshots(self, excluded):
                if not gate.wait(timeout=5):
                    raise OSError('test timeout')
                if self.fail:
                    raise OSError('database unavailable')
                return [sorted(excluded)]
        module = SimpleNamespace(DesktopTaskReader=OriginalReader)
        install_reader(module)
        reader = module.DesktopTaskReader()
        try:
            with self.assertRaises(OSError):
                reader.snapshots({'a'})
            gate.set()
            reader._future[0].result(timeout=5)
            self.assertEqual(reader.snapshots({'a'}), [['a']])
            reader._future[0].result(timeout=5)
            with self.assertRaises(OSError):
                reader.snapshots({'b'})
            reader._future[0].result(timeout=5)
            self.assertEqual(reader.snapshots({'b'}), [['b']])
            reader._future[0].result(timeout=5)
            reader.fail = True
            reader.snapshots({'b'})
            try:
                reader._future[0].result(timeout=5)
            except OSError:
                pass
            with self.assertRaisesRegex(OSError, 'database unavailable'):
                reader.snapshots({'b'})
            reader.fail = False
            reader.snapshots({'b'})
            reader._future[0].result(timeout=5)
            self.assertEqual(reader.snapshots({'b'}), [['b']])
        finally:
            gate.set()
            reader._executor.shutdown(wait=True)


if __name__ == '__main__':
    unittest.main()
