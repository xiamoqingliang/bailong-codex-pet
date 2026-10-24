"""White/lilac controls for the full Bailong engine.

Importing this module is safe before ``bridge_loader.load_original_module``:
Qt is imported only inside ``create_toolbar``. The returned QWidget exposes
``sync_geometry()``, ``sync_visibility(active=None)``, ``set_active(active)``,
and ``constrained_owner_position(position, pointer=None)``. Keep a reference on
the pet. In the LinkedPet.constrained_position override, pass the point returned
by super() to constrained_owner_position to reserve the toolbar and composer.

No chat or quota request is made until the user clicks an appropriate control.
The original engine owns animation, history, chat, quota, menus and sounds.
"""

from __future__ import annotations


TOOLBAR_WIDTH = 280
TOOLBAR_HEIGHT = 46
PET_GAP = 6
COMPOSER_GAP = 8


def create_toolbar(original, pet, on_hide=None):
    """Create a pet-owned top-level toolbar; do not show or run an event loop.

    ``on_hide`` is an optional no-argument callback supplied by the bridge to
    persist/synchronize the user's hide action. The default uses the original
    tray-recoverable ``pet.set_manual_hidden(True)``.
    """
    from PySide6.QtCore import QEvent, QPoint, Qt
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import (
        QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QWidget,
    )

    class BailongToolbar(QWidget):
        def __init__(self):
            super().__init__(pet, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                             | Qt.WindowType.WindowStaysOnTopHint)
            self.owner = pet
            self.on_hide = on_hide
            self._active = True
            self._syncing = False
            self._composer = None
            self._action_menu = None
            self._press_global = None
            self._press_origin = None
            self._dragging = False
            self.setWindowTitle("白龙专属控制栏")
            self.setObjectName("BailongToolbar")
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            self.setFixedSize(TOOLBAR_WIDTH, TOOLBAR_HEIGHT)
            self.setFont(QFont("Microsoft YaHei UI", 9))

            outer = QHBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            self.panel = QFrame(self)
            self.panel.setObjectName("BailongPanel")
            outer.addWidget(self.panel)
            row = QHBoxLayout(self.panel)
            row.setContentsMargins(10, 6, 8, 6)
            row.setSpacing(3)
            self.title = QLabel("白龙", self.panel)
            self.title.setObjectName("BailongTitle")
            self.title.setFixedWidth(36)
            self.title.setToolTip("白龙完整版 · 拖动这里移动白龙和控制栏")
            self.title.setCursor(Qt.CursorShape.SizeAllCursor)
            row.addWidget(self.title)

            self.buttons = {}
            actions = (
                ("chat", "聊天", "在白龙中操作同一 Codex 聊天并同步回复，可选择工作文件夹", self._toggle_chat),
                ("history", "历史", "打开白龙原版历史对话", self._open_history),
                ("quota", "额度", "查询 Codex 剩余额度（点击后联网查询）", self._quota),
                ("actions", "动作", "白龙完整动作、待机和显示设置菜单", self._open_actions),
            )
            for key, text, tooltip, callback in actions:
                button = QPushButton(text, self.panel)
                button.setObjectName("Bailong_" + key)
                button.setFixedSize(43, 32)
                button.setToolTip(tooltip)
                button.setAccessibleName(tooltip)
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
                button.clicked.connect(callback)
                row.addWidget(button)
                self.buttons[key] = button
            self.buttons["chat"].setCheckable(True)
            row.addStretch(1)
            self.hide_button = QPushButton("−", self.panel)
            self.hide_button.setObjectName("BailongHide")
            self.hide_button.setFixedSize(24, 28)
            self.hide_button.setToolTip("隐藏白龙和专属控制栏；可从 Codex 的显示 Mini 或白龙托盘恢复")
            self.hide_button.setAccessibleName("隐藏白龙和专属控制栏")
            self.hide_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.hide_button.clicked.connect(self.request_hide)
            row.addWidget(self.hide_button)
            self.setStyleSheet("""
                QFrame#BailongPanel {
                    background-color: #fbf9ff;
                    border: 1px solid #c7b6e5;
                    border-radius: 15px;
                }
                QLabel#BailongTitle {
                    color: #6b4d91; background: transparent;
                    border: none; font-weight: 600;
                }
                QPushButton {
                    color: #574369; background-color: #f0eaf8;
                    border: 1px solid transparent; border-radius: 9px;
                    padding: 0px;
                }
                QPushButton:hover { background-color: #e5d8f4; }
                QPushButton:pressed, QPushButton:checked {
                    background-color: #d9c7ed; color: #442c62;
                }
                QPushButton:focus { border: 1px solid #9473b7; }
                QPushButton#BailongHide {
                    background-color: transparent; color: #89739f;
                    font-size: 17px;
                }
                QPushButton#BailongHide:hover { background-color: #eee5f7; }
            """)
            self.panel.installEventFilter(self)
            self.title.installEventFilter(self)
            pet.installEventFilter(self)

        def _current_composer(self):
            chat = getattr(self.owner, "chat", None)
            composer = getattr(chat, "composer", None)
            if composer is not None and composer is not self._composer:
                if self._composer is not None:
                    self._composer.removeEventFilter(self)
                self._composer = composer
                composer.installEventFilter(self)
            return composer if chat and chat.enabled else None

        def constrained_owner_position(self, position, pointer=None):
            """Clamp a point after the original Pet clamp, reserving our bar.

            The group remains centered under the pet. Coordinates and sizes
            are Qt logical pixels, including on mixed-DPI monitor setups.
            """
            position = QPoint(position)
            screen = QApplication.screenAt(pointer) if pointer is not None else None
            if screen is None:
                screen = QApplication.screenAt(position + self.owner.rect().center())
            if screen is None:
                screen = self.owner.screen() or QApplication.primaryScreen()
            if screen is None:
                return position
            area = screen.availableGeometry()
            composer = self._current_composer()
            width = max(self.owner.width(), self.width(), composer.width() if composer else 0)
            height = self.owner.height() + PET_GAP + self.height()
            if composer:
                height += COMPOSER_GAP + composer.height()
            shift = (width - self.owner.width()) // 2
            left = area.left() + shift
            right = max(left, area.right() + 1 - width + shift)
            bottom = max(area.top(), area.bottom() + 1 - height)
            return QPoint(max(left, min(position.x(), right)),
                          max(area.top(), min(position.y(), bottom)))

        def sync_geometry(self):
            """Attach the bar and original composer below the pet immediately."""
            if self._syncing:
                return
            self._syncing = True
            try:
                composer = self._current_composer()
                position = self.constrained_owner_position(self.owner.pos())
                if position != self.owner.pos():
                    self.owner.move(position)
                x = self.owner.x() + (self.owner.width() - self.width()) // 2
                y = self.owner.y() + self.owner.height() + PET_GAP
                if self.pos() != QPoint(x, y):
                    self.move(x, y)
                if composer is not None:
                    composer.move(self.owner.x() + (self.owner.width() - composer.width()) // 2,
                                  y + self.height() + COMPOSER_GAP)
                self.buttons["chat"].setChecked(composer is not None)
            finally:
                self._syncing = False

        def sync_visibility(self, active=None):
            """Follow owner hide/fullscreen state and the bridge's selection."""
            if active is not None:
                self._active = bool(active)
            visible = (self._active and self.owner.isVisible()
                       and not getattr(self.owner, "manual_hidden", False)
                       and not getattr(self.owner, "fullscreen_hidden", False))
            if visible:
                self.sync_geometry()
            if visible != self.isVisible():
                self.setVisible(visible)
            if not visible:
                self._press_global = None
                self._dragging = False
                if self._action_menu is not None:
                    self._action_menu.hide()

        def set_active(self, active):
            self.sync_visibility(active)

        def request_hide(self):
            if self.on_hide is not None:
                self.on_hide()
            else:
                self.owner.set_manual_hidden(True)
            self.sync_visibility()

        def _toggle_chat(self):
            self.owner.toggle_chat()
            self.sync_geometry()
            self.sync_visibility()

        def _open_history(self):
            self.owner.open_history()
            self.sync_geometry()

        def _quota(self):
            self.owner.quota()

        def _open_actions(self):
            if self._action_menu is not None:
                self._action_menu.close()
            menu = self.owner.context_menu()
            self._action_menu = menu
            menu.setStyleSheet("""
                QMenu { background: #fbf9ff; color: #574369;
                        border: 1px solid #c7b6e5; padding: 5px; }
                QMenu::item { padding: 6px 22px 6px 12px; border-radius: 5px; }
                QMenu::item:selected { background: #e5d8f4; }
                QMenu::separator { background: #ded2ed; height: 1px; margin: 4px; }
                QMenu::item:disabled { color: #a398ac; }
            """)

            def release_menu():
                if self._action_menu is menu:
                    self._action_menu = None
                menu.deleteLater()

            menu.aboutToHide.connect(release_menu)
            button = self.buttons["actions"]
            menu.popup(button.mapToGlobal(QPoint(0, button.height() + 4)))

        def _drag_event(self, event):
            kind = event.type()
            if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._press_global = event.globalPosition().toPoint()
                self._press_origin = self.owner.pos()
                self._dragging = False
                return True
            if kind == QEvent.Type.MouseMove and self._press_global is not None:
                if not event.buttons() & Qt.MouseButton.LeftButton:
                    self._press_global = None
                    self._dragging = False
                    return False
                pointer = event.globalPosition().toPoint()
                delta = pointer - self._press_global
                if delta.manhattanLength() >= QApplication.startDragDistance():
                    self._dragging = True
                if self._dragging:
                    point = self.owner.constrained_position(self._press_origin + delta, pointer)
                    self.owner.move(self.constrained_owner_position(point, pointer))
                    self.owner.position_bubble()
                    self.sync_geometry()
                return True
            if kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                self._press_global = None
                self._dragging = False
                return True
            return False

        def eventFilter(self, watched, event):
            kind = event.type()
            if watched is self.owner:
                if kind in (QEvent.Type.Move, QEvent.Type.Resize):
                    self.sync_geometry()
                elif kind in (QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.WindowStateChange):
                    self.sync_visibility()
            elif watched is self._composer:
                if kind in (QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Hide):
                    self.sync_geometry()
            elif watched is self.panel or watched is self.title:
                if self._drag_event(event):
                    return True
            return super().eventFilter(watched, event)

        def mousePressEvent(self, event):
            if not self._drag_event(event):
                super().mousePressEvent(event)

        def mouseMoveEvent(self, event):
            if not self._drag_event(event):
                super().mouseMoveEvent(event)

        def mouseReleaseEvent(self, event):
            if not self._drag_event(event):
                super().mouseReleaseEvent(event)

        def closeEvent(self, event):
            event.ignore()
            self.request_hide()

    return BailongToolbar()
