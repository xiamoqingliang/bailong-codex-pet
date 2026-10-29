"""White/lilac activity cards driven only by supplied session metadata.

This module reads no Codex data, starts no observer, opens no URI and changes no
pet setting. Qt imports are deferred until create_activity_cards(), after the
original engine's bundled runtime has been loaded. Activation returns only the
opaque key supplied by the caller.
"""
from __future__ import annotations

import unicodedata
from html import escape


CARD_WIDTH = 304
CARD_HEIGHT = 110
STACK_OFFSET = 8
MAX_VISIBLE_CARDS = 3
GAP = 8
STATE_PRIORITY = {
    'needs_input': 0, 'failed': 1, 'completed': 2,
    'running': 3, 'unknown': 4, 'idle': 5,
}
STATE_LABELS = {
    'needs_input': '等待确认', 'failed': '遇到问题', 'completed': '已完成',
    'running': '正在执行', 'unknown': '等待状态更新', 'idle': '空闲',
}
STATE_COLORS = {
    'needs_input': '#946820', 'failed': '#ac5475', 'completed': '#4f8172',
    'running': '#8061a3', 'unknown': '#8d8299', 'idle': '#8d8299',
}


def plain_text(value, limit):
    """Keep titles as text, remove control/bidi formatting and bound length."""
    if not isinstance(value, str):
        return ''
    clean = ''.join(' ' if character.isspace() else character for character in value
                    if character.isspace() or unicodedata.category(character)[0] != 'C')
    return ' '.join(clean.split())[:limit]


def safe_tooltip(value):
    # Qt tooltips auto-detect rich text; metadata must never become markup.
    return '<qt>' + escape(value).replace('\n', '<br>') + '</qt>'


def normalize_sessions(values):
    """Whitelist key/title/project/state; discard all other payload fields."""
    if not isinstance(values, (list, tuple)):
        return []
    selected = {}
    for item in values:
        if not isinstance(item, dict):
            continue
        key = item.get('key')
        if (not isinstance(key, str) or not key or len(key) > 512
                or any(unicodedata.category(character)[0] == 'C' for character in key)):
            continue
        state = item.get('state')
        state = state.strip().lower() if isinstance(state, str) else 'unknown'
        if state not in STATE_PRIORITY:
            state = 'unknown'
        selected[key] = {
            'key': key,
            'title': plain_text(item.get('title'), 240) or '未命名任务',
            'project': plain_text(item.get('project'), 100),
            'state': state,
        }
    return sorted(selected.values(), key=lambda item: STATE_PRIORITY[item['state']])


def _intersection_area(a, b):
    return max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])) * max(
        0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))


def choose_stack_position(owner, size, available, obstacles=()):
    """Find a screen-contained placement without moving/covering the pet.

    Inputs use Qt logical pixels, including negative multi-monitor origins.
    Prefer above, then either side. Below the controls is a narrow-screen
    fallback; the least-overlapping position is used only if none can fit.
    """
    from companion_layout import place_vertical_window
    point = place_vertical_window(size, owner, available, obstacles, preferred_side="above")
    return point


