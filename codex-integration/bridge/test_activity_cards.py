"""Offline UI tests using invented metadata, never a live Codex data source."""
import os
from pathlib import Path
import sys
import unittest

import activity_cards as cards


class MetadataTests(unittest.TestCase):
    def test_import_does_not_import_qt(self):
        # The factory is deliberately the only Qt entry point; this assertion
        # is valid before the UI test class below initializes the runtime.
        self.assertNotIn('PySide6', cards.__dict__)

    def test_only_whitelisted_metadata_survives_and_priority_is_native_like(self):
        source = [
            {'key': 'run', 'title': '执行项目', 'state': 'running', 'message': 'do not display'},
            {'key': 'done', 'title': '完成项目', 'state': 'completed'},
            {'key': 'fail', 'title': '失败项目', 'state': 'failed'},
            {'key': 'wait', 'title': '确认项目', 'state': 'needs_input', 'tool_input': 'never copy'},
        ]
        actual = cards.normalize_sessions(source)
        self.assertEqual([item['key'] for item in actual], ['wait', 'fail', 'done', 'run'])
        self.assertTrue(all(set(item) == {'key', 'title', 'project', 'state'} for item in actual))
        self.assertNotIn('message', repr(actual))

    def test_malformed_duplicate_and_control_text(self):
        actual = cards.normalize_sessions([
            None, {'key': ''}, {'key': 'bad\nkey', 'title': 'ignore'},
            {'key': 'a', 'title': 'old', 'state': 'running'},
            {'key': 'a', 'title': '<b>title</b>\n\u202e safe', 'project': '  示例\t项目 ', 'state': 'invalid'},
        ])
        self.assertEqual(actual, [{'key': 'a', 'title': '<b>title</b> safe', 'project': '示例 项目', 'state': 'unknown'}])
        self.assertEqual(cards.normalize_sessions({'key': 'not a sequence'}), [])


class GeometryTests(unittest.TestCase):
    def assert_clear(self, owner, size, available, obstacles=()):
        x, y = cards.choose_stack_position(owner, size, available, obstacles)
        rect = (x, y, *size)
        self.assertGreaterEqual(x, available[0])
        self.assertGreaterEqual(y, available[1])
        self.assertLessEqual(x + size[0], available[0] + available[2])
        self.assertLessEqual(y + size[1], available[1] + available[3])
        self.assertEqual(cards._intersection_area(rect, owner), 0)
        for obstacle in obstacles:
            self.assertEqual(cards._intersection_area(rect, obstacle), 0)
        return x, y

    def test_preferred_above_and_negative_monitor_coordinates(self):
        _, y = self.assert_clear((-1200, 300, 256, 256), (320, 126), (-1920, 0, 1920, 1080))
        self.assertLess(y + 126, 300)

    def test_top_edge_uses_side_and_narrow_screen_avoids_toolbar(self):
        _, y = self.assert_clear((0, 0, 256, 256), (320, 126), (0, 0, 800, 700))
        self.assertGreaterEqual(y, 264)
        self.assert_clear((60, 0, 256, 256), (320, 126), (0, 0, 400, 700), [(48, 262, 280, 46)])


class QtCardsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from bridge_loader import load_original_module
        cls.original = load_original_module()
        from PySide6.QtWidgets import QApplication, QWidget
        from PySide6.QtGui import QFontDatabase
        cls.app = QApplication.instance() or QApplication(['activity-cards-test', '-platform', 'offscreen'])
        # The offscreen platform does not enumerate Windows system fonts.
        # Load the same installed family used by the real Windows widget.
        font_file = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'Fonts/msyh.ttc'
        if font_file.exists():
            QFontDatabase.addApplicationFont(str(font_file))
        cls.Widget = QWidget

    def setUp(self):
        self.owner = self.Widget()
        self.owner.setFixedSize(256, 256)
        self.owner.move(300, 300)
        self.owner.manual_hidden = False
        self.owner.fullscreen_hidden = False
        self.owner.show()
        self.opened = []
        self.stack = cards.create_activity_cards(self.original, self.owner, self.opened.append)
        self.app.processEvents()

    def tearDown(self):
        self.stack.set_active(False)
        self.stack.deleteLater()
        self.owner.hide()
        self.owner.deleteLater()
        self.app.processEvents()

    def fixtures(self):
        return [
            {'key': 'sample-running', 'title': '示例：构建项目', 'project': '示例项目 A', 'state': 'running'},
            {'key': 'sample-input', 'title': '示例：等待你的确认', 'project': '示例项目 B', 'state': 'needs_input'},
            {'key': 'sample-complete', 'title': '示例：检查已完成', 'project': '示例项目 C', 'state': 'completed'},
            {'key': 'sample-failed', 'title': '示例：任务需要检查', 'state': 'failed'},
        ]

    def test_stack_depth_cycle_activate_and_all_menu(self):
        self.stack.set_sessions(self.fixtures())
        self.app.processEvents()
        self.assertTrue(self.stack.isVisible())
        self.assertEqual(self.stack.visible_depth, 3)
        self.assertEqual(self.stack.current_key, 'sample-input')
        self.stack.open_button.click()
        self.assertEqual(self.opened, ['sample-input'])
        self.stack.next_button.click()
        self.assertEqual(self.stack.current_key, 'sample-failed')
        self.stack.open_list()
        self.app.processEvents()
        actions = self.stack._menu.actions()
        self.assertEqual(len(actions), 4)
        actions[-1].trigger()
        self.assertEqual(self.opened[-1], 'sample-running')
        self.stack._menu.hide()

    def test_empty_inactive_owner_hidden_and_plain_text(self):
        self.stack.set_sessions([])
        self.stack._reveal_animation.setCurrentTime(450)
        self.assertFalse(self.stack.isVisible())
        self.stack.set_sessions([{'key': 'sample', 'title': '<b>plain</b>', 'state': 'running'}])
        self.assertEqual(self.stack.title_label.text(), '<b>plain</b>')
        self.assertTrue(self.stack.isVisible())
        self.stack.set_active(False)
        self.stack._reveal_animation.setCurrentTime(450)
        self.assertFalse(self.stack.isVisible())
        self.assertTrue(self.owner.isVisible())
        self.stack.set_active(True)
        self.owner.hide()
        self.app.processEvents()
        self.stack._reveal_animation.setCurrentTime(450)
        self.assertFalse(self.stack.isVisible())
        self.owner.show()
        self.app.processEvents()
        self.assertTrue(self.stack.isVisible())
        self.stack.set_sessions([])
        self.stack._reveal_animation.setCurrentTime(450)
        self.assertFalse(self.stack.isVisible())

    def test_owner_moves_cards_without_moving_owner_and_preview(self):
        self.stack.set_sessions(self.fixtures())
        self.app.processEvents()
        self.owner.move(330, 350)
        self.app.processEvents()
        self.assertEqual((self.owner.x(), self.owner.y()), (330, 350))
        self.assertFalse(self.stack.geometry().intersects(self.owner.geometry()))
        self.stack.grab().save(str(Path(__file__).with_name('activity-preview.png')))


if __name__ == '__main__':
    unittest.main()
