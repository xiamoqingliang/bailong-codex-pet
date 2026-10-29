"""Adapt upstream behavior while keeping one task animation owner."""
from collections import deque
from bailong_theme import MENU_STYLE


MESSAGE_SIZE = (256, 52)


def make_message_bubble(original_class):
    """Keep interaction/quota speech the same size and scroll its full text."""
    from math import ceil
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTextBrowser
    from bailong_theme import SCROLLBAR_STYLE
    from ui_motion import MotionWindowMixin

    class MessageText(QTextBrowser):
        _centering = False

        def __init__(self):
            super().__init__()
            self.document().documentLayout().documentSizeChanged.connect(self.center_content)

        def center_content(self, *_):
            if self._centering:
                return
            self._centering = True
            try:
                margins = self.viewportMargins()
                available = self.viewport().height() + margins.top()
                text_height = self.document().documentLayout().documentSize().height()
                top = max(0, int((available - text_height) / 2))
                if margins.top() != top:
                    self.setViewportMargins(0, top, 0, 0)
            finally:
                self._centering = False

        def resizeEvent(self, event):
            super().resizeEvent(event)
            self.center_content()

        def two_line_height(self):
            self.document().documentLayout().documentSize()
            height = self.fontMetrics().height()
            block = self.document().begin()
            seen = 0
            while block.isValid() and seen < 2:
                layout = block.layout()
                for index in range(min(layout.lineCount(), 2 - seen)):
                    height = max(height, layout.lineAt(index).height())
                    seen += 1
                block = block.next()
            return ceil(2 * height)

    class MessageBubble(MotionWindowMixin, original_class):
        _bailong_fixed_messages = True
        _message_view = None

        @property
        def fixed_message_size(self):
            if (self._message_view is None or getattr(self, 'is_task_bubble', False)
                    or getattr(self, 'is_size_preview', False) or getattr(self, 'is_quota_result', False)):
                return None
            return tuple(round(value * self.scale_percent / 100) for value in MESSAGE_SIZE)

        def clear(self):
            from ui_motion import visible
            self._replace_message = visible(self)
            self._message_view = None
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)
            super().clear()

        def label(self, text):
            if (getattr(self, 'is_task_bubble', False) or getattr(self, 'is_size_preview', False)
                    or getattr(self, 'is_quota_result', False)):
                return super().label(text)
            view = MessageText()
            view.setObjectName('InteractionMessageText')
            view.setPlainText(text)
            view.setOpenLinks(False)
            view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            view.document().setDocumentMargin(0)
            view.setProperty('baseHeight', MESSAGE_SIZE[1] - 20)
            self._message_view = view
            self.layout.addWidget(view)

        def set_scale_percent(self, value):
            super().set_scale_percent(value)
            # Rebuilt children of an already visible bubble otherwise stay
            # pending until the next event pass, leaving a stale layout height.
            for index in range(self.layout.count()):
                widget = self.layout.itemAt(index).widget()
                if widget is not None:
                    widget.show()
            size = self.fixed_message_size
            if size is not None:
                margins = self.layout.contentsMargins()
                # Font pixels, frame size and padding round independently.
                # At 118%, two 19px lines need 38px, while the naive padding
                # leaves only 37px. Preserve two lines by taking the missing
                # pixels from padding rather than changing the fixed frame.
                deficit = max(0, self._message_view.two_line_height()
                              + margins.top() + margins.bottom() - size[1])
                top = max(0, margins.top() - deficit // 2)
                bottom = max(0, margins.bottom() - (deficit + 1) // 2)
                # Reserve room for the existing close button, including when
                # a vertical scrollbar is needed for a long message.
                self.layout.setContentsMargins(margins.left(), top,
                                               round(13 * self.scale_percent / 100) + self.close_button.width(),
                                               bottom)
                self._message_view.setFixedHeight(size[1] - top - bottom)
                self._message_view.setStyleSheet(self._message_view.styleSheet() + SCROLLBAR_STYLE)
                self.setFixedSize(*size)
                self._message_view.center_content()
                self._message_view.verticalScrollBar().setValue(0)
                self.close_button.raise_()
                motion = getattr(self, '_ui_motion', None)
                if self._replace_message and motion is not None:
                    from ui_motion import fade_content
                    fade_content(self._message_view, motion.owner)
                self._replace_message = False
            else:
                self.layout.invalidate()
                self.layout.activate()
                self.adjustSize()

    return MessageBubble


def configure_preferences(original, test_file=None):
    from PySide6.QtCore import QSettings
    if not getattr(original.Bubble, '_bailong_fixed_messages', False):
        original.Bubble = make_message_bubble(original.Bubble)
    keys = ('autoHideFullscreen', 'autoMovementDisabled', 'sizePercent', 'bubblePercent',
            'soundDisabled', 'taskBubblesDisabled', 'syncCodexTasks')
    def version_settings(*args, **kwargs):
        if test_file is not None:
            return QSettings(str(test_file), QSettings.Format.IniFormat)
        settings = QSettings('BailongPet', 'DesktopPet-v051')
        if not settings.value('preferencesMigrated', False, type=bool):
            previous = QSettings('BailongPet', 'DesktopPet')
            for key in keys:
                if previous.contains(key) and not settings.contains(key):
                    settings.setValue(key, previous.value(key))
            settings.setValue('preferencesMigrated', True)
            settings.sync()
        return settings
    original.QSettings = version_settings


class MetadataActivityDriver:
    """Cards observe state; upstream DesktopTaskSync alone drives animations."""
    state = 'idle'
    last_action = None
    last_error = None
    def __init__(self, original, pet, is_popup_open=None):
        self.pet = pet
    def update(self, sessions, active=True, available=True, **kwargs):
        priority = ('needs_input', 'failed', 'running', 'completed', 'unknown')
        states = {item.get('state') for item in sessions} if active and available else set()
        self.state = next((state for state in priority if state in states), 'idle')
        return self.state
    def on_idle(self):
        return False


class V051Behavior:
    _ui_motion_enabled = True
    fixed_pet = False
    settings_panel = None
    quota_panel = None
    dock_controls = None
    _quota_speech_active = False
    _quota_ui_dismissed = False
    _side_layout_adjusting = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fixed_pet = self.settings.value('fixedPet', False, type=bool)

    def set_fixed_pet(self, enabled):
        self.fixed_pet = bool(enabled)
        self.settings.setValue('fixedPet', self.fixed_pet)
        self.settings.sync()
        # Keep an in-progress animation, but discard its remaining route so
        # unlocking midway cannot jump to a target planned before the lock.
        if self.movement_plan is not None and not getattr(self, '_movement_manual', False):
            self.movement_plan = (self.pos(), self.pos())
        self.note_interaction()

    def start_movement(self, item, manual=False):
        result = super().start_movement(item, manual=manual)
        if result:
            self._movement_manual = bool(manual)
        return result

    def advance_movement(self, elapsed):
        if not self.fixed_pet or getattr(self, '_movement_manual', False):
            return super().advance_movement(elapsed)

    def finish_movement(self):
        super().finish_movement()
        self._movement_manual = False

    def window_policy(self):
        if getattr(self, '_window_policy', None) is None:
            from window_policy import create_window_policy
            self._window_policy = create_window_policy(self)
        return self._window_policy

    def close_notification(self):
        bubble = getattr(self, 'notification_bubble', None)
        if bubble is not None:
            bubble.hide()
            self._notification_timer.stop()

    def _render_notice(self, method, args, milliseconds):
        from PySide6.QtCore import QTimer
        from ui_motion import visible
        if self.task_bubbles_disabled or self.desktop_hidden():
            return
        if getattr(self, 'notification_bubble', None) is None:
            self.notification_bubble = type(self.bubble)()
            self.window_policy().watch(self.notification_bubble, 'notice')
            self._notification_timer = QTimer(self)
            self._notification_timer.setSingleShot(True)
            self._notification_timer.timeout.connect(self.close_notification)
            self.notification_bubble.dismiss_requested.connect(self.close_notification)
            from companion_layout import observe_speech_layout
            observe_speech_layout(self, self.notification_bubble)
        bubble = self.notification_bubble
        bubble.set_scale_percent(self.bubble_percent)
        if method == 'task_completed':
            text, details = args
            title = '主人，完成啦！'
        else:
            title, text, details = args
        if method == 'task_progress' and getattr(bubble, 'is_task_progress', False) and visible(bubble):
            bar = bubble.result_view.verticalScrollBar()
            previous, at_end = bar.value(), bar.value() >= bar.maximum() - 8
            bubble.result_title.setText(title)
            bubble.result_view.setPlainText(text)
            bar.setValue(bar.maximum() if at_end else min(previous, bar.maximum()))
        else:
            bubble.task_result(title, text, details)
        bubble.is_task_progress = method == 'task_progress'
        self._notification_timer.start(milliseconds)
        bubble._ui_motion.arm_expiry(milliseconds)
        self.position_bubble()
        self.window_policy().schedule_stack()

    def _request_notice(self, method, args, milliseconds):
        if self.task_bubbles_disabled or self.desktop_hidden():
            return
        policy = self.window_policy()
        if not policy.settings_open() and self._quota_lease():
            return self._queue_notice(method, args)
        policy.system_popup('notice', lambda remaining: self._render_notice(method, args, remaining), milliseconds)

    def say(self, text, ms=4000):
        if not getattr(self, '_creating_quota_speech', False):
            self._quota_speech_active = False
        result = super().say(text, ms)
        motion = getattr(self.bubble, '_ui_motion', None)
        if motion is not None and self.bubble.isVisible() and not self.speech_hidden():
            motion.arm_expiry(ms)
        return result

    def set_bubble_percent(self, value):
        super().set_bubble_percent(value)
        bubble = getattr(self, 'notification_bubble', None)
        if bubble is not None:
            bubble.set_scale_percent(self.bubble_percent)
            self.position_bubble()

    def set_task_bubbles_disabled(self, disabled):
        super().set_task_bubbles_disabled(disabled)
        if disabled:
            self.close_notification()
            policy = getattr(self, '_window_policy', None)
            if policy is not None:
                policy.drop_pending('notice')

    def toggle_chat(self):
        self.window_policy().user_open('composer')
        super().toggle_chat()
        self.window_policy().schedule_stack()

    def _ui_busy(self):
        toolbar = getattr(self, 'toolbar', None)
        panel = self.settings_panel
        from PySide6.QtWidgets import QApplication
        return bool(panel and panel.isVisible() or toolbar and toolbar._dragging
                    or QApplication.activePopupWidget())

    def movement_allowed(self, now=None):
        return not self._ui_busy() and not self._quota_lease() and super().movement_allowed(now)

    def try_leisure(self):
        if self._ui_busy():
            return False
        return super().try_leisure()

    def constrained_position(self, position, pointer=None):
        # Upstream reserves the composer below the pet when chat is enabled.
        # Our composer moves independently, so only the pet canvas is clamped.
        from PySide6.QtCore import QPoint
        from PySide6.QtWidgets import QApplication
        screen = QApplication.screenAt(pointer) if pointer is not None else None
        screens = [screen] if screen is not None else QApplication.screens()
        candidates = []
        for target in screens:
            area = target.availableGeometry()
            max_x = max(area.left(), area.right() + 1 - self.width())
            max_y = max(area.top(), area.bottom() + 1 - self.height())
            candidates.append(QPoint(max(area.left(), min(position.x(), max_x)),
                                     max(area.top(), min(position.y(), max_y))))
        return min(candidates, key=lambda point: (point.x() - position.x()) ** 2 +
                   (point.y() - position.y()) ** 2) if candidates else QPoint(position)

    def show_settings(self):
        from bailong_settings import create_settings, position_settings
        self.note_interaction()
        policy = self.window_policy()
        if policy.settings_open():
            self.settings_panel.accept()
            return
        if self.settings_panel is None:
            self.settings_panel = create_settings(self)
        panel = self.settings_panel
        policy.watch(panel, 'settings')
        policy.opening_settings()
        panel.refresh()
        if not position_settings(self, panel):
            self.say('当前屏幕空间不足，请先调小白龙或移动位置。', 4000)
            return
        panel.show()
        panel.raise_()
        panel.activateWindow()
        policy.watch(panel)
        policy.schedule_stack()

    def position_bubble(self):
        from companion_layout import layout_companions
        layout_companions(self)

    def ensure_quota_side_space(self, width):
        if self.fixed_pet or self._side_layout_adjusting:
            return
        area = self.screen().availableGeometry()
        space = max(self.x() - area.left(), area.right() + 1 - self.x() - self.width())
        if space >= width + 8 or area.width() < self.width() + width + 8:
            return
        choices = (area.left(), area.right() + 1 - self.width())
        self._side_layout_adjusting = True
        try:
            self.move(min(choices, key=lambda value: abs(value - self.x())), self.y())
        finally:
            self._side_layout_adjusting = False

    show_size_dialog = show_settings
    show_bubble_settings = show_settings

    def actions_menu(self):
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.setStyleSheet(MENU_STYLE)
        groups = (('互动', self.variants, self.choose),
                  ('工作', self.work_slots, self.choose),
                  ('待机', self.idles + self.extra_idles, self.preview_idle),
                  ('移动', self.movement_slots, self._manual_movement))
        for caption, values, callback in groups:
            submenu = menu.addMenu(caption)
            submenu.setStyleSheet(MENU_STYLE)
            for item in values:
                if not isinstance(item, dict) or not self.asset_path(item).is_file():
                    continue
                title = item.get('name') or item.get('id', caption)
                action = submenu.addAction(str(title))
                action.triggered.connect(lambda checked=False, item=item, fn=callback: fn(item))
            submenu.setEnabled(bool(submenu.actions()))
        return menu

    def _manual_movement(self, item):
        self.undock()
        self.note_interaction()
        self.start_movement(item, manual=True)

    def context_menu(self):
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.setStyleSheet(MENU_STYLE)
        if self.dock_edge:
            menu.addAction('展开桌宠', self.begin_undock)
        hidden = self.manual_hidden
        menu.addAction('显示桌宠' if hidden else '隐藏桌宠',
                       lambda checked=False: self.set_manual_hidden(not hidden))
        menu.addAction('聊天', self.toggle_chat)
        menu.addAction('查询额度', self.quota)
        actions = self.actions_menu()
        actions.setTitle('动作')
        menu.addMenu(actions)
        menu.addAction('设置', self.show_settings)
        menu.addSeparator()
        menu.addAction('退出桌宠', self.close)
        return menu

    def task_finished(self):
        # Own-chat completion must not clear another running Desktop task.
        sync = getattr(self, 'desktop_sync', None)
        chat = getattr(self, 'chat', None)
        if getattr(sync, 'running', None) or getattr(chat, 'running_turn', None):
            self.task_active = True
            return
        if self._quota_lease():
            self.task_active = False
            return
        super().task_finished()

    def task_started(self):
        if self._quota_lease():
            self.task_active = True
            return
        super().task_started()

    def _quota_lease(self):
        timer = getattr(self, '_quota_hide_timer', None)
        return bool(getattr(self, 'quota_inflight', False) and not self._quota_ui_dismissed
                    or self.quota_panel is not None and self.quota_panel.isVisible() and timer is not None and timer.isActive()
                    or self.bubble.isVisible() and getattr(self.bubble, 'is_quota_result', False)
                    and not getattr(self.bubble, 'is_task_bubble', False)
                    and self.bubble_timer.isActive())

    def _start_quota_deadline(self, milliseconds=12000):
        from PySide6.QtCore import QTimer
        if not hasattr(self, '_quota_hide_timer'):
            self._quota_hide_timer = QTimer(self)
            self._quota_hide_timer.setSingleShot(True)
            self._quota_hide_timer.timeout.connect(self.dismiss_quota)
        self._quota_ui_dismissed = False
        self._quota_hide_timer.start(milliseconds)
        motion = getattr(self.quota_panel, '_ui_motion', None)
        if motion is not None:
            motion.arm_expiry(milliseconds)

    def _start_quota_connection_timeout(self):
        from PySide6.QtCore import QTimer
        if not hasattr(self, '_quota_connect_timer'):
            self._quota_connect_timer = QTimer(self)
            self._quota_connect_timer.setSingleShot(True)
            self._quota_connect_timer.timeout.connect(self._quota_connection_timeout)
        self._quota_connect_timer.start(30000)

    def _quota_connection_timeout(self):
        if not self.quota_inflight or self.quota_generation != self.generation:
            return
        self._cancel_quota_worker()
        self.quota_failed('连接等待超时，请确认 Codex 已启动、已登录且网络可用。')
        self.quota_generation = -1

    def _cancel_quota_worker(self):
        worker = getattr(self, 'worker', None)
        if worker is None:
            return
        for signal, callback in ((worker.ready, self.quota_ready), (worker.failed, self.quota_failed)):
            try:
                signal.disconnect(callback)
            except (RuntimeError, TypeError):
                pass
        cancel = getattr(worker, 'cancel', None)
        if cancel is not None:
            cancel.set()
        if worker.isRunning():
            if not hasattr(self, '_retired_quota_workers'):
                self._retired_quota_workers = []
            self._retired_quota_workers.append(worker)
            worker.finished.connect(lambda worker=worker: self._retired_quota_workers.remove(worker)
                                    if worker in self._retired_quota_workers else None)

    def quota_ready(self, windows):
        if hasattr(self, '_quota_connect_timer'):
            self._quota_connect_timer.stop()
        self.quota_inflight = False
        query_speech = self._quota_speech_active
        self._quota_speech_active = False
        if self.desktop_hidden() or self.quota_generation != self.generation:
            if self.quota_panel is not None:
                self.quota_panel.hide()
            return
        from quota_panel import create_quota_panel
        if self.quota_panel is None:
            self.quota_panel = create_quota_panel(self)
        if query_speech:
            self.bubble.hide()
            self.bubble_timer.stop()
        def display(remaining):
            self.quota_panel.display(windows)
            self._start_quota_deadline(remaining)
        self.window_policy().system_popup('quota', display, 12000)
        if getattr(self, 'activity_cards', None) is not None:
            self.activity_cards.sync_geometry()

    def quota(self):
        from quota_panel import create_quota_panel
        self.window_policy().user_open('quota')
        self.close_notification()
        self._quota_speech_active = True
        self._quota_ui_dismissed = False
        if self.quota_panel is not None:
            self.quota_panel.hide()
        self._creating_quota_speech = True
        try:
            super().quota()
        finally:
            self._creating_quota_speech = False
        if self.quota_inflight and not self.desktop_hidden():
            if self.quota_panel is None:
                self.quota_panel = create_quota_panel(self)
            self.bubble_timer.stop()
            motion = getattr(self.bubble, '_ui_motion', None)
            if motion is not None:
                motion.arm_expiry(None)
            self.quota_panel.display([], loading=True)
            if hasattr(self, '_quota_hide_timer'):
                self._quota_hide_timer.stop()
            self._start_quota_connection_timeout()
        self.window_policy().schedule_stack()

    def quota_failed(self, error):
        if hasattr(self, '_quota_connect_timer'):
            self._quota_connect_timer.stop()
        self.quota_inflight = False
        query_speech = self._quota_speech_active
        self._quota_speech_active = False
        if self.desktop_hidden() or self.quota_generation != self.generation:
            if self.quota_panel is not None:
                self.quota_panel.hide()
            return
        from quota_panel import create_quota_panel
        if self.quota_panel is None:
            self.quota_panel = create_quota_panel(self)
        if query_speech:
            self.bubble.hide()
            self.bubble_timer.stop()
        def display(remaining):
            self.quota_panel.display([], error=error)
            self._start_quota_deadline(remaining)
        self.window_policy().system_popup('quota', display, 8000)

    def dismiss_quota(self):
        query_speech = self._quota_speech_active
        self._quota_speech_active = False
        self._quota_ui_dismissed = True
        if hasattr(self, '_quota_hide_timer'):
            self._quota_hide_timer.stop()
        if hasattr(self, '_quota_connect_timer'):
            self._quota_connect_timer.stop()
        if self.quota_panel is not None:
            if self.quota_panel.isVisible() and self.quota_inflight:
                self.quota_generation = -1
                self._cancel_quota_worker()
                self.quota_inflight = False
            self.quota_panel.hide()
        policy = getattr(self, '_window_policy', None)
        if policy is not None:
            from PySide6.QtCore import QTimer
            policy.drop_pending('quota')
            QTimer.singleShot(0, policy.flush)
        if query_speech:
            super().dismiss_bubble()
        elif self.mode == 'quota':
            self.idle()

    def dismiss_bubble(self):
        self._quota_speech_active = False
        super().dismiss_bubble()

    def closeEvent(self, event):
        from PySide6.QtCore import QTimer
        self.close_notification()
        if getattr(self, '_window_policy', None) is not None:
            self._window_policy.drop_pending()
        if any(worker.isRunning() for worker in getattr(self, '_retired_quota_workers', ())):
            event.ignore()
            QTimer.singleShot(100, self.close)
            return
        super().closeEvent(event)

    def idle(self):
        if self._quota_lease():
            self.play(self.idles[0], 'idle')
            return
        super().idle()

    def _queue_notice(self, method, args):
        from PySide6.QtCore import QTimer
        if not hasattr(self, '_notice_timer'):
            self._notices = deque(maxlen=16)
            self._notice_timer = QTimer(self)
            self._notice_timer.setInterval(250)
            self._notice_timer.timeout.connect(self._flush_notices)
        if method == 'task_progress':
            self._notices = deque((item for item in self._notices if item[0] != method), maxlen=16)
        else:
            self._notices = deque((item for item in self._notices if item[0] != 'task_progress'), maxlen=16)
        self._notices.append((method, args))
        self._notice_timer.start()

    def _flush_notices(self):
        if getattr(self, 'exit_reason', None) is not None:
            self._notice_timer.stop()
            self._notices.clear()
            return
        if self._quota_lease() or self.window_policy().settings_open():
            return
        if not self._notices:
            self._notice_timer.stop()
            return
        method, args = self._notices.popleft()
        getattr(self, method)(*args)
        if not self._notices:
            self._notice_timer.stop()

    def task_progress(self, title, text, details):
        self._request_notice('task_progress', (title, text, details), 12000)

    def task_result(self, title, text, details):
        self._request_notice('task_result', (title, text, details), 20000)

    def task_completed(self, text, details):
        self.play_click_sound()
        celebration = next((item for item in self.variants if item.get('id') == '18'), None)
        if celebration and not self.dock_edge:
            self.play(celebration, 'task-celebration')
        self._request_notice('task_completed', (text, details), 20000)

    def apply_visibility(self):
        super().apply_visibility()
        if self.manual_hidden or self.fullscreen_hidden:
            self.close_notification()
            if self.settings_panel is not None:
                self.settings_panel.hide()
            if self.quota_panel is not None:
                self.quota_panel.hide()
        for name in ('toolbar', 'activity_cards'):
            control = getattr(self, name, None)
            if control is not None:
                control.sync_visibility()
