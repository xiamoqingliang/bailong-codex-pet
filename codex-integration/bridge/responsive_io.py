"""Bounded background I/O for the upstream GUI; no Codex database writes."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import pickle
import time

def install_reader(module):
    original = module.DesktopTaskReader
    if getattr(original, '_responsive_io', False):
        return
    class Reader(original):
        _responsive_io = True
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='bailong-task-read')
            self._future = None
            self._cached = None
            self._context = None
            self._stamp = 0
            from PySide6.QtCore import QCoreApplication
            app = QCoreApplication.instance()
            if app:
                app.aboutToQuit.connect(lambda: self._executor.shutdown(wait=False, cancel_futures=True))
        def snapshots(self, excluded=()):
            context = frozenset(excluded)
            if self._future is not None and self._future[0].done():
                future, previous = self._future
                self._future = None
                rows = future.result()  # Preserve upstream error handling.
                if previous == context:
                    self._cached, self._context, self._stamp = rows, context, time.monotonic()
            if self._future is None:
                self._future = (self._executor.submit(super().snapshots, context), context)
            if self._cached is None or self._context != context or time.monotonic()-self._stamp > 10:
                # A pending read must not look like task completion.
                raise OSError('Task snapshot pending')
            return self._cached
    module.DesktopTaskReader = Reader

class HistoryWriter:
    def __init__(self, controller):
        from PySide6.QtCore import QTimer
        self.controller = controller
        self.store = controller.store
        self.original_save = self.store.save
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='bailong-history-write')
        self.future = None
        self.pending = False
        self.closed = False
        self.error = None
        self.timer = QTimer(controller)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.poll)
        self.store.save = self.request
    @staticmethod
    def write(path, frozen):
        snapshot = pickle.loads(frozen)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            # Streaming encoder avoids one large C encoder holding the GIL.
            encoder = json.JSONEncoder(ensure_ascii=False, separators=(',', ':'))
            batch = []
            for chunk in encoder.iterencode(snapshot):
                batch.append(chunk)
                if len(batch) >= 2048:
                    stream.write(''.join(batch))
                    batch.clear()
            if batch:
                stream.write(''.join(batch))
        os.replace(temporary, path)
    def request(self):
        if self.closed:
            return self.original_save()
        if self.error is not None:
            error, self.error = self.error, None
            # Surface worker failures through ChatController.flush's existing
            # disk-error message. A subsequent flush retries the latest model.
            raise error
        if self.future is not None:
            self.pending = True
            return
        # Freeze mutable data in C instead of recursively deepcopying hundreds
        # of historical turns in the GUI thread. Worker owns deserialization.
        frozen = pickle.dumps(self.store.conversations, protocol=5)
        self.future = self.executor.submit(self.write, self.store.path, frozen)
        self.timer.start()
    def poll(self):
        if self.future is None or not self.future.done():
            return
        future, self.future = self.future, None
        try:
            future.result()
        except OSError as error:
            # Let the normal flush retry; never silently discard unsaved data.
            self.controller.dirty = True
            self.error = error
            self.pending = False
            self.timer.stop()
            return
        if self.pending:
            self.pending = False
            self.request()
        else:
            self.timer.stop()
    def close(self):
        if self.closed:
            return
        self.closed = True
        self.timer.stop()
        failed = False
        if self.future is not None:
            try:
                self.future.result()
            except Exception:
                failed = True
        self.executor.shutdown(wait=True)
        self.store.save = self.original_save
        if self.pending or failed or self.error is not None or self.controller.dirty:
            self.original_save()
