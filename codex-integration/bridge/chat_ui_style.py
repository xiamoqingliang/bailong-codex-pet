"""White/lilac companion chat widgets with the original controller contract."""
from bailong_theme import SCROLLBAR_STYLE

CHAT_STYLE = '''
QWidget { font-family:"Microsoft YaHei UI","Segoe UI"; font-size:13px; color:#574369; }
QFrame#ChatShell { background:#fbf9ff; border:1px solid #c7b6e5; border-radius:18px; }
QFrame#ChatSidebar, QFrame#ChatConversation { background:white; border:1px solid #e4d9ef; border-radius:14px; }
QFrame#ChatInput { background:#fbf9ff; border:1px solid #d9cbe5; border-radius:12px; }
QLabel { background:transparent; border:none; }
QLabel#ChatTitle { color:#6b4d91; font-size:17px; font-weight:600; }
QLabel#ChatSectionTitle { color:#6b4d91; font-size:14px; font-weight:600; }
QLabel#ChatHint, QLabel#ChatPath { color:#89739f; font-size:11px; }
QLabel#ChatStatus { color:#8061a3; background:#f0eaf8; border-radius:8px; padding:4px 8px; font-size:11px; }
QPushButton { background:#f0eaf8; color:#574369; border:1px solid transparent; border-radius:9px; padding:6px 10px; }
QPushButton:hover { background:#e5d8f4; }
QPushButton:pressed { background:#d9c7ed; }
QPushButton:focus { border:1px solid #9473b7; }
QPushButton:disabled { color:#ab9ab6; background:#f6f1fa; }
QPushButton#ChatPrimary { background:#9473b7; color:white; }
QPushButton#ChatPrimary:hover { background:#8061a3; }
QPushButton#ChatStop { background:#f7e8ed; color:#ac5475; }
QPushButton#ChatWindowControl, QPushButton#ChatClose { background:transparent; padding:0; font-size:16px; }
QPushButton#ChatWindowControl:hover, QPushButton#ChatClose:hover { background:#e9dff4; }
QPushButton#ChatClose:hover { background:#f7e8ed; color:#ac5475; }
QListWidget { background:white; border:none; padding:0; outline:0; }
QListWidget::item { color:#574369; padding:10px 8px; margin:2px 0; border-radius:8px; }
QListWidget::item:hover { background:#f5effb; }
QListWidget::item:selected { color:#6b4d91; background:#eadff4; }
QPlainTextEdit { background:transparent; border:none; padding:2px; selection-background-color:#d9c7ed; }
QTextBrowser { background:white; border:none; padding:2px; selection-background-color:#d9c7ed; }
QSplitter::handle { background:transparent; }
QSplitter::handle:hover { background:#e5d8f4; }
QSizeGrip { width:12px; height:12px; background:transparent; }
''' + SCROLLBAR_STYLE


