"""Real v051 engine and WebM decoding, isolated preferences and no model calls."""
import json
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dock_controls import HoverLease


class DockPolicyTests(unittest.TestCase):
    def test_only_pet_hover_reveals_then_controls_keep_the_lease(self):
        lease = HoverLease()
        self.assertFalse(lease.update('left', False, True, False, now=1))
        self.assertTrue(lease.update('left', True, False, False, now=2))
        self.assertTrue(lease.update('left', False, True, False, now=3))
        self.assertTrue(lease.update('left', False, False, False, now=3.2))
        self.assertFalse(lease.update('left', False, False, False, now=3.4))
        self.assertFalse(lease.update('right', False, False, False, now=4))

    def test_hidden_state_wins_over_hover_and_popups(self):
        lease = HoverLease()
        self.assertFalse(lease.update('right', True, True, True, enabled=False))


class V051EngineTests(unittest.TestCase):
    def assert_speech_attached_vertically(self, below=False):
        pet = self.pet
        speech, body = pet.bubble.frameGeometry(), pet.frameGeometry()
        self.assertTrue(pet.bubble.isVisible())
        if below:
            self.assertEqual(speech.top(), body.bottom() + 1 + 8)
        else:
            self.assertEqual(speech.bottom() + 1 + 8, body.top())
        self.assertTrue(pet.screen().availableGeometry().contains(speech))
        for widget in (pet.toolbar, pet.activity_cards):
            if widget.isVisible():
                self.assertFalse(speech.intersects(widget.frameGeometry()))

    def assert_speech_beside_pet(self):
        pet = self.pet
        from companion_layout import companion_owner
        from PySide6.QtCore import QRect
        notification = pet.notification_bubble
        self.assertTrue(notification.isVisible())
        speech, body = notification.frameGeometry(), QRect(*companion_owner(pet))
        self.assertTrue(speech.right() < body.left() or speech.left() > body.right())
        self.assertTrue(pet.screen().availableGeometry().contains(speech))
        for widget in (pet.toolbar, pet.activity_cards):
            if widget.isVisible():
                self.assertFalse(speech.intersects(widget.frameGeometry()))

    def capture_speech_scene(self, name):
        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QImage, QPainter, QColor
        pet = self.pet
        area = pet.screen().availableGeometry()
        for widget in (pet.toolbar, pet.activity_cards):
            if hasattr(widget, '_reveal_animation'):
                widget._reveal_animation.setCurrentTime(widget._reveal_animation.duration())
        canvas = QImage(area.width(), area.height(), QImage.Format.Format_ARGB32_Premultiplied)
        canvas.fill(QColor('#101927'))
        painter = QPainter(canvas)
        for widget in (pet.bubble, getattr(pet, 'notification_bubble', None), pet.quota_panel,
                       getattr(getattr(pet, 'chat', None), 'composer', None), pet.settings_panel,
                       pet.toolbar, pet.activity_cards, pet):
            if widget is not None and widget.isVisible():
                painter.setOpacity(widget.windowOpacity())
                painter.drawPixmap(QPoint(widget.x() - area.x(), widget.y() - area.y()), widget.grab())
        painter.end()
        folder = Path(__file__).resolve().parents[2] / 'analysis/ui-preview'
        folder.mkdir(parents=True, exist_ok=True)
        self.assertTrue(canvas.save(str(folder / name)))

    @classmethod
    def setUpClass(cls):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from bridge_loader import load_original_module
        from v051_behavior import V051Behavior, configure_preferences
        from bailong_toolbar import create_toolbar
        from activity_cards import create_activity_cards
        from dock_controls import create_dock_controls
        from companion_layout import observe_speech_layout
        original = load_original_module()
        from asset_source import configure_asset_source
        configure_asset_source(original)
        cls.original = original
        cls.temporary = tempfile.TemporaryDirectory(prefix='Bailong-v051-QA-')
        configure_preferences(original, Path(cls.temporary.name) / 'settings.ini')
        cls.app = original.QApplication.instance() or original.QApplication(['v051-qa'])
        from bailong_theme import configure_fonts
        configure_fonts(cls.app)
        cls.app.setQuitOnLastWindowClosed(False)
        class Pet(V051Behavior, original.Pet):
            toolbar = None
            exit_reason = None
            _ui_motion_disabled = True
        started = time.monotonic()
        cls.pet = Pet()
        cls.preload_seconds = time.monotonic() - started
        cls.pet.tick.stop()
        cls.pet.visibility_timer.stop()
        cls.pet.desktop_sync.stop()
        cls.pet.sound_disabled = True
        cls.pet.toolbar = create_toolbar(original, cls.pet)
        cls.pet.activity_cards = create_activity_cards(original, cls.pet)
        cls.pet.dock_controls = create_dock_controls(cls.pet)
        cls.pet.dock_controls.timer.stop()
        observe_speech_layout(cls.pet)
        cls.pet.show()
        cls.app.processEvents()

    @classmethod
    def tearDownClass(cls):
        pet = cls.pet
        report = {'upstreamVersion': '0.5.1', 'loadedAnimations': len(pet.pinned_frames),
                  'loadedFrames': sum(len(frames) for frames in pet.pinned_frames.values()),
                  'preloadSeconds': round(cls.preload_seconds, 3), 'modelRequests': 0,
                  'preferencesIsolated': True}
        folder = Path(__file__).resolve().parents[2] / 'analysis'
        folder.mkdir(exist_ok=True)
        (folder / 'engine-verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        pet.close()
        cls.app.processEvents()
        cls.temporary.cleanup()

    def setUp(self):
        pet = self.pet
        pet.set_fixed_pet(False)
        if getattr(pet, '_window_policy', None) is not None:
            pet._window_policy.drop_pending()
            pet._window_policy.clock = time.monotonic
        pet.close_notification()
        pet.settings_panel.hide() if pet.settings_panel else None
        pet.quota_panel.hide() if pet.quota_panel else None
        if hasattr(pet, "_notice_timer"):
            pet._notice_timer.stop()
            pet._notices.clear()
        for name in ('_quota_hide_timer', '_quota_connect_timer'):
            if hasattr(pet, name):
                getattr(pet, name).stop()
        pet._quota_speech_active = False
        pet._quota_ui_dismissed = False
        pet._local_hide_animation = False
        pet.set_size_percent(100)
        pet.dock_edge = None
        pet.manual_hidden = pet.fullscreen_hidden = False
        pet.task_bubbles_disabled = False
        pet.quota_inflight = False
        pet.bubble.hide()
        pet.bubble_timer.stop()
        pet.set_bubble_percent(100)
        pet.desktop_sync.running.clear()
        pet.chat = None
        pet.task_active = False
        pet.show()

    @contextmanager
    def styled_chat(self):
        import chat_ui
        from chat_ui_style import make_chat_widgets
        from conversation_sync import make_controller_class
        pet = self.pet
        Composer, Window = make_chat_widgets(chat_ui.Composer, chat_ui.ChatWindow)
        with tempfile.TemporaryDirectory(prefix='BailongWindowPolicy-') as folder:
            Controller = make_controller_class(chat_ui.ChatController, home=Path(folder), background=False)
            with patch.object(chat_ui, 'Composer', Composer), patch.object(chat_ui, 'ChatWindow', Window):
                controller = Controller(pet, directory=Path(folder))
            pet.chat = controller
            try:
                yield controller
            finally:
                controller.shutdown()
                pet.chat = None
                controller.composer.removeEventFilter(pet.toolbar)
                pet.toolbar._composer = None
                controller.window.deleteLater()
                controller.composer.deleteLater()
                self.app.processEvents()

    def use_wide_settings_screen(self):
        from PySide6.QtWidgets import QApplication
        # 200% pet plus a full-size side panel cannot fit the offscreen
        # platform's default 800px work area. Model a normal desktop here.
        area = self.pet.screen().availableGeometry()
        area.setWidth(1500)
        screen = SimpleNamespace(availableGeometry=lambda: area, name=lambda: 'settings-qa',
                                 devicePixelRatio=lambda: 1)
        screen_patch = patch.object(self.pet, 'screen', return_value=screen)
        screen_patch.start()
        self.addCleanup(screen_patch.stop)
        screen_at_patch = patch.object(QApplication, 'screenAt', side_effect=lambda point: screen if area.contains(point) else None)
        screen_at_patch.start()
        self.addCleanup(screen_at_patch.stop)
        return area

    @contextmanager
    def quota_query(self):
        from PySide6.QtCore import QObject, Signal
        from threading import Event
        class Worker(QObject):
            ready = Signal(object)
            failed = Signal(str)
            def __init__(self, parent):
                super().__init__(parent)
                self.cancel = Event()
            def start(self): pass
            def isRunning(self): return False
        with patch.object(self.original, 'QuotaWorker', Worker):
            self.pet.quota()
            worker = self.pet.worker
            try:
                yield worker
            finally:
                self.pet.dismiss_quota()
                self.pet.worker = None

    @contextmanager
    def live_motion(self):
        pet = self.pet
        pet._ui_motion_disabled = False
        try:
            yield
        finally:
            pet._ui_motion_disabled = True
            pet.window_policy().drop_pending()
            for motion in list(pet._ui_motions):
                if motion.role in ('toolbar', 'cards'):
                    motion.finish()
                else:
                    motion.cancel()
            self.app.processEvents()

    def test_live_half_speed_reflow_uses_stable_targets_and_immediate_owner_follow(self):
        from companion_layout import rectangle
        pet = self.pet
        pet.move(300, 300)
        pet.mode = 'idle'
        pet.activity_cards.set_sessions([{'key': 'motion', 'title': '动效测试', 'state': 'running'}])
        pet.activity_cards.set_active(True)
        with self.live_motion():
            pet.say('半速展开，保持固定尺寸。', 4000)
            self.app.processEvents()
            motion = pet.bubble._ui_motion
            self.assertEqual(motion.animation.duration(), 640)
            animation = motion.animation
            motion.animation.setCurrentTime(300)
            expected = rectangle(pet.bubble)
            for _ in range(3):
                pet.toolbar.sync_geometry()
                self.app.processEvents()
            self.assertIs(motion.animation, animation)
            self.assertEqual(rectangle(pet.bubble), expected)
            card_motion = pet.activity_cards._ui_motion
            if card_motion.active():
                card_motion.animation.setCurrentTime(min(300, card_motion.animation.duration()))
            self.capture_speech_scene('motion-live-entry-middle.png')
            pet.move(pet.x(), pet.y()+40)
            pet.toolbar.sync_geometry()
            self.assertFalse(motion.active())
            self.assertEqual(pet.bubble.y()+pet.bubble.height()+8, pet.y())

    def test_live_settings_switch_waits_for_retraction_without_resetting_quota_timer(self):
        pet = self.pet
        pet.move(300, 300)
        with self.live_motion():
            pet.show_settings()
            pet.settings_panel._ui_motion.finish()
            with self.quota_query() as worker:
                settings_motion = pet.settings_panel._ui_motion
                quota_motion = pet.quota_panel._ui_motion
                self.assertTrue(settings_motion.closing)
                self.assertEqual(quota_motion.animation.duration(), 1080)
                self.assertEqual(pet.quota_panel.windowOpacity(), 0)
                settings_motion.finish()
                quota_motion.finish()
                worker.ready.emit([{'name': '5 小时', 'remaining': 61, 'resetsAt': None}])
                before = pet._quota_hide_timer.remainingTime()
                deadline = quota_motion.expiry_at
                for _ in range(4):
                    pet.quota_panel.reposition()
                    self.app.processEvents()
                self.assertEqual(quota_motion.expiry_at, deadline)
                self.assertLessEqual(pet._quota_hide_timer.remainingTime(), before)
                self.assertEqual(quota_motion.expiry_timer.interval(), 11560)

    def test_live_progress_update_reopens_a_retracting_notice(self):
        pet = self.pet
        pet.move(300, 300)
        with self.live_motion():
            pet.task_progress('进展', '第一条进展。', lambda: None)
            bubble = pet.notification_bubble
            motion = bubble._ui_motion
            motion.finish()
            pet.close_notification()
            motion.animation.setCurrentTime(200)
            self.assertTrue(motion.closing)
            pet.task_progress('进展', '最新进展应继续显示。', lambda: None)
            self.assertFalse(motion.closing)
            motion.finish()
            self.assertTrue(bubble.isVisible())
            self.assertEqual(bubble.result_view.toPlainText(), '最新进展应继续显示。')

    def test_live_quota_state_resize_content_and_bar_transitions_keep_original_expiry(self):
        pet = self.pet
        pet.move(300, 300)
        with self.live_motion(), self.quota_query() as worker:
            panel = pet.quota_panel
            motion = panel._ui_motion
            motion.finish()
            old_height = panel.height()
            worker.ready.emit([{'name': '5 小时', 'remaining': 61, 'resetsAt': 1800000000},
                               {'name': '一周', 'remaining': 94, 'resetsAt': 1800300000}])
            self.app.processEvents()
            self.assertEqual(motion.size_animation.duration(), 400)
            self.assertGreater(motion.target_size.height(), old_height)
            content = panel.scroll.widget()
            self.assertEqual(content._ui_content_animation.duration(), 400)
            bar = panel.bars[0]
            self.assertEqual(bar.value(), 61)
            self.assertEqual(bar._fill_animation.duration(), 400)
            deadline = motion.expiry_at
            for _ in range(4):
                panel.reposition()
                self.app.processEvents()
            self.assertEqual(motion.expiry_at, deadline)
            motion.finish()
            bar._fill_animation.setCurrentTime(400)
            content._ui_content_animation.setCurrentTime(400)
            self.assertEqual(bar._display_value, 61)
            self.assertEqual(panel.height(), motion.target_size.height())
            panel.display([], error='连接失败提示')
            self.assertEqual(motion.size_animation.duration(), 400)
            motion.finish()
            self.assertLess(panel.height(), 200)

    def test_live_message_replacement_fades_text_without_resizing_or_reopening(self):
        pet = self.pet
        pet.move(300, 300)
        pet.mode = 'idle'
        with self.live_motion():
            pet.say('第一句台词。', 4000)
            pet.bubble._ui_motion.finish()
            position = pet.bubble.pos()
            pet.say('第二句台词保持固定大小。', 4000)
            view = pet.bubble._message_view
            self.assertEqual(view._ui_content_animation.duration(), 400)
            self.assertEqual(pet.bubble.pos(), position)
            self.assertEqual((pet.bubble.width(), pet.bubble.height()), (256, 52))
            self.assertFalse(pet.bubble._ui_motion.active())

    def test_settings_keeps_its_side_when_dragged_across_center_and_switches_only_at_edge(self):
        from companion_layout import rectangle
        from PySide6.QtCore import QRect
        pet = self.pet
        pet.set_size_percent(50)
        pet.bubble.hide()
        pet.activity_cards.set_sessions([])
        area = self.use_wide_settings_screen()
        pet.move(area.left()+100, area.top()+300)
        with self.live_motion():
            pet.show_settings()
            panel = pet.settings_panel
            self.assertEqual(panel._placement_side, 'right')
            # The wider toolbar also needs clearance on this side.
            for x in (240, 500, 650, 750, 800):
                pet.move(area.left()+x, pet.y())
                self.app.processEvents()
                self.assertEqual(panel._placement_side, 'right')
                self.assertGreaterEqual(rectangle(panel)[0], pet.x()+pet.width()+12)
            pet.move(area.right()+1-pet.width(), pet.y())
            self.app.processEvents()
            self.assertEqual(panel._placement_side, 'left')
            swap = panel._ui_motion.animation
            self.assertEqual(panel._ui_motion.kind, 'swap')
            self.assertEqual(swap.duration(), 800)
            swap.setCurrentTime(200)
            for x in (1100, 950, 850, 750, 700, 600):
                pet.move(area.left()+x, pet.y())
                self.app.processEvents()
                self.assertEqual(panel._placement_side, 'left')
                rect = rectangle(panel)
                self.assertLessEqual(rect[0]+rect[2]+12, pet.x())
                self.assertTrue(area.contains(QRect(*rect)))
                self.assertIs(panel._ui_motion.animation, swap)
                self.assertEqual(swap.currentTime(), 200)
            panel._ui_motion.finish()

    def test_live_toolbar_and_task_card_flip_sides_during_pet_drag_with_motion(self):
        from companion_layout import rectangle
        pet = self.pet
        area = pet.screen().availableGeometry()
        pet.activity_cards.set_sessions([{'key': 'swap-drag', 'title': '换位动效', 'state': 'completed'}])
        pet.activity_cards.set_active(True)
        pet.toolbar.set_active(True)
        with self.live_motion():
            for name, destination_y in (('activity_cards', area.top() + 10),
                                        ('toolbar', area.bottom() + 1 - pet.height())):
                pet.dragging = False
                pet.move(area.left() + 100, area.top() + 300)
                pet.toolbar.sync_geometry()
                for window in (pet.toolbar, pet.activity_cards):
                    window._ui_motion.finish()
                try:
                    pet.dragging = True
                    pet.move(pet.x(), destination_y)
                    pet.toolbar.sync_geometry()
                    motion = getattr(pet, name)._ui_motion
                    self.assertEqual(motion.kind, 'swap', name)
                    animation = motion.animation
                    self.assertEqual(animation.duration(), 800)
                    animation.setCurrentTime(200)
                    pet.move(pet.x() + 20, pet.y())
                    pet.toolbar.sync_geometry()
                    self.assertIs(motion.animation, animation)
                    self.assertEqual(animation.currentTime(), 200)
                    animation.setCurrentTime(320)
                    self.assertEqual(getattr(pet, name).windowOpacity(), 0)
                    motion.finish()
                    window = getattr(pet, name)
                    self.assertEqual((window.x(), window.y()), rectangle(window)[:2])
                finally:
                    pet.dragging = False

    def test_settings_fixed_full_size_does_not_compress_near_screen_edges(self):
        from bailong_settings import SETTINGS_SIZE, position_settings
        from PySide6.QtCore import QRect
        from PySide6.QtWidgets import QApplication
        pet = self.pet
        area = pet.screen().availableGeometry()
        pet.move(area.left(), area.top() + 200)
        pet.show_settings()
        panel = pet.settings_panel
        for x in (area.left(), area.center().x(), area.right() + 1 - pet.width()):
            pet.move(x, pet.y())
            self.app.processEvents()
            self.assertTrue(panel.isVisible())
            self.assertEqual((panel.width(), panel.height()), SETTINGS_SIZE)
            self.assertEqual(panel.minimumSize(), panel.maximumSize())
            self.assertEqual(panel.scroll.verticalScrollBar().maximum(), 0)
        # Even a smaller work area must not silently compress the panel.
        small = SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 600, 300))
        pet.set_fixed_pet(True)
        with patch.object(pet, 'screen', return_value=small), patch.object(QApplication, 'screens', return_value=[small]):
            self.assertFalse(position_settings(pet, panel))
            self.assertEqual((panel.width(), panel.height()), SETTINGS_SIZE)

    def test_settings_stays_centered_beside_pet_with_task_card_and_speech(self):
        from companion_layout import companion_owner, rectangle, visible_obstacles, intersects
        pet = self.pet
        area = pet.screen().availableGeometry()
        pet.set_size_percent(50)
        pet.activity_cards.set_sessions([{'key': 'centered-settings', 'title': '任务', 'state': 'completed'}])
        pet.activity_cards.set_active(True)
        pet.toolbar.set_active(True)
        pet.mode = 'idle'
        pet.move(area.left() + 300, area.top() + 300)
        pet.say('设置打开时仍保留的独立消息框。', 4000)
        pet.show_settings()
        panel = pet.settings_panel
        for y in (area.top() + 300, area.top(), area.bottom() + 1 - pet.height()):
            pet.move(pet.x(), y)
            self.app.processEvents()
            self.assertTrue(panel.isVisible())
            owner = companion_owner(pet)
            x, py, width, height = rectangle(panel)
            expected = owner[1] + (owner[3] - height) // 2
            self.assertEqual(py, max(area.top(), min(expected, area.bottom() + 1 - height)))
            if panel._placement_side == 'left':
                self.assertLessEqual(x + width + 12, owner[0])
            else:
                self.assertGreaterEqual(x, owner[0] + owner[2] + 12)
            for obstacle in visible_obstacles(pet, ('toolbar', 'activity_cards')):
                self.assertFalse(intersects((x, py, width, height), obstacle, 12))
        self.capture_speech_scene('settings-centered-card-clearance.png')

    def test_quota_loading_and_results_keep_side_across_center_and_state_change(self):
        from companion_layout import rectangle
        from PySide6.QtCore import QRect
        pet = self.pet
        pet.set_size_percent(50)
        pet.bubble.hide()
        pet.activity_cards.set_sessions([])
        area = pet.screen().availableGeometry()
        pet.move(area.left()+100, area.top()+300)
        with self.live_motion(), self.quota_query() as worker:
            panel = pet.quota_panel
            self.assertEqual(panel._placement_side, 'right')
            for x in (240, 300, 350, 400):
                pet.move(area.left()+x, pet.y())
                self.app.processEvents()
                self.assertEqual(panel._placement_side, 'right')
                self.assertEqual(rectangle(panel)[0], pet.x()+pet.width()+8)
            worker.ready.emit([{'name': '5 小时', 'remaining': 61, 'resetsAt': None},
                               {'name': '一周', 'remaining': 94, 'resetsAt': None}])
            self.app.processEvents()
            self.assertEqual(panel._placement_side, 'right')
            deadline = panel._ui_motion.expiry_at
            pet.move(area.right()+1-pet.width(), pet.y())
            self.app.processEvents()
            self.assertEqual(panel._placement_side, 'left')
            for x in (480, 420, 380, 340, 300):
                pet.move(area.left()+x, pet.y())
                self.app.processEvents()
                self.assertEqual(panel._placement_side, 'left')
                rect = rectangle(panel)
                self.assertEqual(rect[0]+rect[2]+8, pet.x())
                self.assertTrue(area.contains(QRect(*rect)))
                self.assertEqual(panel._ui_motion.expiry_at, deadline)

    def test_settings_toggle_closes_popups_but_keeps_independent_speech_and_history(self):
        pet = self.pet
        pet.move(300, 300)
        pet.mode = 'idle'
        with self.styled_chat() as chat:
            pet.toggle_chat()
            chat.open_history()
            pet.say('独立消息框。', 4000)
            pet.task_progress('进展', '正在执行', lambda: None)
            self.app.processEvents()
            self.assertTrue(chat.composer.isVisible())
            self.assertTrue(pet.notification_bubble.isVisible())
            pet.show_settings()
            for _ in range(3):
                pet.sync_dialogue_visibility()
                self.app.processEvents()
            self.assertFalse(chat.composer.isVisible())
            self.assertFalse(chat.enabled)
            self.assertFalse(pet.notification_bubble.isVisible())
            self.assertTrue(pet.bubble.isVisible())
            self.assertTrue(chat.window.isVisible())
            panel, body = pet.settings_panel.frameGeometry(), pet.frameGeometry()
            self.assertTrue(panel.right() < body.left() or panel.left() > body.right())
            policy = pet.window_policy()
            policy.restack()
            self.assertGreater(policy.last_order.index('pet'), policy.last_order.index('settings'))
            self.assertGreater(policy.last_order.index('history'), policy.last_order.index('pet'))
            for role in ('toolbar', 'cards'):
                if role in policy.last_order:
                    self.assertGreater(policy.last_order.index('pet'), policy.last_order.index(role))
                    self.assertGreater(policy.last_order.index(role), policy.last_order.index('settings'))
            pet.settings_panel.sliders['bubble_percent'][0].setValue(118)
            pet.toolbar.buttons['settings'].click()
            self.assertFalse(pet.settings_panel.isVisible())
            self.assertEqual(pet.settings.value('bubblePercent', type=int), 118)

    def test_deferred_quota_keeps_remaining_deadline_and_discards_expired_results(self):
        pet = self.pet
        now = [100.0]
        policy = pet.window_policy()
        policy.clock = lambda: now[0]
        for elapsed, should_show in ((5, True), (13, False)):
            with self.subTest(elapsed=elapsed), self.quota_query() as worker:
                pet.show_settings()
                self.assertTrue(pet.settings_panel.isVisible())
                self.assertFalse(pet.quota_panel.isVisible())
                worker.ready.emit([{'name': '5 小时', 'remaining': 61, 'resetsAt': None}])
                self.assertFalse(pet.quota_panel.isVisible())
                self.assertEqual(policy.pending[0], 'quota')
                now[0] += elapsed
                pet.show_settings()
                self.app.processEvents()
                self.assertEqual(pet.quota_panel.isVisible(), should_show)
                if should_show:
                    self.assertLessEqual(pet._quota_hide_timer.remainingTime(), 7000)
                    self.assertGreater(pet._quota_hide_timer.remainingTime(), 6500)
                    pet.mode = 'idle'
                    pet.say('额度面板收起不影响这句台词。', 4000)
                    pet._quota_hide_timer.timeout.emit()
                    self.assertTrue(pet.bubble.isVisible())
                else:
                    self.assertIsNone(policy.pending)
                    self.assertFalse(pet._quota_hide_timer.isActive())

    def test_user_opening_quota_saves_settings_and_supersedes_pending_notice(self):
        pet = self.pet
        pet.show_settings()
        pet.settings_panel.sliders['bubble_percent'][0].setValue(118)
        pet.task_completed('暂缓的完成提示', lambda: None)
        self.assertEqual(pet.window_policy().pending[0], 'notice')
        with self.quota_query():
            self.app.processEvents()
            self.assertFalse(pet.settings_panel.isVisible())
            self.assertIsNone(pet.window_policy().pending)
            self.assertTrue(pet.quota_panel.isVisible())
            self.assertFalse(pet.notification_bubble.isVisible()) if getattr(pet, 'notification_bubble', None) else None
            self.assertEqual(pet.settings.value('bubblePercent', type=int), 118)

    def test_pending_quota_is_not_lost_when_a_task_notice_arrives_during_settings(self):
        pet = self.pet
        now = [100.0]
        policy = pet.window_policy()
        policy.clock = lambda: now[0]
        with self.quota_query() as worker:
            pet.show_settings()
            worker.ready.emit([{'name': '5 小时', 'remaining': 61, 'resetsAt': None}])
            now[0] = 101
            pet.task_completed('查询期间完成的任务。', lambda: None)
            self.assertEqual(policy.pending[0], 'quota')
            now[0] = 105
            pet.show_settings()
            self.app.processEvents()
            self.assertTrue(pet.quota_panel.isVisible())
            self.assertFalse(pet.notification_bubble.isVisible()) if getattr(pet, 'notification_bubble', None) else None
            self.assertEqual(policy.pending[0], 'notice')
            now[0] = 112
            pet.dismiss_quota()
            self.app.processEvents()
            self.assertTrue(pet.notification_bubble.isVisible())
            self.assertLessEqual(pet._notification_timer.remainingTime(), 9000)
            self.assertGreater(pet._notification_timer.remainingTime(), 8500)

    def test_deferred_notices_coalesce_and_user_chat_can_coexist_with_them(self):
        pet = self.pet
        now = [100.0]
        policy = pet.window_policy()
        policy.clock = lambda: now[0]
        pet.move(300, 300)
        with self.styled_chat() as chat:
            pet.show_settings()
            pet.task_progress('旧进展', '进行中', lambda: None)
            pet.task_completed('最终结果', lambda: None)
            self.assertFalse(getattr(pet, 'notification_bubble', None) and pet.notification_bubble.isVisible())
            now[0] += 3
            pet.toggle_chat()
            self.app.processEvents()
            self.assertFalse(pet.settings_panel.isVisible())
            self.assertTrue(chat.composer.isVisible())
            self.assertTrue(pet.notification_bubble.isVisible())
            self.assertEqual(pet.notification_bubble.result_view.toPlainText(), '最终结果')
            self.assertLessEqual(pet._notification_timer.remainingTime(), 17000)
            self.assertGreater(pet._notification_timer.remainingTime(), 16500)

    def test_centered_chat_coexists_with_quota_and_notices_without_blocking_dragging(self):
        pet = self.pet
        pet.move(300, 300)
        with self.styled_chat() as chat:
            pet.toggle_chat()
            self.app.processEvents()
            self.assertEqual(chat.composer.x() + chat.composer.width() // 2, pet.x() + pet.width() // 2)
            self.assertFalse(chat.composer.frameGeometry().intersects(pet.toolbar.frameGeometry()))
            pet.task_progress('进展', '进行中', lambda: None)
            self.app.processEvents()
            self.assertTrue(chat.composer.isVisible())
            self.assertTrue(pet.notification_bubble.isVisible())
            with self.quota_query() as worker:
                self.assertTrue(chat.composer.isVisible())
                self.assertFalse(pet.notification_bubble.isVisible())
                worker.ready.emit([{'name': '5 小时', 'remaining': 61, 'resetsAt': None}])
                pet.task_completed('完成提示等待额度收起', lambda: None)
                self.assertFalse(pet.notification_bubble.isVisible())
                self.assertTrue(pet.quota_panel.isVisible())
                pet.dismiss_quota()
                pet._flush_notices()
                self.app.processEvents()
                self.assertTrue(pet.notification_bubble.isVisible())
                self.assertFalse(pet.quota_panel.isVisible())
                self.assertTrue(chat.composer.isVisible())

    def test_docked_popups_attach_to_visible_head_instead_of_transparent_canvas(self):
        from companion_layout import companion_owner
        pet = self.pet
        area = pet.screen().availableGeometry()
        pet.toolbar.hide()
        pet.activity_cards.hide()
        pet.activity_cards.sessions = []
        for edge, x in (('left', area.left()), ('right', area.right() + 1 - pet.width())):
            pet.close_notification()
            pet.dock_edge = edge
            pet.move(x, area.top() + 250)
            before = pet.pos()
            pet.task_progress('吸边进展', '保持贴近白龙。', lambda: None)
            self.app.processEvents()
            ox, oy, ow, oh = companion_owner(pet)
            panel = pet.notification_bubble.frameGeometry()
            self.assertEqual(panel.left() - (ox + ow) if edge == 'left' else ox - panel.right() - 1, 8)
            self.assertEqual(pet.pos(), before)
            pet.show_settings()
            self.app.processEvents()
            settings = pet.settings_panel.frameGeometry()
            self.assertEqual(settings.left() - (ox + ow) if edge == 'left' else ox - settings.right() - 1, 12)
            self.assertEqual(pet.pos(), before)
            self.capture_speech_scene(f'docked-{edge}-settings.png')
            pet.show_settings()

    def test_all_35_webm_animations_decode_with_transparency(self):
        pet = self.pet
        self.assertEqual(len(pet.pinned_frames), 35)
        for key, frames in pet.pinned_frames.items():
            self.assertTrue(key.endswith('.webm'))
            self.assertGreater(len(frames), 1)
            image = frames[0].toImage()
            self.assertTrue(image.hasAlphaChannel())
            self.assertEqual(image.pixelColor(0, 0).alpha(), 0)

    def test_toolbar_title_order_and_minus_are_retained(self):
        toolbar = self.pet.toolbar
        self.assertEqual(toolbar.title.text(), '白龙')
        self.assertEqual([button.text() for button in toolbar.buttons.values()], ['聊天', '额度', '动作', '设置'])
        self.assertEqual(toolbar.hide_button.text(), '−')
        self.assertNotIn('history', toolbar.buttons)

    def test_first_second_and_third_speech_share_the_same_anchor(self):
        from PySide6.QtCore import QPoint
        pet = self.pet
        area = pet.screen().availableGeometry()
        pet.move(QPoint(area.left() + 300, area.top() + 300))
        pet.activity_cards.set_sessions([{'key': 'speech-qa', 'title': '布局测试', 'state': 'running'}])
        pet.activity_cards.set_active(True)
        pet.toolbar.set_active(True)
        # Seed a previous result with a different height to reproduce the
        # first-click stale layout rather than testing only a fresh bubble.
        pet.bubble.task_result('之前的长结果', '测试结果\n' * 12, lambda: None)
        pet.bubble.hide()
        for index, message in enumerate(('完成啦！给主人的一颗星星！' * 2, '今日份喜欢，已经送到主人的桌面啦。', '主人，这块饼干是思考的燃料！'), 1):
            pet.say(message, 4000)
            for _ in range(3):
                pet.toolbar.sync_geometry()
                pet.activity_cards.sync_geometry()
                self.app.processEvents()
                self.assert_speech_attached_vertically()
                self.assertEqual(pet.activity_cards.frameGeometry().bottom() + 1 + 8, pet.bubble.y())
            self.capture_speech_scene(f'speech-click-{index}.png')

    def test_speech_near_top_edge_attaches_below_pet(self):
        from PySide6.QtCore import QPoint
        pet = self.pet
        area = pet.screen().availableGeometry()
        pet.move(QPoint(area.left() + 300, area.top() + 10))
        pet.activity_cards.set_sessions([{'key': 'speech-qa', 'title': '布局测试', 'state': 'running'}])
        pet.activity_cards.set_active(True)
        pet.say('上方放不下时，消息框紧贴桌宠下方显示。', 4000)
        self.app.processEvents()
        self.assert_speech_attached_vertically(below=True)
        self.assertFalse(pet.bubble.frameGeometry().intersects(pet.toolbar.frameGeometry()))
        self.capture_speech_scene('speech-interaction-below.png')

    def test_click_interaction_during_task_is_still_an_interaction_box(self):
        from PySide6.QtCore import QPoint
        pet = self.pet
        area = pet.screen().availableGeometry()
        pet.move(QPoint(area.left() + 300, area.top() + 300))
        pet.task_progress('进展', '任务进行中', lambda: None)
        self.app.processEvents()
        self.assert_speech_beside_pet()
        pet.task_active = True
        # The upstream working animation suppresses speech; an interaction
        # animation permits it even while the task remains active.
        pet.mode = 'idle'
        pet.say('点击互动不受当前任务状态影响。', 4000)
        self.app.processEvents()
        self.assert_speech_attached_vertically()

    def test_message_size_is_fixed_for_text_length_position_and_manual_scale(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtWidgets import QTextBrowser
        from v051_behavior import MESSAGE_SIZE
        pet = self.pet
        pet.mode = 'idle'
        area = pet.screen().availableGeometry()
        messages = ('你好。', '一段比普通台词更长的测试消息。' * 6,
                    '\n'.join(f'第 {index} 行，完整保留的长消息。' for index in range(80)))
        for scale in (50, 100, 150):
            pet.set_bubble_percent(scale)
            expected = tuple(round(value * scale / 100) for value in MESSAGE_SIZE)
            for position in (area.topLeft(), QPoint(area.right() + 1 - pet.width(), area.top()),
                             QPoint(area.left(), area.bottom() + 1 - pet.height()),
                             QPoint(area.right() + 1 - pet.width(), area.bottom() + 1 - pet.height())):
                pet.move(position)
                for message in messages:
                    pet.say(message, 4000)
                    self.app.processEvents()
                    self.assertEqual((pet.bubble.width(), pet.bubble.height()), expected)
                    self.assert_speech_attached_vertically(below=position.y() == area.top())
                    view = pet.bubble.findChild(QTextBrowser, 'InteractionMessageText')
                    self.assertEqual(view.toPlainText(), message)
                    self.assertEqual(view.horizontalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                    self.assertEqual(view.verticalScrollBar().value(), 0)
                    if message == messages[0]:
                        text_height = view.document().documentLayout().documentSize().height()
                        expected_top = max(0, int((view.height() - text_height) / 2))
                        self.assertEqual(view.viewportMargins().top(), expected_top)
                        self.assertGreater(expected_top, 0)
                        self.assertEqual(view.verticalScrollBar().maximum(), 0)
                    if message == messages[-1]:
                        self.assertEqual(view.viewportMargins().top(), 0)
                        bar = view.verticalScrollBar()
                        self.assertGreater(bar.maximum(), 0)
                        bar.setValue(bar.maximum())
                        self.assertEqual(bar.value(), bar.maximum())
                        self.assertEqual((pet.bubble.width(), pet.bubble.height()), expected)
        pet.set_bubble_percent(100)
        self.capture_speech_scene('message-fixed-long.png')
        pet.say(messages[0], 4000)
        self.app.processEvents()
        self.capture_speech_scene('message-fixed-short.png')

    def test_fixed_message_and_task_notification_are_independent(self):
        pet = self.pet
        pet.mode = 'idle'
        pet.say('普通互动消息框。', 4000)
        self.assertEqual(pet.bubble.fixed_message_size, (256, 52))
        pet.task_completed('任务已完成。\n' * 20, lambda: None)
        self.app.processEvents()
        self.assertEqual(pet.bubble.fixed_message_size, (256, 52))
        self.assertTrue(pet.bubble.isVisible())
        self.assertIsNone(pet.notification_bubble.fixed_message_size)
        self.assertGreater(pet.notification_bubble.height(), 96)
        self.assert_speech_beside_pet()
        pet.mode = 'idle'
        pet.say('恢复普通消息框。', 4000)
        self.app.processEvents()
        self.assertEqual((pet.bubble.width(), pet.bubble.height()), (256, 52))

    def test_fractional_message_scaling_keeps_two_lines_without_spurious_scrollbars(self):
        from v051_behavior import MESSAGE_SIZE
        pet = self.pet
        pet.mode = 'idle'
        for scale in (118, *range(50, 151)):
            pet.set_bubble_percent(scale)
            pet.say('第一行文字。\n第二行文字。', 4000)
            for _ in range(3):
                self.app.processEvents()
            view = pet.bubble._message_view
            with self.subTest(scale=scale):
                self.assertEqual((pet.bubble.width(), pet.bubble.height()),
                                 tuple(round(value * scale / 100) for value in MESSAGE_SIZE))
                self.assertEqual(view.verticalScrollBar().maximum(), 0)
                self.assertGreaterEqual(view.viewport().height(), view.document().size().height())
        pet.set_bubble_percent(118)
        pet.say('第一行文字。\n第二行文字。', 4000)
        self.app.processEvents()
        self.capture_speech_scene('message-118-two-lines.png')
        pet.say('真正超出框体的长文字。\n' * 20, 4000)
        self.app.processEvents()
        self.assertGreater(pet.bubble._message_view.verticalScrollBar().maximum(), 0)
        pet.set_bubble_percent(100)

    def test_completion_and_progress_notifications_are_beside_pet(self):
        from PySide6.QtCore import QPoint
        pet = self.pet
        area = pet.screen().availableGeometry()
        for x in (area.left(), area.right() + 1 - pet.width(), area.center().x() - pet.width() // 2):
            pet.move(QPoint(x, area.center().y() - pet.height() // 2))
            pet.task_progress('任务进展', '正在处理任务。\n' * 20, lambda: None)
            self.app.processEvents()
            self.assert_speech_beside_pet()
            pet.task_completed('已完成。\n' * 20, lambda: None)
            self.app.processEvents()
            self.assert_speech_beside_pet()
            self.capture_speech_scene('speech-side-notifications.png')
            pet.close_notification()

    def test_dock_updates_do_not_reveal_and_geometry_does_not_move_pet(self):
        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QCursor
        pet = self.pet
        pet.activity_cards.set_sessions([{'key': 'qa-thread', 'state': 'running', 'title': '测试任务', 'project': 'QA'}])
        for edge in ('left', 'right'):
            pet.dock_edge = edge
            pet.dock_controls.lease = HoverLease()
            with patch.object(QCursor, 'pos', return_value=QPoint(-99999, -99999)):
                pet.dock_controls.refresh()
            before = pet.pos()
            pet.toolbar.sync_geometry()
            self.assertEqual(pet.pos(), before)
            pet.toolbar._reveal_animation.setCurrentTime(pet.toolbar._reveal_animation.duration())
            pet.activity_cards._reveal_animation.setCurrentTime(pet.activity_cards._reveal_animation.duration())
            self.assertFalse(pet.toolbar.isVisible())
            self.assertFalse(pet.activity_cards.isVisible())
            point = pet.mapToGlobal(pet.dock_rect().toAlignedRect().center())
            with patch.object(QCursor, 'pos', return_value=point):
                pet.dock_controls.refresh()
            self.assertTrue(pet.toolbar.isVisible())
            self.assertTrue(pet.activity_cards.isVisible())

    def test_settings_ranges_persist_and_pause_leisure(self):
        pet = self.pet
        self.use_wide_settings_screen()
        pet.show_settings()
        panel = pet.settings_panel
        self.assertTrue(panel.isVisible())
        for key, expected in [('size_percent', (50, 200)), ('bubble_percent', (50, 150))]:
            slider = panel.sliders[key][0]
            self.assertEqual((slider.minimum(), slider.maximum()), expected)
            slider.setValue(expected[0])
            self.assertEqual(getattr(pet, key), expected[0])
            slider.setValue(expected[1])
            self.assertEqual(getattr(pet, key), expected[1])
            slider.setValue(100)
        self.assertFalse(pet.movement_allowed(time.monotonic() + 120))
        self.assertFalse(pet.try_leisure())
        self.assertEqual(pet.settings.value('sizePercent', type=int), 100)
        panel.checks['notifications'][0].setChecked(False)
        self.assertTrue(pet.task_bubbles_disabled)

    def test_fixed_pet_default_and_saved_preference_load(self):
        from v051_behavior import V051Behavior
        from PySide6.QtCore import QSettings
        with tempfile.TemporaryDirectory(prefix='BailongFixed-') as folder:
            class Base:
                def __init__(self):
                    self.settings = QSettings(str(Path(folder) / 'settings.ini'), QSettings.Format.IniFormat)
            class Probe(V051Behavior, Base):
                pass
            first = Probe()
            self.assertFalse(first.fixed_pet)
            first.settings.setValue('fixedPet', True)
            first.settings.sync()
            self.assertTrue(Probe().fixed_pet)

    def test_fixed_pet_setting_preserves_movement_animations_and_unlock_has_no_jump(self):
        pet = self.pet
        pet.move(300, 300)
        origin = pet.pos()
        pet.auto_movement_disabled = False
        pet.set_fixed_pet(True)
        self.assertTrue(pet.settings.value('fixedPet', False, type=bool))
        self.assertTrue(pet.movement_allowed(time.monotonic() + 120))
        for item in pet.movement_slots:
            pet.dragging = pet.pointer_down = False
            pet.movement_ready_at = 0
            self.assertTrue(pet.start_movement(item))
            self.assertEqual(pet.mode, 'movement')
            for elapsed in (0, item['durationMs'] / 2000, item['durationMs'] / 1000):
                pet.advance_movement(elapsed)
                self.assertEqual(pet.pos(), origin)
            pet.finish_movement()
        self.assertTrue(pet.start_movement(pet.movement_slots[0], manual=True))
        pet.advance_movement(pet.current['durationMs'] / 1000)
        self.assertNotEqual(pet.pos(), origin)
        pet.finish_movement()
        pet.move(300, 300)
        origin = pet.pos()
        pet.movement_ready_at = 0
        self.assertTrue(pet.start_movement(pet.movement_slots[0]))
        pet.set_fixed_pet(False)
        pet.advance_movement(pet.current['durationMs'] / 1000)
        self.assertEqual(pet.pos(), origin)
        pet.move(origin.x() + 20, origin.y() + 20)
        self.assertNotEqual(pet.pos(), origin)
        pet.finish_movement()

    def test_fixed_pet_settings_toggle_still_allows_body_and_toolbar_drag(self):
        from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        pet = self.pet
        pet.move(300, 300)
        pet.show_settings()
        panel = pet.settings_panel
        check = panel.checks['fixed'][0]
        self.assertFalse(check.isChecked())
        check.setChecked(True)
        pet.show_settings()
        for widget in (pet, pet.toolbar.title):
            origin = pet.pos()
            local = widget.rect().center()
            start = widget.mapToGlobal(local)
            end = start + QPoint(120, 100)
            for kind, point, button, buttons in (
                (QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton),
                (QEvent.Type.MouseMove, end, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton),
                (QEvent.Type.MouseButtonRelease, end, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton),
            ):
                self.app.sendEvent(widget, QMouseEvent(kind, QPointF(local), QPointF(point), button, buttons, Qt.KeyboardModifier.NoModifier))
            self.app.processEvents()
            self.assertNotEqual(pet.pos(), origin)
        pet.click_timer.stop()
        pet.show_settings()
        self.assertTrue(check.isChecked())
        check.setChecked(False)
        self.assertFalse(pet.fixed_pet)
        self.assertFalse(pet.settings.value('fixedPet', True, type=bool))
        pet.show_settings()

    def test_composer_at_bottom_edge_does_not_cover_character(self):
        from PySide6.QtCore import QPoint, QRect
        from PySide6.QtWidgets import QWidget
        pet = self.pet
        composer = QWidget()
        composer.resize(320, 150)
        pet.chat = SimpleNamespace(composer=composer, enabled=True, position=lambda: None)
        area = pet.screen().availableGeometry()
        pet.move(QPoint(area.center().x(), area.bottom() + 1 - pet.height()))
        pet.toolbar.sync_geometry()
        rect = QRect(composer.x(), composer.y(), composer.width(), composer.height())
        self.assertTrue(area.contains(rect))
        self.assertFalse(rect.intersects(pet.frameGeometry()))
        pet.chat = None
        pet.toolbar._composer = None
        composer.deleteLater()

    def test_open_styled_composer_does_not_block_dragging_to_screen_bottom(self):
        import chat_ui
        from chat_ui_style import make_chat_widgets
        from conversation_sync import make_controller_class
        from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        pet = self.pet
        area = pet.screen().availableGeometry()
        pet.move(QPoint(area.right() + 1 - pet.width(), area.top() + 280))
        pet.activity_cards.set_sessions([{'key': 'drag-chat', 'title': '拖拽测试', 'state': 'completed'}])
        pet.activity_cards.set_active(True)
        pet.toolbar.set_active(True)
        Composer, Window = make_chat_widgets(chat_ui.Composer, chat_ui.ChatWindow)
        with tempfile.TemporaryDirectory(prefix='BailongChatDrag-') as folder:
            Controller = make_controller_class(chat_ui.ChatController, home=Path(folder), background=False)
            with patch.object(chat_ui, 'Composer', Composer), patch.object(chat_ui, 'ChatWindow', Window):
                controller = Controller(pet, directory=Path(folder))
            pet.chat = controller
            try:
                before_open = pet.pos()
                controller.toggle()
                self.app.processEvents()
                self.assertEqual(pet.pos(), before_open)
                title = pet.toolbar.title
                local = title.rect().center()
                start = title.mapToGlobal(local)
                delta = area.bottom() + 1 - pet.height() - pet.y()
                end = start + QPoint(0, delta)
                for kind, point, button, buttons in (
                    (QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton),
                    (QEvent.Type.MouseMove, end, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton),
                    (QEvent.Type.MouseButtonRelease, end, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton),
                ):
                    self.app.sendEvent(title, QMouseEvent(kind, QPointF(local), QPointF(point), button, buttons, Qt.KeyboardModifier.NoModifier))
                self.app.processEvents()
                self.assertEqual(pet.y() + pet.height(), area.bottom() + 1)
                for _ in range(3):
                    controller.position()
                    self.app.processEvents()
                    self.assertTrue(area.contains(controller.composer.frameGeometry()))
                    for widget in (pet, pet.toolbar, pet.activity_cards):
                        self.assertFalse(controller.composer.frameGeometry().intersects(widget.frameGeometry()))
                self.capture_speech_scene('chat-drag-bottom.png')
                controller.toggle()
                self.assertEqual(pet.y() + pet.height(), area.bottom() + 1)
            finally:
                controller.shutdown()
                pet.chat = None
                controller.composer.removeEventFilter(pet.toolbar)
                pet.toolbar._composer = None
                controller.window.deleteLater()
                controller.composer.deleteLater()
                self.app.processEvents()

    def test_drag_bounds_use_pet_only_on_negative_and_pointer_selected_monitors(self):
        from PySide6.QtCore import QPoint, QRect
        from PySide6.QtWidgets import QApplication
        pet = self.pet
        left = SimpleNamespace(availableGeometry=lambda: QRect(-1200, -100, 1200, 900))
        right = SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 1600, 1000))
        pet.chat = SimpleNamespace(enabled=True, composer=SimpleNamespace(width=lambda: 360, height=lambda: 232))
        try:
            with patch.object(QApplication, 'screens', return_value=[left, right]):
                self.assertEqual(pet.constrained_position(QPoint(-400, 1000)), QPoint(-400, 800 - pet.height()))
                with patch.object(QApplication, 'screenAt', return_value=right):
                    self.assertEqual(pet.constrained_position(QPoint(-400, 1000), QPoint(10, 900)),
                                     QPoint(0, 1000 - pet.height()))
                pet.chat.enabled = False
                self.assertEqual(pet.constrained_position(QPoint(-400, 1000)), QPoint(-400, 800 - pet.height()))
        finally:
            pet.chat = None

    def test_action_menu_has_no_settings_or_history(self):
        menu = self.pet.actions_menu()
        self.assertEqual([action.text() for action in menu.actions()], ['互动', '工作', '待机', '移动'])
        self.assertTrue(all(action.menu().actions() for action in menu.actions()))
        menu.deleteLater()

    def test_other_running_task_keeps_work_state_after_own_chat_finishes(self):
        pet = self.pet
        pet.desktop_sync.running.add(('other-thread', 'turn-1'))
        pet.task_active = True
        pet.task_finished()
        self.assertTrue(pet.task_active)
        pet.desktop_sync.running.clear()
        pet.task_finished()
        self.assertFalse(pet.task_active)

    def test_quota_result_is_not_overwritten_by_task_notices(self):
        pet = self.pet
        pet.bubble.is_quota_result = True
        pet.bubble.is_task_bubble = False
        pet.bubble.show()
        pet.bubble_timer.start(12000)
        pet.task_progress('进展', '测试进展', lambda: None)
        pet.task_completed('测试完成', lambda: None)
        self.assertTrue(pet.bubble.is_quota_result)
        self.assertEqual(len(pet._notices), 1)
        self.assertEqual(pet._notices[0][0], 'task_completed')
        pet.bubble.hide()
        pet.bubble_timer.stop()
        pet._flush_notices()
        self.assertFalse(pet._quota_lease())
        self.assertEqual(pet.notification_bubble.result_title.text(), '主人，完成啦！')

    def test_visual_artifacts(self):
        from PySide6.QtCore import QPoint
        pet = self.pet
        area = pet.screen().availableGeometry()
        pet.move(area.topLeft() + QPoint(180, 100))
        pet.set_size_percent(100)
        pet.toolbar.sync_visibility()
        pet.show_settings()
        self.app.processEvents()
        folder = Path(__file__).resolve().parents[2] / 'analysis/ui-preview'
        folder.mkdir(parents=True, exist_ok=True)
        pet.toolbar._reveal_animation.setCurrentTime(pet.toolbar._reveal_animation.duration())
        self.assertTrue(pet.toolbar.grab().save(str(folder / 'toolbar.png')))
        pet.toolbar._reveal_animation.setCurrentTime(pet.toolbar._reveal_animation.duration())
        self.assertTrue(pet.settings_panel.grab().save(str(folder / 'settings.png')))

    def test_settings_opens_clear_of_pet_and_updates_after_scaling(self):
        pet = self.pet
        self.use_wide_settings_screen()
        pet.show_settings()
        self.app.processEvents()
        self.assertFalse(pet.settings_panel.frameGeometry().intersects(pet.frameGeometry()))
        pet.set_size_percent(200)
        self.app.processEvents()
        self.assertFalse(pet.settings_panel.frameGeometry().intersects(pet.frameGeometry()))
        self.assertTrue(pet.screen().availableGeometry().contains(pet.settings_panel.frameGeometry()))
        pet.settings_panel.hide()
        pet.set_size_percent(100)

    def test_corner_and_dock_toolbar_and_cards_do_not_intersect(self):
        from PySide6.QtCore import QPoint
        pet = self.pet
        area = pet.screen().availableGeometry()
        for edge in (None, 'left', 'right'):
            for x in (area.left(), area.right() + 1 - pet.width()):
                for y in (area.top(), area.bottom() + 1 - pet.height()):
                    pet.dock_edge = edge
                    pet.move(QPoint(x, y))
                    pet.dock_controls.visible = True
                    pet.toolbar.sync_visibility(True)
                    pet.activity_cards.set_sessions([{'key': 'layout-qa', 'title': '布局测试', 'state': 'running'}])
                    pet.activity_cards.set_active(True)
                    from companion_layout import companion_owner
                    from PySide6.QtCore import QRect
                    body = QRect(*companion_owner(pet))
                    self.assertFalse(pet.toolbar.frameGeometry().intersects(body))
                    self.assertFalse(pet.activity_cards.frameGeometry().intersects(body))
                    self.assertFalse(pet.activity_cards.frameGeometry().intersects(pet.toolbar.frameGeometry()))

    def test_reveal_fades_once_and_polling_does_not_restart_it(self):
        toolbar = self.pet.toolbar
        toolbar.set_active(True)
        toolbar._reveal_animation.setCurrentTime(toolbar._reveal_animation.duration())
        toolbar.set_active(False)
        self.assertTrue(toolbar.isVisible())
        toolbar._reveal_animation.setCurrentTime(225)
        self.assertGreater(toolbar._reveal_effect.opacity(), 0)
        self.assertLess(toolbar._reveal_effect.opacity(), 1)
        toolbar.sync_visibility(False)
        self.assertEqual(toolbar._reveal_animation.currentTime(), 225)
        toolbar._reveal_animation.setCurrentTime(toolbar._reveal_animation.duration())
        self.assertFalse(toolbar.isVisible())
        toolbar.set_active(True)
        animation = toolbar._reveal_animation
        self.assertEqual(animation.duration(), 640)
        self.assertEqual(toolbar._reveal_effect.opacity(), 0)
        animation.setCurrentTime(225)
        opacity = toolbar._reveal_effect.opacity()
        self.assertGreater(opacity, 0)
        self.assertLess(opacity, 1)
        toolbar.sync_visibility()
        self.assertEqual(animation.currentTime(), 225)
        animation.setCurrentTime(animation.duration())
        self.assertEqual(toolbar._reveal_effect.opacity(), 1)

    def test_quota_stays_visible_during_work_and_fits_near_corners(self):
        from PySide6.QtCore import QPoint
        pet = self.pet
        area = pet.screen().availableGeometry()
        windows = [{'name': '5 小时', 'remaining': 75, 'resetsAt': None},
                   {'name': '每周', 'remaining': 50, 'resetsAt': None}]
        for x in (area.left(), area.right() + 1 - pet.width()):
            for y in (area.top(), area.bottom() + 1 - pet.height()):
                pet.move(QPoint(x, y))
                pet.mode = 'working'
                pet.quota_generation = pet.generation
                pet.quota_ready(windows)
                panel = pet.quota_panel
                self.assertTrue(panel.isVisible())
                self.assertTrue(area.contains(panel.frameGeometry()))
                self.assertFalse(panel.frameGeometry().intersects(pet.frameGeometry()))
                self.assertTrue(panel.frameGeometry().right() < pet.frameGeometry().left()
                                or panel.frameGeometry().left() > pet.frameGeometry().right())
                pet.idle()
                self.assertTrue(panel.isVisible())
                self.assertTrue(pet._quota_lease())
                pet.dismiss_bubble()

    def test_minus_button_finishes_panel_fade_before_hiding_owner(self):
        pet = self.pet
        toolbar = pet.toolbar
        toolbar.sync_visibility(True)
        toolbar._reveal_animation.setCurrentTime(toolbar._reveal_animation.duration())
        with patch.object(pet, 'set_manual_hidden', side_effect=lambda hidden: pet.hide()) as hide:
            toolbar.request_hide()
            self.assertTrue(pet.isVisible())
            self.assertFalse(toolbar._reveal_target)
            toolbar._reveal_animation.setCurrentTime(225)
            self.assertGreater(toolbar._reveal_effect.opacity(), 0)
            self.assertLess(toolbar._reveal_effect.opacity(), 1)
            toolbar._reveal_animation.setCurrentTime(toolbar._reveal_animation.duration())
            toolbar._hide_timer.timeout.emit()
            hide.assert_called_once_with(True)
            self.assertFalse(pet.isVisible())
            self.assertFalse(pet._local_hide_animation)

    def test_quota_windows_fit_content_and_bars_have_rounded_caps(self):
        pet = self.pet
        pet.quota_generation = pet.generation
        pet.quota_ready([{'name': '5 小时', 'remaining': 61, 'resetsAt': 1800000000},
                         {'name': '一周', 'remaining': 94, 'resetsAt': 1800300000}])
        panel = pet.quota_panel
        self.app.processEvents()
        self.assertLess(panel.height(), 270)
        self.assertLessEqual(panel.scroll.viewport().height() - panel.content_height, 8)
        bar = panel.bars[0]
        image = bar.grab().toImage()
        self.assertNotEqual(image.pixelColor(0, 0).rgb(), image.pixelColor(6, 6).rgb())
        folder = Path(__file__).resolve().parents[2] / 'analysis/ui-preview'
        panel.grab().save(str(folder / 'quota-result.png'))
        panel.display([], loading=True)
        self.app.processEvents()
        self.assertLess(panel.height(), 160)
        self.assertLessEqual(panel.scroll.viewport().height() - panel.content_height, 8)
        panel.grab().save(str(folder / 'quota-loading.png'))
        pet.dismiss_bubble()

    def test_quota_result_uses_independent_twelve_second_deadline(self):
        pet = self.pet
        pet.quota_generation = pet.generation
        pet.quota_ready([{'name': '5 小时', 'remaining': 61, 'resetsAt': None}])
        self.assertEqual(pet._quota_hide_timer.interval(), 12000)
        self.assertTrue(pet._quota_hide_timer.isActive())
        pet.bubble_timer.stop()
        self.assertTrue(pet.quota_panel.isVisible())
        pet._quota_hide_timer.timeout.emit()
        self.assertFalse(pet.quota_panel.isVisible())

    def test_connection_timeout_shows_failure_before_hiding_and_ignores_late_result(self):
        from PySide6.QtCore import QObject, Signal
        from PySide6.QtWidgets import QLabel
        from threading import Event
        class Worker(QObject):
            ready = Signal(object)
            failed = Signal(str)
            def __init__(self, parent):
                super().__init__(parent)
                self.cancel = Event()
            def start(self): pass
            def isRunning(self): return False
        pet = self.pet
        with patch.object(self.original, 'QuotaWorker', Worker):
            pet.quota()
            self.assertTrue(pet.bubble.isVisible())
            self.assertTrue(pet.quota_panel.isVisible())
            self.assertTrue(pet._quota_connect_timer.isActive())
            self.assertEqual(pet._quota_connect_timer.interval(), 30000)
            self.assertFalse(pet._quota_hide_timer.isActive()) if hasattr(pet, '_quota_hide_timer') else None
            pet._quota_connect_timer.timeout.emit()
            self.assertTrue(pet.quota_panel.isVisible())
            text = '\n'.join(label.text() for label in pet.quota_panel.scroll.widget().findChildren(QLabel))
            self.assertIn('与 Codex 连接失败', text)
            self.assertEqual(pet._quota_hide_timer.interval(), 8000)
            self.assertTrue(pet.worker.cancel.is_set())
            pet.worker.ready.emit([{'name': '过期结果', 'remaining': 61, 'resetsAt': None}])
            self.assertIn('与 Codex 连接失败', text)
            pet._quota_hide_timer.timeout.emit()
            self.assertFalse(pet.quota_panel.isVisible())
            pet.worker = None

    def test_quota_keeps_original_query_speech_without_starting_real_query(self):
        from PySide6.QtCore import QObject, Signal
        class Worker(QObject):
            ready = Signal(object)
            failed = Signal(str)
            def start(self): pass
            def isRunning(self): return False
        pet = self.pet
        from PySide6.QtCore import QPoint
        area = pet.screen().availableGeometry()
        pet.move(QPoint(area.left() + 300, area.top() + 300))
        with patch.object(self.original, 'QuotaWorker', Worker):
            pet.quota()
            self.assertTrue(pet.bubble.isVisible())
            self.assertTrue(pet.quota_panel.isVisible())
            self.assertFalse(pet.bubble.frameGeometry().intersects(pet.quota_panel.frameGeometry()))
            self.assert_speech_attached_vertically()
            self.assertEqual((pet.bubble.width(), pet.bubble.height()), (256, 52))
            speech, panel = pet.frameGeometry(), pet.quota_panel.frameGeometry()
            self.assertTrue(panel.right() < speech.left() or panel.left() > speech.right())
            pet.worker.ready.emit([{'name': '5 小时', 'remaining': 61, 'resetsAt': None}])
            self.assertTrue(pet.quota_panel.isVisible())
            self.assertFalse(pet.bubble.isVisible())
            pet.worker = None
        pet.dismiss_bubble()


if __name__ == '__main__':
    unittest.main(verbosity=2)