def create_activity_cards(original, pet, on_activate=None):
    """Return an inactive-content card stack with no data acquisition side effects.

    Public API: set_sessions(), set_active(), sync_geometry(), sync_visibility(),
    activate_current(), cycle_next(); current_key/current_session/sessions and
    visible_depth are exposed for the host and diagnostics.
    """
    from PySide6.QtCore import QEvent, QPoint, Qt
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication, QFrame, QLabel, QMenu, QPushButton, QWidget

    from ui_motion import MotionWindowMixin

    class ActivityCards(MotionWindowMixin, QWidget):
        def __init__(self):
            super().__init__(pet, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                             | Qt.WindowType.WindowStaysOnTopHint)
            self.owner = pet
            self.on_activate = on_activate
            self.sessions = []
            self.current_key = None
            self._active = True
            self._syncing = False
            self._menu = None
            self.visible_depth = 0
            self.setWindowTitle('白龙项目状态')
            self.setObjectName('BailongActivityCards')
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            self.setFont(QFont('Microsoft YaHei UI', 9))

            self.back_cards = []
            for index in range(MAX_VISIBLE_CARDS - 1):
                back = QFrame(self)
                back.setObjectName('BailongActivityBack' + str(index))
                back.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                color = '#f1eaf8' if index == 0 else '#e9e0f2'
                back.setStyleSheet(f'background:{color};border:1px solid #cdbce4;border-radius:14px;')
                self.back_cards.append(back)

            self.open_button = QPushButton(self)
            self.open_button.setObjectName('BailongActivityOpen')
            self.open_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.open_button.clicked.connect(self.activate_current)
            self.open_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

            self.project_label = QLabel(self.open_button)
            self.project_label.setObjectName('BailongActivityProject')
            self.title_label = QLabel(self.open_button)
            self.title_label.setObjectName('BailongActivityTitle')
            title_font = QFont('Microsoft YaHei UI', 10)
            title_font.setBold(True)
            self.title_label.setFont(title_font)
            self.status_label = QLabel(self.open_button)
            self.status_label.setObjectName('BailongActivityStatus')
            for label in (self.project_label, self.title_label, self.status_label):
                label.setTextFormat(Qt.TextFormat.PlainText)
                label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

            self.count_label = QLabel(self.open_button)
            self.count_label.setTextFormat(Qt.TextFormat.PlainText)
            self.count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.count_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self.next_button = QPushButton('›', self.open_button)
            self.next_button.setObjectName('BailongActivityNext')
            self.next_button.setAccessibleName('查看下一个任务')
            self.next_button.setToolTip('查看下一个任务')
            self.next_button.clicked.connect(self.cycle_next)
            self.list_button = QPushButton('...', self.open_button)
            self.list_button.setObjectName('BailongActivityList')
            self.list_button.clicked.connect(self.open_list)
            for button in (self.next_button, self.list_button):
                button.setCursor(Qt.CursorShape.PointingHandCursor)

            self.setStyleSheet('''
                QPushButton#BailongActivityOpen {
                    background:#fbf9ff; border:1px solid #c7b6e5; border-radius:14px;
                }
                QPushButton#BailongActivityOpen:hover { background:#f8f3fd; }
                QPushButton#BailongActivityOpen:pressed { background:#f0e6f8; }
                QPushButton#BailongActivityOpen:focus { border:1px solid #9473b7; }
                QLabel { background:transparent; border:none; color:#7e698e; }
                QLabel#BailongActivityTitle { color:#503963; }
                QPushButton#BailongActivityNext, QPushButton#BailongActivityList {
                    background:#f0eaf8; color:#73558e; border:none; border-radius:7px;
                    font-size:18px; padding:0;
                }
                QPushButton#BailongActivityNext:hover, QPushButton#BailongActivityList:hover {
                    background:#e5d8f4;
                }
            ''')
            pet.installEventFilter(self)
            self._layout_cards()

        @property
        def current_session(self):
            return next((item for item in self.sessions if item['key'] == self.current_key), None)

        def set_sessions(self, values):
            incoming = normalize_sessions(values)
            old_priority = STATE_PRIORITY[self.sessions[0]['state']] if self.sessions else 99
            if incoming == self.sessions:
                self.sync_visibility()
                return
            self.sessions = incoming
            keys = {item['key'] for item in incoming}
            if (self.current_key not in keys or
                    (incoming and STATE_PRIORITY[incoming[0]['state']] < old_priority)):
                self.current_key = incoming[0]['key'] if incoming else None
            if not incoming and self._menu is not None:
                self._menu.hide()
            self._layout_cards()
            self._render_current()
            self.sync_visibility()

        def _layout_cards(self):
            self.visible_depth = min(MAX_VISIBLE_CARDS, len(self.sessions))
            offset = max(0, self.visible_depth - 1) * STACK_OFFSET
            width = CARD_WIDTH
            screen = self.owner.screen() or QApplication.primaryScreen()
            if screen is not None:
                width = min(width, max(180, screen.availableGeometry().width() - offset - 16))
            self.setFixedSize(width + offset, CARD_HEIGHT + offset)
            # Third card at the back, second in the middle, first at the front.
            for index in reversed(range(len(self.back_cards))):
                depth = index + 1
                card = self.back_cards[index]
                card.setVisible(depth < self.visible_depth)
                card.setGeometry(depth * STACK_OFFSET, offset - depth * STACK_OFFSET, width, CARD_HEIGHT)
                card.raise_()
            self.open_button.setGeometry(0, offset, width, CARD_HEIGHT)
            self.open_button.raise_()
            self.project_label.setGeometry(14, 10, max(40, width - 128), 20)
            self.count_label.setGeometry(width - 107, 9, 38, 22)
            self.next_button.setGeometry(width - 65, 8, 24, 25)
            self.list_button.setGeometry(width - 36, 8, 24, 25)
            self.title_label.setGeometry(14, 36, width - 28, 28)
            self.status_label.setGeometry(14, 73, width - 28, 22)
            multiple = len(self.sessions) > 1
            self.next_button.setVisible(multiple)
            self.list_button.setVisible(multiple)
            self.count_label.setVisible(multiple)

        def _render_current(self):
            item = self.current_session
            if item is None:
                self.project_label.clear()
                self.title_label.clear()
                self.status_label.clear()
                self.open_button.setAccessibleName('暂无项目活动')
                return
            title = item['title']
            project = item['project'] or 'Codex 任务'
            state_label = STATE_LABELS[item['state']]
            self.project_label.setText(self.project_label.fontMetrics().elidedText(
                project, Qt.TextElideMode.ElideRight, self.project_label.width()))
            self.title_label.setText(self.title_label.fontMetrics().elidedText(
                title, Qt.TextElideMode.ElideRight, self.title_label.width()))
            self.status_label.setText('●  ' + state_label)
            self.status_label.setStyleSheet(f'color:{STATE_COLORS[item["state"]]};background:transparent;border:none;')
            tooltip = '\n'.join(part for part in (item['project'], title, state_label) if part)
            self.open_button.setToolTip(safe_tooltip(tooltip))
            self.open_button.setAccessibleName(f'打开聊天：{title}，{state_label}')
            self.count_label.setText(f'{self.sessions.index(item) + 1}/{len(self.sessions)}')
            self.list_button.setAccessibleName(f'查看全部 {len(self.sessions)} 个任务')
            self.list_button.setToolTip(f'查看全部 {len(self.sessions)} 个任务')

        def activate_current(self):
            if self.current_session is not None and self.on_activate is not None:
                self.on_activate(self.current_key)

        def cycle_next(self):
            if not self.sessions:
                return
            index = next((i for i, item in enumerate(self.sessions) if item['key'] == self.current_key), 0)
            self.current_key = self.sessions[(index + 1) % len(self.sessions)]['key']
            self._render_current()

        def select_session(self, key, activate=False):
            if not any(item['key'] == key for item in self.sessions):
                return
            self.current_key = key
            self._render_current()
            if activate:
                self.activate_current()

        def open_list(self):
            if not self.sessions:
                return
            if self._menu is not None:
                self._menu.close()
            menu = QMenu(self)
            self._menu = menu
            menu.setStyleSheet('''
                QMenu { background:#fbf9ff; color:#574369; border:1px solid #c7b6e5; padding:5px; }
                QMenu::item { padding:7px 18px 7px 10px; border-radius:5px; }
                QMenu::item:selected { background:#e5d8f4; }
            ''')
            for item in self.sessions:
                title = (item['project'] + ' / ' if item['project'] else '') + item['title']
                text = f"{STATE_LABELS[item['state']]} · {title[:100]}".replace('&', '&&')
                action = menu.addAction(text)
                action.setToolTip(safe_tooltip(title))
                action.setData(item['key'])
                action.triggered.connect(lambda checked=False, key=item['key']: self.select_session(key, True))

            def released():
                if self._menu is menu:
                    self._menu = None
                menu.deleteLater()
            menu.aboutToHide.connect(released)
            menu.popup(self.list_button.mapToGlobal(QPoint(0, self.list_button.height() + 4)))

        def sync_geometry(self):
            if self._syncing:
                return
            self._syncing = True
            try:
                from companion_layout import layout_companions
                layout_companions(self.owner, cards=self)
            finally:
                self._syncing = False

        def sync_visibility(self):
            visible = (self._active and bool(self.sessions) and self.owner.isVisible()
                       and not getattr(self.owner, 'manual_hidden', False)
                       and not getattr(self.owner, 'fullscreen_hidden', False)
                       and not getattr(self.owner, '_local_hide_animation', False)
                       and (not getattr(self.owner, 'dock_edge', None) or
                            bool(getattr(getattr(self.owner, 'dock_controls', None), 'visible', False))))
            if visible:
                self.sync_geometry()
                visible = getattr(self, "_space_available", True)
            from bailong_theme import reveal_window
            reveal_window(self, visible)
            if not visible and self._menu is not None:
                self._menu.hide()

        def set_active(self, active):
            self._active = bool(active)
            self.sync_visibility()

        def eventFilter(self, watched, event):
            if watched is self.owner:
                if event.type() in (QEvent.Type.Move, QEvent.Type.Resize):
                    self.sync_geometry()
                elif event.type() in (QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.WindowStateChange):
                    self.sync_visibility()
            return super().eventFilter(watched, event)

        def closeEvent(self, event):
            # Closing this optional tray never closes or hides the pet itself.
            event.ignore()
            self.set_active(False)

    return ActivityCards()