def make_chat_widgets(original_composer, original_window):
    from ui_motion import MotionWindowMixin
    from PySide6.QtCore import Qt, QPoint
    from PySide6.QtGui import QFont, QShortcut, QKeySequence
    from PySide6.QtWidgets import (QWidget, QFrame, QVBoxLayout, QHBoxLayout, QLabel,
                                 QPushButton, QPlainTextEdit, QTextBrowser, QListWidget,
                                 QSplitter, QSizeGrip)

    class TitleBar(QWidget):
        def __init__(self, owner, title, compact=False):
            super().__init__(owner)
            self.owner, self.compact = owner, compact
            self.press = self.origin = None
            row = QHBoxLayout(self)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            label = QLabel(title)
            label.setObjectName('ChatTitle')
            row.addWidget(label)
            row.addStretch()
            if not compact:
                for caption, tooltip, callback in (
                    ('−', '最小化', owner.showMinimized),
                    ('□', '最大化或还原', self.maximize),
                ):
                    button = QPushButton(caption)
                    button.setObjectName('ChatWindowControl')
                    button.setFixedSize(30, 28)
                    button.setToolTip(tooltip)
                    button.clicked.connect(callback)
                    row.addWidget(button)
            close = QPushButton('×')
            close.setObjectName('ChatClose')
            close.setFixedSize(28, 28)
            close.setAccessibleName('关闭聊天窗口')
            close.clicked.connect(owner.close)
            row.addWidget(close)

        def maximize(self):
            self.owner.showNormal() if self.owner.isMaximized() else self.owner.showMaximized()

        def mousePressEvent(self, event):
            if event.button() == Qt.MouseButton.LeftButton:
                self.press, self.origin = event.globalPosition().toPoint(), self.owner.pos()
                handle = self.owner.windowHandle()
                if handle is not None and handle.startSystemMove():
                    self.press = None
                event.accept()

        def mouseMoveEvent(self, event):
            if self.press is not None and event.buttons() & Qt.MouseButton.LeftButton:
                self.owner.move(self.origin + event.globalPosition().toPoint() - self.press)

        def mouseReleaseEvent(self, event):
            self.press = self.origin = None

        def mouseDoubleClickEvent(self, event):
            if not self.compact and event.button() == Qt.MouseButton.LeftButton:
                self.maximize()

    def shell(owner, compact=False):
        owner.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        owner.setFont(QFont('Microsoft YaHei UI', 10))
        owner.setStyleSheet(CHAT_STYLE)
        outer = QVBoxLayout(owner)
        outer.setContentsMargins(0, 0, 0, 0)
        frame = QFrame(owner)
        frame.setObjectName('ChatShell')
        outer.addWidget(frame)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12 if compact else 18, 12, 12 if compact else 18, 14)
        layout.setSpacing(10 if compact else 12)
        owner.title_bar = TitleBar(owner, '白龙聊天', compact)
        layout.addWidget(owner.title_bar)
        owner.shell = frame
        return layout

    def button(caption, callback, object_name=None):
        value = QPushButton(caption)
        if object_name:
            value.setObjectName(object_name)
        value.setCursor(Qt.CursorShape.PointingHandCursor)
        value.clicked.connect(callback)
        return value

    class Composer(MotionWindowMixin, original_composer):
        _bailong_chat_ui = True

        def __init__(self):
            QWidget.__init__(self, None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                             Qt.WindowType.WindowStaysOnTopHint)
            self.setWindowTitle('白龙娘 · 对话模式')
            self.setFixedSize(360, 232)
            layout = shell(self, compact=True)
            self.status = QLabel('把想说的话告诉白龙吧。')
            self.status.setObjectName('ChatHint')
            self.status.setWordWrap(True)
            self.status.setMaximumHeight(34)
            layout.addWidget(self.status)
            input_frame = QFrame()
            input_frame.setObjectName('ChatInput')
            input_layout = QVBoxLayout(input_frame)
            input_layout.setContentsMargins(9, 6, 9, 6)
            self.editor = QPlainTextEdit()
            self.editor.setPlaceholderText('继续这段对话…\nCtrl+Enter 发送')
            input_layout.addWidget(self.editor)
            layout.addWidget(input_frame, 1)
            row = QHBoxLayout()
            row.setSpacing(6)
            self.folder_button = button('文件夹', self.folder)
            self.history_button = button('历史', self.history)
            self.new_button = button('新对话', self.new_chat)
            self.send_button = button('发送', lambda: self.stop.emit() if self.send_button.text() == '停止' else self.submit(), 'ChatPrimary')
            self.send_button.setFixedWidth(60)
            self.stop_button = button('停止', self.stop, 'ChatStop')
            self.stop_button.hide()
            for value in (self.folder_button, self.history_button, self.new_button):
                row.addWidget(value)
            row.addStretch()
            row.addWidget(self.send_button)
            layout.addLayout(row)
            self.shortcut = QShortcut(QKeySequence('Ctrl+Return'), self)
            self.shortcut.activated.connect(self.submit)

        def busy(self, value):
            original_composer.busy(self, value)
            self.status.setText('正在处理消息，可随时停止。' if value else '把想说的话告诉白龙吧。')

    class ChatWindow(MotionWindowMixin, original_window):
        _bailong_chat_ui = True

        def __init__(self):
            QWidget.__init__(self, None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
            self.setWindowTitle('白龙 · 聊天记录')
            self.resize(1080, 780)
            self.setMinimumSize(720, 540)
            layout = shell(self)
            split = QSplitter(Qt.Orientation.Horizontal)
            split.setChildrenCollapsible(False)
            split.setHandleWidth(8)
            sidebar = QFrame()
            sidebar.setObjectName('ChatSidebar')
            sidebar.setMinimumWidth(190)
            sidebar.setMaximumWidth(290)
            side = QVBoxLayout(sidebar)
            side.setContentsMargins(12, 12, 12, 12)
            head = QHBoxLayout()
            label = QLabel('会话')
            label.setObjectName('ChatSectionTitle')
            self.new_button = button('＋ 新建', self.new_chat)
            head.addWidget(label)
            head.addStretch()
            head.addWidget(self.new_button)
            side.addLayout(head)
            self.list = QListWidget()
            self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.list.currentItemChanged.connect(lambda item, old: self.selected.emit(item.data(Qt.ItemDataRole.UserRole)) if item else None)
            side.addWidget(self.list, 1)
            hint = QLabel('与 Codex 中的会话记录同步')
            hint.setObjectName('ChatHint')
            hint.setWordWrap(True)
            side.addWidget(hint)
            conversation = QFrame()
            conversation.setObjectName('ChatConversation')
            content = QVBoxLayout(conversation)
            content.setContentsMargins(14, 12, 14, 12)
            content.setSpacing(10)
            self.thread_title = QLabel('新的对话')
            self.thread_title.setObjectName('ChatSectionTitle')
            self.thread_title.setWordWrap(True)
            self.thread_title.setMaximumHeight(44)
            self.status = QLabel('会话同步')
            self.status.setObjectName('ChatStatus')
            self.status.setWordWrap(True)
            self.status.setMaximumWidth(340)
            header = QHBoxLayout()
            header.addWidget(self.thread_title, 1)
            header.addWidget(self.status)
            content.addLayout(header)
            self.view = QTextBrowser()
            self.view.setOpenExternalLinks(False)
            self.view.setOpenLinks(False)
            content.addWidget(self.view, 1)
            input_frame = QFrame()
            input_frame.setObjectName('ChatInput')
            input_layout = QVBoxLayout(input_frame)
            input_layout.setContentsMargins(10, 8, 10, 8)
            input_layout.setSpacing(8)
            self.editor = QPlainTextEdit()
            self.editor.setPlaceholderText('继续这段对话… Ctrl+Enter 发送')
            self.editor.setMinimumHeight(52)
            self.editor.setMaximumHeight(90)
            input_layout.addWidget(self.editor)
            footer = QHBoxLayout()
            self.folder_button = button('文件夹', self.folder)
            self.stop_button = button('停止任务', self.stop, 'ChatStop')
            self.stop_button.hide()
            self.send_button = button('发送', self.submit, 'ChatPrimary')
            self.send_button.setFixedWidth(76)
            footer.addWidget(self.folder_button)
            hint = QLabel('Enter 换行')
            hint.setObjectName('ChatHint')
            footer.addWidget(hint)
            footer.addStretch()
            footer.addWidget(self.stop_button)
            footer.addWidget(self.send_button)
            input_layout.addLayout(footer)
            content.addWidget(input_frame)
            split.addWidget(sidebar)
            split.addWidget(conversation)
            split.setSizes([235, 800])
            layout.addWidget(split, 1)
            path = QHBoxLayout()
            self.path_label = QLabel('任务文件夹：尚未选择')
            self.path_label.setObjectName('ChatPath')
            self.path_label.setWordWrap(True)
            self.path_label.setMaximumHeight(32)
            path.addWidget(self.path_label, 1)
            self.resize_grip = QSizeGrip(self)
            path.addWidget(self.resize_grip, 0, Qt.AlignmentFlag.AlignBottom)
            layout.addLayout(path)
            self.shortcut = QShortcut(QKeySequence('Ctrl+Return'), self)
            self.shortcut.activated.connect(self.submit)

        def set_chat_header(self, chat):
            title = (chat.get('title') or '新的对话') if chat else '新的对话'
            title = title.removeprefix('Codex同步 · ')
            self.thread_title.setText(title)
            self.thread_title.setToolTip(title)

        def busy(self, value):
            original_window.busy(self, value)
            self.status.setText('正在处理' if value else '会话同步')

    return Composer, ChatWindow


def install_chat_ui(ui):
    if not getattr(ui.Composer, '_bailong_chat_ui', False):
        ui.Composer, ui.ChatWindow = make_chat_widgets(ui.Composer, ui.ChatWindow)
