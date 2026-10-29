"""Exercise real Qt transition timelines, target stability and interruption."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class MotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from bridge_loader import load_original_module
        original = load_original_module()
        cls.app = original.QApplication.instance() or original.QApplication(['motion-qa'])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QWidget, QDialog
        from ui_motion import MotionWindowMixin
        class Panel(MotionWindowMixin, QWidget): pass
        class Dialog(MotionWindowMixin, QDialog): pass
        self.Panel, self.Dialog = Panel, Dialog
        self.owner = QWidget()
        self.owner.setGeometry(320, 300, 160, 180)
        self.owner.dock_edge = None
        self.owner._ui_motion_enabled = True
        self.owner.exit_reason = None
        self.owner.manual_hidden = False
        self.owner.fullscreen_hidden = False
        self.owner.show()
        self.widgets = []
        self.Qt = Qt
        self.app.processEvents()

    def panel(self, role='notice', point=(488, 330), dialog=False):
        from ui_motion import attach
        widget = (self.Dialog if dialog else self.Panel)(None, self.Qt.WindowType.Tool)
        widget.resize(180, 100)
        self.widgets.append(widget)
        motion = attach(widget, self.owner, role)
        motion.move_to(point)
        return widget, motion

    def tearDown(self):
        self.owner.exit_reason = 'test-end'
        for widget in self.widgets:
            widget._ui_motion.cancel()
            widget.deleteLater()
        self.owner.hide()
        self.owner.deleteLater()
        self.app.processEvents()

    def test_entry_uses_half_speed_and_layout_reads_final_target(self):
        from ui_motion import geometry
        from PySide6.QtCore import QPoint
        widget, motion = self.panel()
        widget.show()
        self.assertEqual(motion.animation.duration(), 640)
        self.assertEqual(widget.windowOpacity(), 0)
        self.assertNotEqual(widget.pos(), QPoint(488, 330))
        self.assertEqual(geometry(widget), (488, 330, 180, 100))
        self.assertTrue(widget.testAttribute(self.Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        motion.animation.setCurrentTime(320)
        self.assertGreater(widget.windowOpacity(), 0)
        self.assertLess(widget.windowOpacity(), 1)
        motion.finish()
        self.assertEqual(widget.pos(), QPoint(488, 330))
        self.assertEqual(widget.windowOpacity(), 1)
        self.assertFalse(widget.testAttribute(self.Qt.WidgetAttribute.WA_TransparentForMouseEvents))

    def test_repeated_layout_does_not_restart_and_retarget_keeps_current_position(self):
        widget, motion = self.panel()
        widget.show()
        motion.animation.setCurrentTime(200)
        animation = motion.animation
        for _ in range(20):
            motion.move_to((488, 330))
        self.assertIs(motion.animation, animation)
        self.assertEqual(animation.currentTime(), 200)
        motion.finish()
        motion.move_to((488, 420))
        self.assertEqual(motion.animation.duration(), 400)
        motion.animation.setCurrentTime(200)
        current = widget.pos()
        motion.move_to((488, 440))
        self.assertEqual(widget.pos(), current)
        motion.finish()
        self.assertEqual(widget.y(), 440)

    def test_side_change_disappears_before_teleporting_and_reappearing(self):
        from PySide6.QtCore import QPoint
        widget, motion = self.panel()
        widget.show()
        motion.finish()
        motion.move_to((132, 330))
        self.assertEqual(motion.kind, 'swap')
        self.assertEqual(motion.animation.duration(), 800)
        motion.animation.setCurrentTime(320)
        self.assertEqual(widget.windowOpacity(), 0)
        old_side_home = widget.pos()
        motion.animation.setCurrentTime(399)
        self.assertEqual(widget.pos(), old_side_home)
        self.assertEqual(widget.windowOpacity(), 0)
        motion.animation.setCurrentTime(400)
        self.assertEqual(widget.windowOpacity(), 0)
        motion.animation.setCurrentTime(600)
        self.assertGreater(widget.windowOpacity(), 0)
        motion.finish()
        self.assertEqual(widget.pos(), QPoint(132, 330))

    def test_owner_movement_follows_immediately_without_drag_lag(self):
        from PySide6.QtCore import QPoint
        widget, motion = self.panel()
        widget.show()
        motion.animation.setCurrentTime(200)
        self.owner.move(340, 320)
        motion.move_to((508, 350))
        self.assertFalse(motion.active())
        self.assertEqual(widget.pos(), QPoint(508, 350))
        self.assertEqual(widget.windowOpacity(), 1)

    def test_dragging_side_panels_swap_and_keep_one_timeline_while_target_follows(self):
        from PySide6.QtCore import QPoint
        for role in ('settings', 'quota', 'notice'):
            self.owner.move(320, 300)
            self.owner.dragging = False
            widget, motion = self.panel(role)
            widget.show()
            motion.finish()
            motion.arm_expiry(12000)
            deadline = motion.expiry_at
            self.owner.dragging = True
            self.owner.move(500, 300)
            motion.move_to((312, 330))
            self.assertEqual(motion.kind, 'swap', role)
            animation = motion.animation
            self.assertEqual(animation.duration(), 800)
            for elapsed, owner_x in ((150, 520), (320, 540), (399, 550), (600, 560)):
                animation.setCurrentTime(elapsed)
                self.owner.move(owner_x, 300)
                motion.move_to((owner_x - 188, 330))
                self.assertIs(motion.animation, animation)
                self.assertEqual(animation.currentTime(), elapsed)
                self.assertEqual(motion.expiry_at, deadline)
                if elapsed in (320, 399):
                    self.assertEqual(widget.windowOpacity(), 0)
            motion.finish()
            self.assertEqual(widget.pos(), QPoint(372, 330))
            self.assertEqual(widget.windowOpacity(), 1)

    def test_controls_swap_above_below_even_when_owner_and_old_anchor_move(self):
        from PySide6.QtCore import QPoint
        for role in ('toolbar', 'cards'):
            self.owner.move(320, 300)
            self.owner.dragging = False
            widget, motion = self.panel(role, (310, 192))
            widget.show()
            motion.finish()
            self.owner.dragging = True
            self.owner.move(320, 50)
            motion.move_to((310, 238))
            self.assertEqual(motion.kind, 'swap', role)
            animation = motion.animation
            animation.setCurrentTime(200)
            self.owner.move(340, 60)
            motion.move_to((330, 248))
            self.assertIs(motion.animation, animation)
            self.assertEqual(animation.currentTime(), 200)
            animation.setCurrentTime(320)
            self.assertEqual(widget.windowOpacity(), 0)
            motion.finish()
            self.assertEqual(widget.pos(), QPoint(330, 248))

    def test_owner_motion_without_drag_flag_still_animates_a_side_change(self):
        widget, motion = self.panel('settings')
        widget.show()
        motion.finish()
        self.owner.move(500, 300)
        motion.move_to((312, 330))
        self.assertEqual(motion.kind, 'swap')
        self.assertEqual(motion.animation.duration(), 800)

    def test_drag_reversal_during_retraction_returns_smoothly_from_current_opacity(self):
        widget, motion = self.panel('cards', (310, 192))
        widget.show()
        motion.finish()
        self.owner.dragging = True
        self.owner.move(320, 50)
        motion.move_to((310, 238))
        motion.animation.setCurrentTime(200)
        opacity = widget.windowOpacity()
        self.owner.move(320, 300)
        motion.move_to((310, 192))
        self.assertEqual(motion.kind, 'move')
        self.assertEqual(motion.animation.duration(), 400)
        self.assertAlmostEqual(widget.windowOpacity(), opacity)
        motion.animation.setCurrentTime(200)
        self.assertGreater(widget.windowOpacity(), opacity)
        motion.finish()
        self.assertEqual(widget.windowOpacity(), 1)

    def test_resizing_uses_stable_target_and_can_be_retargeted_without_jumping(self):
        from ui_motion import geometry
        from PySide6.QtCore import QSize
        widget, motion = self.panel('quota')
        widget.show()
        motion.finish()
        motion.arm_expiry(12000)
        deadline = motion.expiry_at
        motion.resize_to((220, 190))
        self.assertEqual(motion.size_animation.duration(), 400)
        self.assertEqual(widget.size(), QSize(180, 100))
        self.assertEqual(geometry(widget)[2:], (220, 190))
        motion.size_animation.setCurrentTime(200)
        animation = motion.size_animation
        current = widget.size()
        for _ in range(10):
            motion.resize_to((220, 190))
        self.assertIs(motion.size_animation, animation)
        self.assertEqual(widget.size(), current)
        motion.resize_to((240, 210))
        self.assertEqual(widget.size(), current)
        motion.finish()
        self.assertEqual(widget.size(), QSize(240, 210))
        self.assertEqual(motion.expiry_at, deadline)

    def test_docking_has_one_shared_deadline_and_dragging_stays_immediate(self):
        from PySide6.QtCore import QPoint
        widget, motion = self.panel()
        widget.show()
        motion.finish()
        now = [100.0]
        motion.clock = lambda: now[0]
        with patch('companion_layout.companion_owner', return_value=(320, 300, 160, 180)):
            self.owner.dock_edge = 'right'
            motion.move_to((488, 345))
            self.assertEqual(motion.kind, 'dock')
            self.assertEqual(motion.animation.duration(), 600)
            motion.animation.setCurrentTime(200)
            current = widget.pos()
            now[0] = 100.2
            motion.move_to((488, 360))
            self.assertEqual(widget.pos(), current)
            self.assertEqual(motion.animation.duration(), 400)
            self.owner.dragging = True
            self.owner.move(340, 320)
            motion.move_to((508, 350))
            self.assertFalse(motion.active())
            self.assertEqual(widget.pos(), QPoint(508, 350))

    def test_screen_relocation_jumps_to_safe_position_then_fades_in(self):
        from PySide6.QtCore import QPoint
        widget, motion = self.panel()
        widget.show()
        motion.finish()
        changed = (*motion._stamp[:5], ('changed-screen', 2, (0, 0, 1600, 1000)))
        with patch.object(motion, 'owner_stamp', return_value=changed):
            motion.move_to((490, 350))
            animation = motion.animation
            animation.setCurrentTime(200)
            with patch.object(widget, 'screen', return_value=None):
                for _ in range(10):
                    motion.move_to((490, 350))
            self.assertIs(motion.animation, animation)
            self.assertEqual(animation.currentTime(), 200)
            animation.setCurrentTime(0)
        self.assertEqual(widget.pos(), QPoint(490, 350))
        self.assertEqual(motion.kind, 'screen')
        self.assertEqual(motion.animation.duration(), 640)
        self.assertEqual(widget.windowOpacity(), 0)
        motion.animation.setCurrentTime(320)
        self.assertEqual(widget.pos(), QPoint(490, 350))
        self.assertGreater(widget.windowOpacity(), 0)
        motion.finish()

    def test_history_fades_in_place_and_remains_draggable(self):
        from PySide6.QtCore import QPoint
        widget, motion = self.panel('history', (60, 110))
        widget.show()
        initial = widget.pos()
        self.assertFalse(widget.testAttribute(self.Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        self.assertEqual(motion.animation.duration(), 640)
        motion.animation.setCurrentTime(200)
        self.assertEqual(widget.pos(), initial)
        widget.move(40, 70)
        motion.animation.setCurrentTime(320)
        self.assertEqual(widget.pos(), QPoint(40, 70))
        motion.finish()
        self.assertEqual(widget.pos(), QPoint(40, 70))
        self.owner.hide()
        widget.hide()
        self.assertEqual(motion.animation.duration(), 440)
        motion.animation.setCurrentTime(220)
        self.assertEqual(widget.pos(), QPoint(40, 70))
        motion.finish()
        self.assertFalse(widget.isVisible())

    def test_replacement_content_uses_same_half_speed_without_changing_size(self):
        from PySide6.QtWidgets import QLabel
        from ui_motion import fade_content
        widget, motion = self.panel()
        label = QLabel('新内容', widget)
        before = widget.size()
        animation = fade_content(label, self.owner)
        self.assertEqual(animation.duration(), 400)
        self.assertEqual(label.graphicsEffect().opacity(), 0)
        animation.setCurrentTime(200)
        self.assertGreater(label.graphicsEffect().opacity(), 0)
        self.assertLess(label.graphicsEffect().opacity(), 1)
        animation.setCurrentTime(400)
        self.assertEqual(label.graphicsEffect().opacity(), 1)
        self.assertEqual(widget.size(), before)

    def test_controls_retract_on_vertical_swap_and_really_hide_after_fade(self):
        widget, motion = self.panel('cards', (310, 192))
        widget.show()
        motion.finish()
        motion.move_to((310, 488))
        self.assertEqual(motion.kind, 'swap')
        self.assertEqual(motion.animation.duration(), 800)
        motion.animation.setCurrentTime(320)
        self.assertEqual(widget.windowOpacity(), 0)
        motion.finish()
        motion.retract_for_fade()
        self.assertTrue(motion.closing)
        motion.finish()
        widget.hide()
        self.assertFalse(widget.isVisible())
        self.assertFalse(motion.closing)

    def test_rapid_side_reversal_uses_current_visible_side_without_crossing(self):
        widget, motion = self.panel()
        widget.show()
        motion.finish()
        motion.move_to((132, 330))
        motion.animation.setCurrentTime(150)
        current = widget.x()
        motion.move_to((488, 330))
        self.assertEqual(motion.kind, 'move')
        for time in (0, 100, 200, 300):
            motion.animation.setCurrentTime(time)
            self.assertGreaterEqual(widget.x(), current)
        motion.finish()
        self.assertEqual(widget.x(), 488)

    def test_dialog_accept_finishes_before_mutually_exclusive_panel_appears(self):
        saved, accepted = [], []
        self.owner.settings = SimpleNamespace(sync=lambda: saved.append(True))
        settings, closing = self.panel('settings', dialog=True)
        settings.accepted.connect(lambda: accepted.append(True))
        settings.show()
        closing.finish()
        settings.accept()
        self.assertEqual(len(saved), 1)
        self.assertTrue(closing.closing)
        self.assertEqual(closing.animation.duration(), 440)
        quota, entering = self.panel('quota')
        quota.show()
        self.assertEqual(entering.animation.duration(), 1080)
        entering.animation.setCurrentTime(439)
        self.assertEqual(quota.windowOpacity(), 0)
        closing.finish()
        self.assertFalse(settings.isVisible())
        self.assertEqual(len(accepted), 1)
        entering.finish()
        self.assertEqual(quota.windowOpacity(), 1)

    def test_expiry_includes_retraction_and_is_not_extended_by_reposition(self):
        widget, motion = self.panel('quota')
        now = [100.0]
        motion.clock = lambda: now[0]
        widget.show()
        motion.finish()
        motion.arm_expiry(12000)
        self.assertEqual(motion.expiry_at, 112.0)
        self.assertEqual(motion.expiry_timer.interval(), 11560)
        motion.move_to((488, 380))
        self.assertEqual(motion.expiry_at, 112.0)
        motion.finish()
        now[0] = 111.56
        motion.expiry_timer.timeout.emit()
        self.assertTrue(motion.closing)
        self.assertLessEqual(motion.animation.duration(), 440)
        now[0] = 112.0
        widget.hide()
        self.assertFalse(widget.isVisible())
        self.assertFalse(motion.active())

    def test_cancelled_waiting_panel_cannot_resurrect_after_previous_window_closes(self):
        first, a = self.panel('settings', dialog=True)
        first.show()
        a.finish()
        first.accept()
        second, b = self.panel('quota')
        second.show()
        second.hide()
        b.finish()
        a.finish()
        self.app.processEvents()
        self.assertFalse(second.isVisible())
        self.assertFalse(b.intent)

    def test_external_hide_and_shutdown_cancel_all_motion(self):
        from PySide6.QtWidgets import QWidget
        widget, motion = self.panel('quota')
        widget.show()
        motion.resize_to((240, 180))
        QWidget.hide(widget)
        self.assertFalse(motion.active())
        self.assertIsNone(motion.size_animation)
        self.assertFalse(motion.intent)
        widget.show()
        self.owner.exit_reason = 'shutdown'
        widget.hide()
        self.assertFalse(widget.isVisible())
        self.assertFalse(motion.active())


if __name__ == '__main__':
    unittest.main()
