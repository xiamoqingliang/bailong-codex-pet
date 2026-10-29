"""Screen-contained, scrollable quota results independent of work animation."""
from datetime import datetime
from bailong_theme import PANEL_STYLE


def create_quota_panel(pet):
    from PySide6.QtCore import Qt, QTimer, QRectF, QVariantAnimation, QEasingCurve
    from ui_motion import CONTENT_MS, visible, fade_content
    from PySide6.QtGui import QFont, QPainter, QColor
    from PySide6.QtWidgets import (QDialog, QFrame, QWidget, QScrollArea, QVBoxLayout,
                                 QHBoxLayout, QLabel, QPushButton, QSizePolicy)

    class RoundedQuotaBar(QWidget):
        def __init__(self):
            super().__init__()
            self.setFixedHeight(12)
            self._minimum, self._maximum, self._value = 0, 100, 0
            self._display_value = 0.0
            self._fill_animation = QVariantAnimation(self)
            self._fill_animation.setDuration(CONTENT_MS)
            self._fill_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
            self._fill_animation.valueChanged.connect(self._fill_changed)
            self._phase = 0.0
            self._timer = QTimer(self)
            self._timer.setInterval(30)
            self._timer.timeout.connect(self._advance)

        def setRange(self, minimum, maximum):
            self._minimum, self._maximum = minimum, maximum
            if minimum == maximum and self.isVisible():
                self._timer.start()
            else:
                self._timer.stop()
            self.update()

        def setValue(self, value):
            self._value = max(self._minimum, min(value, self._maximum))
            if not getattr(pet, '_ui_motion_enabled', False) or getattr(pet, '_ui_motion_disabled', False):
                self._display_value = float(self._value)
            elif self.isVisible():
                self._start_fill()
            self.update()

        def _fill_changed(self, value):
            self._display_value = float(value)
            self.update()

        def _start_fill(self):
            self._fill_animation.stop()
            self._fill_animation.setStartValue(self._display_value)
            self._fill_animation.setEndValue(float(self._value))
            self._fill_animation.start()

        def value(self):
            return self._value

        def setTextVisible(self, visible):
            pass

        def _advance(self):
            self._phase = (self._phase + 0.0125) % 2
            self.update()

        def showEvent(self, event):
            if self._maximum == self._minimum:
                self._timer.start()
            elif getattr(pet, '_ui_motion_enabled', False) and not getattr(pet, '_ui_motion_disabled', False):
                self._start_fill()
            super().showEvent(event)

        def hideEvent(self, event):
            self._timer.stop()
            self._fill_animation.stop()
            super().hideEvent(event)

        def paintEvent(self, event):
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor('#eee5f7'))
            painter.drawRoundedRect(QRectF(self.rect()), self.height() / 2, self.height() / 2)
            if self._maximum == self._minimum:
                width = self.width() * 0.35
                fraction = self._phase if self._phase <= 1 else 2 - self._phase
                left = (self.width() - width) * fraction
            else:
                width = self.width() * (self._display_value - self._minimum) / max(1, self._maximum - self._minimum)
                left = 0
            if width > 0:
                painter.setBrush(QColor('#aa8ac6'))
                radius = min(self.height() / 2, width / 2)
                painter.drawRoundedRect(QRectF(left, 0, width, self.height()), radius, radius)

    from ui_motion import MotionWindowMixin

    class QuotaPanel(MotionWindowMixin, QDialog):
        def __init__(self):
            super().__init__(pet, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                             | Qt.WindowType.WindowStaysOnTopHint)
            self.setWindowTitle('Codex 剩余额度')
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            self.setMinimumSize(250, 80)
            self.setFont(QFont('Microsoft YaHei UI', 9))
            self.setStyleSheet(PANEL_STYLE + '''
                QProgressBar { background:#eee5f7; border:0; border-radius:4px; height:8px; }
                QProgressBar::chunk { background:#aa8ac6; border-radius:4px; }
            ''')
            outer = QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            panel = QFrame(self)
            panel.setObjectName('SettingsPanel')
            outer.addWidget(panel)
            layout = QVBoxLayout(panel)
            layout.setContentsMargins(16, 12, 16, 12)
            self.chrome_layout = layout
            header = QHBoxLayout()
            title = QLabel('Codex 剩余额度')
            title.setProperty('section', True)
            close = QPushButton('×')
            close.setFixedWidth(28)
            close.setStyleSheet('padding:0;')
            close.setAccessibleName('关闭额度面板')
            close.clicked.connect(pet.dismiss_quota)
            header.addWidget(title)
            header.addStretch()
            header.addWidget(close)
            layout.addLayout(header)
            self.header_layout = header
            self.scroll = QScrollArea()
            self.scroll.setWidgetResizable(True)
            self.scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
            self.scroll.setMinimumHeight(0)
            self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.scroll.viewport().setAutoFillBackground(False)
            layout.addWidget(self.scroll, 1)

        def display(self, windows, loading=False, error=None):
            replacing = visible(self)
            content = QWidget()
            content.setStyleSheet('background:transparent;')
            layout = QVBoxLayout(content)
            layout.setContentsMargins(0, 2, 4, 2)
            layout.setSpacing(10)
            for window in windows:
                value = window.get('remaining')
                label = QLabel(str(window.get('name', '额度')) + ' · ' +
                               (f'剩余 {value:g}%' if value is not None else '暂未提供'))
                label.setWordWrap(True)
                label.setTextFormat(Qt.TextFormat.PlainText)
                layout.addWidget(label)
                bar = RoundedQuotaBar()
                bar.setRange(0, 100)
                bar.setValue(round(value) if value is not None else 0)
                bar.setTextVisible(False)
                layout.addWidget(bar)
                if window.get('resetsAt'):
                    reset = QLabel('重置：' + datetime.fromtimestamp(window['resetsAt']).strftime('%m月%d日 %H:%M'))
                    reset.setWordWrap(True)
                    layout.addWidget(reset)
            if not windows:
                message = '正在查询额度…' if loading else ('与 Codex 连接失败，暂时无法查询额度。\n' + str(error) if error else '当前未提供额度窗口。')
                label = QLabel(message)
                label.setTextFormat(Qt.TextFormat.PlainText)
                label.setWordWrap(True)
                layout.addWidget(label)
                if loading:
                    bar = RoundedQuotaBar()
                    bar.setRange(0, 0)
                    layout.addWidget(bar)
            self.scroll.setWidget(content)
            if replacing:
                fade_content(content, pet)
            self.bars = content.findChildren(RoundedQuotaBar)
            content.ensurePolished()
            width = max(250, round(280 * pet.bubble_percent / 100))
            margins = self.chrome_layout.contentsMargins()
            body_width = width - margins.left() - margins.right() - 2 * self.scroll.frameWidth()
            body_height = layout.heightForWidth(body_width) if layout.hasHeightForWidth() else layout.sizeHint().height()
            self.content_height = max(0, body_height, layout.sizeHint().height())
            border = self.chrome_layout.parentWidget().frameWidth()
            self.natural_height = max(80, margins.top() + margins.bottom() + self.header_layout.sizeHint().height()
                                     + self.chrome_layout.spacing() + self.content_height
                                     + 2 * self.scroll.frameWidth() + 2 * border)
            if self.reposition():
                self.show()
                self.raise_()

        def reposition(self):
            if getattr(self, '_positioning', False):
                return True
            self._positioning = True
            try:
                return self._position_side()
            finally:
                self._positioning = False

        def _position_side(self, allow_nudge=True):
            from companion_layout import place_side_window, companion_owner, visible_obstacles
            from ui_motion import move_window, resize_window
            area = pet.screen().availableGeometry()
            bounds = (area.x(), area.y(), area.width(), area.height())
            obstacles = visible_obstacles(pet, ('toolbar', 'settings_panel', 'composer', 'activity_cards', 'bubble'))
            natural_width = max(250, round(280 * pet.bubble_percent / 100))
            owner = companion_owner(pet)
            current_side = getattr(self, '_placement_side', None)
            for side in ((current_side, None) if current_side in ('left', 'right') else (None,)):
                for width, height in ((natural_width, min(self.natural_height, area.height())),
                                      (250, min(self.natural_height, area.height())),
                                      (min(natural_width, 280), min(self.natural_height, 240)),
                                      (250, min(self.natural_height, 180)), (250, min(self.natural_height, 120))):
                    point = place_side_window((width, height), owner, bounds, obstacles,
                                              preferred_side=side, allowed_sides=(side,) if side else ('left', 'right'))
                    if point is not None:
                        self._placement_side = 'left' if point[0] + width <= owner[0] else 'right'
                        resize_window(self, (width, height), pet, 'quota')
                        move_window(self, point, pet, 'quota')
                        return True
            if allow_nudge:
                before = pet.pos()
                pet.ensure_quota_side_space(250)
                if before != pet.pos():
                    return self._position_side(False)
            return False

        def reject(self):
            pet.dismiss_quota()

    return QuotaPanel()
