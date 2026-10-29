"""Styled chat UI keeps original signals and supports the synced controller."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace


class ChatUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from bridge_loader import load_original_module
        original = load_original_module()
        import chat_ui
        from bailong_theme import configure_fonts
        from chat_ui_style import make_chat_widgets
        from conversation_sync import make_controller_class
        from PySide6.QtWidgets import QWidget
        cls.ui = chat_ui
        cls.app = original.QApplication.instance() or original.QApplication(['chat-ui-qa'])
        configure_fonts(cls.app)
        cls.Composer, cls.Window = make_chat_widgets(chat_ui.Composer, chat_ui.ChatWindow)
        cls.Controller = make_controller_class(chat_ui.ChatController, background=False)
        class Pet(QWidget):
            def __init__(self):
                super().__init__()
                self.speech = []
            def say(self, *args): self.speech.append(args)
        cls.Pet = Pet

    def test_compact_window_send_stop_shortcut_and_close_signals(self):
        composer = self.Composer()
        sent, stopped, closed = [], [], []
        composer.send.connect(sent.append)
        composer.stop.connect(lambda: stopped.append(True))
        composer.closed.connect(lambda: closed.append(True))
        composer.show()
        composer.editor.setPlainText('一条测试消息，不发送到模型')
        composer.send_button.click()
        self.assertEqual(sent, ['一条测试消息，不发送到模型'])
        composer.shortcut.activated.emit()
        self.assertEqual(len(sent), 2)
        composer.busy(True)
        composer.send_button.click()
        self.assertEqual(len(stopped), 1)
        self.assertFalse(composer.folder_button.isEnabled())
        self.assertFalse(composer.new_button.isEnabled())
        composer.close()
        self.assertFalse(composer.isVisible())
        self.assertEqual(len(closed), 1)
        composer.deleteLater()
        self.app.processEvents()

    def test_compact_history_folder_and_new_chat_actions(self):
        composer = self.Composer()
        actions = []
        composer.history.connect(lambda: actions.append('history'))
        composer.folder.connect(lambda: actions.append('folder'))
        composer.new_chat.connect(lambda: actions.append('new'))
        composer.history_button.click()
        composer.folder_button.click()
        composer.new_button.click()
        self.assertEqual(actions, ['history', 'folder', 'new'])
        composer.deleteLater()
        self.app.processEvents()

    def test_full_window_selection_send_busy_and_close_contract(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QListWidgetItem
        window = self.Window()
        selected, sent = [], []
        window.selected.connect(selected.append)
        window.send.connect(sent.append)
        item = QListWidgetItem('测试会话')
        item.setData(Qt.ItemDataRole.UserRole, 'test-local-id')
        window.list.addItem(item)
        window.list.setCurrentItem(item)
        self.assertEqual(selected, ['test-local-id'])
        window.editor.setPlainText('测试输入')
        window.send_button.click()
        self.assertEqual(sent, ['测试输入'])
        window.show()
        window.busy(True)
        self.assertTrue(window.stop_button.isVisible())
        self.assertFalse(window.send_button.isEnabled())
        window.busy(False)
        self.assertFalse(window.stop_button.isVisible())
        window.set_chat_header({'title': 'Codex同步 · 标题测试'})
        self.assertEqual(window.thread_title.text(), '标题测试')
        window.close()
        self.assertFalse(window.isVisible())
        window.deleteLater()
        self.app.processEvents()

    def test_titlebar_maximize_and_restore(self):
        window = self.Window()
        window.show()
        window.title_bar.maximize()
        self.assertTrue(window.isMaximized())
        window.title_bar.maximize()
        self.assertFalse(window.isMaximized())
        window.close()
        window.deleteLater()
        self.app.processEvents()

    def test_synced_history_and_file_panels_inside_new_window(self):
        from test_conversation_sync import DatabaseFixture, cached_chat
        from conversation_sync import ConversationReader, tool_token
        from PySide6.QtCore import QUrl
        import json
        with tempfile.TemporaryDirectory(prefix='BailongStyledChat-') as folder:
            base = Path(folder)
            fixture = DatabaseFixture(base / 'codex')
            fixture.thread()
            fixture.turn()
            fixture.user('u1', '请检查并修复文件。', 1)
            fixture.command(output='检查完成：发现一处配置需要调整。', ordinal=2)
            fixture.change(diff='@@ -1 +1 @@\n-size = 80\n+size = 100', ordinal=3)
            fixture.agent('a1', '已完成调整，文件差异可在记录中展开查看。', 4)
            data = base / 'data'
            data.mkdir()
            (data / 'conversations.json').write_text(json.dumps([cached_chat()], ensure_ascii=False), encoding='utf-8')
            pet = self.Pet()
            with patch.object(self.ui, 'Composer', self.Composer), patch.object(self.ui, 'ChatWindow', self.Window):
                controller = self.Controller(pet, directory=data)
            try:
                controller.conversation_sync.reader = ConversationReader(fixture.root)
                controller.conversation_sync.apply(controller.conversation_sync.reader.read(['thread-1']))
                controller.open_history()
                self.app.processEvents()
                self.assertEqual(controller.window.thread_title.text(), '测试会话')
                self.assertIn('请检查并修复文件。', controller.window.view.toPlainText())
                self.assertEqual(controller.window.view.horizontalScrollBar().maximum(), 0)
                controller.window.view.anchorClicked.emit(QUrl('bailong-tool:' + tool_token('thread-1', 'file-1')))
                self.app.processEvents()
                panel = next(iter(controller.file_views.panels.values()))
                self.assertEqual([label.text() for label in panel.labels], ['原文件', '新文件'])
                self.assertIn('size = 100', panel.after.toPlainText())
                output = Path(__file__).resolve().parents[2] / 'analysis/ui-preview'
                output.mkdir(parents=True, exist_ok=True)
                self.assertTrue(controller.window.grab().save(str(output / 'chat-window-redesigned.png')))
                controller.composer.show()
                self.app.processEvents()
                self.assertTrue(controller.composer.grab().save(str(output / 'chat-composer-redesigned.png')))
            finally:
                controller.shutdown()
                controller.window.deleteLater()
                controller.composer.deleteLater()
                pet.deleteLater()
                self.app.processEvents()
