"""One styled settings window backed by upstream preferences."""
from bailong_theme import PANEL_STYLE

SETTINGS_SIZE = (350, 460)


def position_settings(pet, panel):
    from PySide6.QtWidgets import QApplication
    from companion_layout import place_side_window, companion_owner, visible_obstacles
    from ui_motion import move_window
    owner = companion_owner(pet)
    # Clear the wider controls/cards horizontally while retaining vertical center.
    # Independent speech keeps its own placement/layer policy.
    obstacles = visible_obstacles(pet, ('toolbar', 'activity_cards'))
    screens = [pet.screen(), *[screen for screen in QApplication.screens() if screen is not pet.screen()]]
    current_side = getattr(panel, '_placement_side', None)
    width, height = SETTINGS_SIZE
    # Retain the full settings size even when space on this side is limited.
    for side in ((current_side, None) if current_side in ('left', 'right') else (None,)):
        for screen in screens:
            if screen is None:
                continue
            area = screen.availableGeometry()
            point = place_side_window((width, height), owner,
                                      (area.x(), area.y(), area.width(), area.height()), obstacles, gap=12,
                                      preferred_side=side, allowed_sides=(side,) if side else ('left', 'right'),
                                      centered=True)
            if point is not None:
                panel._placement_side = 'left' if point[0] + width <= owner[0] else 'right'
                move_window(panel, point, pet, 'settings')
                return True
    if not getattr(panel, '_making_room', False) and not pet.dock_edge:
        before = pet.pos()
        panel._making_room = True
        try:
            overhang = max((max(owner[0] - x, x + w - owner[0] - owner[2], 0)
                            for x, y, w, h in obstacles), default=0)
            pet.ensure_quota_side_space(width + overhang + 4)
            if pet.pos() != before:
                return position_settings(pet, panel)
        finally:
            panel._making_room = False
    return False


def create_settings(pet):
    from ui_motion import MotionWindowMixin
    from PySide6.QtCore import Qt, QEvent, QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import (QDialog, QFrame, QVBoxLayout, QHBoxLayout,
                                 QLabel, QSlider, QPushButton, QCheckBox, QScrollArea, QWidget)

    class Settings(MotionWindowMixin, QDialog):
        def __init__(self):
            super().__init__(pet, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                             | Qt.WindowType.WindowStaysOnTopHint)
            self.setObjectName('BailongSettings')
            self.setWindowTitle('白龙设置')
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setFont(QFont('Microsoft YaHei UI', 10))
            self.setFixedSize(*SETTINGS_SIZE)
            self.setStyleSheet(PANEL_STYLE)
            outer = QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            panel = QFrame(self)
            panel.setObjectName('SettingsPanel')
            outer.addWidget(panel)
            chrome = QVBoxLayout(panel)
            chrome.setContentsMargins(20, 16, 20, 16)
            chrome.setSpacing(10)
            title = QLabel('白龙设置')
            title.setProperty('section', True)
            chrome.addWidget(title)
            scroll = QScrollArea(panel)
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setMinimumHeight(60)
            scroll.viewport().setAutoFillBackground(False)
            content = QWidget()
            content.setStyleSheet('background:transparent;')
            layout = QVBoxLayout(content)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(10)
            scroll.setWidget(content)
            chrome.addWidget(scroll, 1)
            self.scroll = scroll
            self.sliders = {}
            for key, caption, lower, upper, callback in (
                ('size_percent', '桌宠大小', 50, 200, pet.set_size_percent),
                ('bubble_percent', '气泡大小', 50, 150, pet.set_bubble_percent),
            ):
                row = QHBoxLayout()
                label = QLabel(caption)
                value = QLabel()
                reset = QPushButton('默认')
                reset.setToolTip('恢复为 100%')
                row.addWidget(label)
                row.addStretch()
                row.addWidget(value)
                row.addWidget(reset)
                layout.addLayout(row)
                slider = QSlider(Qt.Orientation.Horizontal)
                slider.setObjectName(key)
                slider.setRange(lower, upper)
                slider.setAccessibleName(caption)
                slider.valueChanged.connect(callback)
                slider.valueChanged.connect(lambda v, label=value: label.setText(f'{v}%'))
                reset.clicked.connect(lambda checked=False, slider=slider: slider.setValue(100))
                layout.addWidget(slider)
                self.sliders[key] = (slider, value)
            self.checks = {}
            for key, caption, attr, callback in (
                ('movement', '自动移动', 'auto_movement_disabled', pet.set_auto_movement_disabled),
                ('fixed', '固定桌宠', 'fixed_pet', pet.set_fixed_pet),
                ('fullscreen', '全屏时自动隐藏', 'auto_hide_fullscreen', pet.set_auto_hide_fullscreen),
                ('notifications', 'Codex 任务气泡提醒', 'task_bubbles_disabled', pet.set_task_bubbles_disabled),
                ('sound', '播放音效', 'sound_disabled', pet.set_sound_disabled),
            ):
                checkbox = QCheckBox(caption)
                checkbox.setObjectName(key)
                inverted = key in ('movement', 'notifications', 'sound')
                checkbox.toggled.connect(lambda on, fn=callback, inv=inverted: fn(not on if inv else on))
                self.checks[key] = (checkbox, attr, inverted)
                if key == 'fixed':
                    checkbox.setToolTip('自发移动时只播放动画；用户拖拽或手动选择移动动作仍可改变位置。')
                layout.addWidget(checkbox)
            hint = QLabel('关闭任务气泡后，互动、额度和任务卡仍可使用。')
            hint.setWordWrap(True)
            hint.setStyleSheet('color:#89739f;font-size:11px;')
            layout.addWidget(hint)
            close = QPushButton('完成')
            close.clicked.connect(self.accept)
            chrome.addWidget(close)
            self._position_pending = False
            pet.installEventFilter(self)

        def reposition(self):
            self._position_pending = False
            if self.isVisible() and not position_settings(pet, self):
                self.hide()

        def eventFilter(self, watched, event):
            if (watched is pet and self.isVisible()
                    and event.type() in (QEvent.Type.Move, QEvent.Type.Resize)
                    and not self._position_pending):
                self._position_pending = True
                QTimer.singleShot(0, self.reposition)
            return super().eventFilter(watched, event)

        def refresh(self):
            for key, (slider, label) in self.sliders.items():
                value = getattr(pet, key)
                slider.blockSignals(True)
                slider.setValue(value)
                slider.blockSignals(False)
                label.setText(f'{value}%')
            for checkbox, attr, inverted in self.checks.values():
                checkbox.blockSignals(True)
                value = bool(getattr(pet, attr))
                checkbox.setChecked(not value if inverted else value)
                checkbox.blockSignals(False)

        def showEvent(self, event):
            pet.note_interaction()
            super().showEvent(event)

        def hideEvent(self, event):
            pet.note_interaction()
            super().hideEvent(event)
            pet.window_policy().settings_closed()

    return Settings()
