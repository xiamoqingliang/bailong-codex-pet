"""Coordinate expanded windows without affecting independent speech/history."""
import sys
import time


def create_window_policy(pet):
    from PySide6.QtCore import QObject, QEvent, QTimer
    from PySide6.QtWidgets import QApplication

    class WindowPolicy(QObject):
        def __init__(self):
            super().__init__(pet)
            self.clock = time.monotonic
            self._pending = {}
            self.expiry = QTimer(self)
            self.expiry.setSingleShot(True)
            self.expiry.timeout.connect(self.expire_pending)
            self._watched = set()
            self._stacking = False
            self._stack_pending = False
            self.last_order = ()
            self.watch(pet)
            self.watch(getattr(pet, 'bubble', None))

        def settings_open(self):
            from ui_motion import visible
            panel = getattr(pet, 'settings_panel', None)
            return visible(panel)

        @property
        def pending(self):
            return self._pending.get('quota') or self._pending.get('notice')

        def arm_expiry(self):
            self.expiry.stop()
            if self._pending:
                deadline = min(record[2] for record in self._pending.values())
                self.expiry.start(max(1, int((deadline - self.clock()) * 1000)))

        def drop_pending(self, kind=None):
            if kind is None:
                self._pending.clear()
            else:
                self._pending.pop(kind, None)
            self.arm_expiry()

        def expire_pending(self):
            now = self.clock()
            self._pending = {kind: record for kind, record in self._pending.items() if record[2] > now}
            self.arm_expiry()
            if not self.settings_open():
                self.flush()

        def opening_settings(self):
            self.drop_pending()
            pet.close_notification()
            panel = getattr(pet, 'quota_panel', None)
            if panel is not None:
                panel.hide()
            timer = getattr(pet, '_quota_hide_timer', None)
            if timer is not None:
                timer.stop()
            chat = getattr(pet, 'chat', None)
            if chat is not None:
                chat.hide()
            if hasattr(pet, '_notices'):
                pet._notices.clear()
                pet._notice_timer.stop()

        def user_open(self, kind):
            if kind in ('quota', 'notice'):
                self.drop_pending()
            if self.settings_open():
                pet.settings.sync()
                pet.settings_panel.accept()

        def settings_closed(self):
            pet.settings.sync()
            if getattr(pet, 'exit_reason', None) is None:
                QTimer.singleShot(0, self.flush)

        def system_popup(self, kind, callback, milliseconds):
            if self.settings_open():
                self._pending[kind] = (kind, callback, self.clock() + milliseconds / 1000)
                self.arm_expiry()
                return
            self.present(kind, callback, milliseconds)

        def present(self, kind, callback, milliseconds):
            if getattr(pet, 'exit_reason', None) is not None or pet.desktop_hidden():
                return
            if kind == 'quota':
                pet.close_notification()
            elif kind == 'notice':
                panel = getattr(pet, 'quota_panel', None)
                if panel is not None:
                    panel.hide()
            callback(milliseconds)
            self.schedule_stack()

        def flush(self):
            if self.settings_open() or self.pending is None:
                return
            kind, callback, deadline = self.pending
            remaining = int((deadline - self.clock()) * 1000)
            if kind == 'notice' and pet._quota_lease() and remaining > 0:
                return
            self.drop_pending(kind)
            if remaining > 0:
                self.present(kind, callback, remaining)
            elif self.pending is not None:
                self.flush()

        def watch(self, widget, role=None):
            if widget is not None:
                role = role or next((name for name, value in self.windows() if value is widget), None)
                if role is not None and role != 'pet':
                    from ui_motion import attach
                    attach(widget, pet, role)
            if widget is not None and widget not in self._watched:
                self._watched.add(widget)
                widget.installEventFilter(self)
                widget.destroyed.connect(lambda *_, widget=widget: self._watched.discard(widget))

        def windows(self):
            chat = getattr(pet, 'chat', None)
            return [('speech', getattr(pet, 'bubble', None)),
                    ('notice', getattr(pet, 'notification_bubble', None)),
                    ('quota', getattr(pet, 'quota_panel', None)),
                    ('composer', getattr(chat, 'composer', None)),
                    ('settings', getattr(pet, 'settings_panel', None)),
                    ('toolbar', getattr(pet, 'toolbar', None)),
                    ('cards', getattr(pet, 'activity_cards', None)),
                    ('pet', pet), ('history', getattr(chat, 'window', None))]

        def schedule_stack(self):
            if not self._stacking and not self._stack_pending:
                self._stack_pending = True
                QTimer.singleShot(0, self.restack)

        def restack(self):
            self._stack_pending = False
            if self._stacking or getattr(pet, 'exit_reason', None) is not None:
                return
            self._stacking = True
            try:
                windows = self.windows()
                for name, widget in windows:
                    self.watch(widget, name)
                visible = [(name, widget) for name, widget in windows
                           if widget is not None and widget.isVisible()]
                self.last_order = tuple(name for name, _ in visible)
                if sys.platform == 'win32' and QApplication.platformName() != 'offscreen':
                    import ctypes
                    user32 = ctypes.windll.user32
                    set_owner = user32.SetWindowLongPtrW
                    set_owner.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
                    set_owner.restype = ctypes.c_ssize_t
                    position = user32.SetWindowPos
                    position.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                         ctypes.c_int, ctypes.c_int, ctypes.c_uint]
                    position.restype = ctypes.c_int
                    get_owner = user32.GetWindow
                    get_owner.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                    get_owner.restype = ctypes.c_void_p
                    get_long = user32.GetWindowLongPtrW
                    get_long.argtypes = [ctypes.c_void_p, ctypes.c_int]
                    get_long.restype = ctypes.c_ssize_t
                    handles = [int(widget.winId()) for _, widget in visible]
                    for name, widget in visible:
                        handle = int(widget.winId())
                        # Native owned windows are forced above their owner.
                        # Remove that relation while retaining Qt lifetime ownership.
                        if name != 'pet' and get_owner(handle, 4):  # GW_OWNER
                            widget.windowHandle().setTransientParent(None)
                            set_owner(handle, -8, 0)  # GWLP_HWNDPARENT
                    current = []
                    wanted = set(handles)
                    callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_ssize_t)
                    def collect(handle, _):
                        if handle in wanted:
                            current.append(handle)
                        return len(current) < len(handles)
                    callback = callback_type(collect)
                    user32.EnumWindows(callback, 0)
                    if current == list(reversed(handles)) and all(get_long(handle, -20) & 8 for handle in handles):
                        return
                    for handle in handles:
                        position(handle, ctypes.c_void_p(-1), 0, 0, 0, 0,
                                 0x0001 | 0x0002 | 0x0010 | 0x0200)
                # Offscreen tests inspect the same ordered roles; native calls
                # above preserve activation, focus and all window geometries.
            finally:
                self._stacking = False

        def eventFilter(self, watched, event):
            if event.type() in (QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.WindowActivate, QEvent.Type.ZOrderChange):
                self.schedule_stack()
            return super().eventFilter(watched, event)

    return WindowPolicy()
