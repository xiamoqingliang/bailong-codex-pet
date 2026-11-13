"""Pet-only mode with real Qt controls, isolated settings and synthetic frames."""
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class PetOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from bridge_loader import load_original_module
        from v051_behavior import V051Behavior
        cls.original = original = load_original_module()
        cls.app = original.QApplication.instance() or original.QApplication(['pet-only-test'])
        cls.app.setQuitOnLastWindowClosed(False)
        from PySide6.QtGui import QColor, QPixmap
        from PySide6.QtWidgets import QSystemTrayIcon

        class LeanOriginal(original.Pet):
            def load(self, item):
                # Exercise the real constructor and behavior without decoding
                # animation assets or creating any model/network requests.
                if not hasattr(self, '_fixture_frame'):
                    self._fixture_frame = QPixmap(self.frame_size, self.frame_size)
                    self._fixture_frame.fill(QColor('#b9a4d8'))
                return [self._fixture_frame]

            def setup_tray(self):
                self.tray = QSystemTrayIcon(self)
                self.tray_menu = self.context_menu()
                self.tray.setContextMenu(self.tray_menu)

            def toggle_chat(self):
                self.chat.enabled = not self.chat.enabled
                self.chat.composer.setVisible(self.chat.enabled)

        class Pet(V051Behavior, LeanOriginal):
            toolbar = None
            exit_reason = None
            _ui_motion_disabled = True

        cls.Pet = Pet

    def setUp(self):
        from PySide6.QtCore import QSettings
        from v051_behavior import configure_preferences
        self.directory = tempfile.TemporaryDirectory(prefix='bailong-pet-only-')
        self.addCleanup(self.directory.cleanup)
        self.settings_path = Path(self.directory.name) / 'preferences.ini'
        preferences = QSettings(str(self.settings_path), QSettings.Format.IniFormat)
        preferences.setValue('soundDisabled', True)
        preferences.sync()
        old_settings = self.original.QSettings
        configure_preferences(self.original, self.settings_path)
        self.addCleanup(setattr, self.original, 'QSettings', old_settings)
        self.pets = []
        self.addCleanup(self.dispose)
        self.pet = self.make_pet()

    def make_pet(self):
        from bailong_toolbar import create_toolbar
        from activity_cards import create_activity_cards
        from dock_controls import create_dock_controls
        pet = self.Pet()
        self.pets.append(pet)
        pet.tick.stop()
        pet.visibility_timer.stop()
        pet.desktop_sync.stop()
        pet.sound_disabled = True
        pet.set_size_percent(65)
        pet.move(280, 250)
        pet.toolbar = create_toolbar(self.original, pet)
        pet.activity_cards = create_activity_cards(self.original, pet)
        pet.activity_cards.set_sessions([{'key': 'fixture', 'title': '后台任务', 'state': 'running'}])
        pet.dock_controls = create_dock_controls(pet)
        pet.dock_controls.timer.stop()
        pet.show()
        pet.toolbar.sync_visibility(True)
        pet.activity_cards.set_active(True)
        self.app.processEvents()
        return pet

    def dispose(self):
        from PySide6.QtCore import QTimer
        for pet in self.pets:
            pet.exit_reason = 'fixture-complete'
            pet.desktop_sync.stop()
            policy = getattr(pet, '_window_policy', None)
            if policy is not None:
                policy.drop_pending()
            for timer in pet.findChildren(QTimer):
                timer.stop()
            for motion in list(getattr(pet, '_ui_motions', ())):
                motion.cancel()
            windows = [widget for _, widget in policy.windows()] if policy else [pet.bubble, pet]
            for widget in windows:
                if widget is not None:
                    widget.hide()
                    widget.deleteLater()
            pet.tray.hide()
            pet.tray.deleteLater()
            pet.tray_menu.deleteLater()
        self.app.processEvents()

    def auxiliary_windows(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QWidget
        from ui_motion import MotionWindowMixin
        from quota_panel import create_quota_panel
        pet = self.pet

        class Panel(MotionWindowMixin, QWidget):
            pass

        composer = Panel(None, Qt.WindowType.Tool)
        history = Panel(None, Qt.WindowType.Tool)
        for widget in (composer, history):
            widget.resize(220, 160)
        chat = SimpleNamespace(composer=composer, window=history, enabled=False, position=lambda: None)

        def hide_chat():
            chat.enabled = False
            composer.hide()
            history.hide()

        chat.hide = hide_chat
        pet.chat = chat
        pet.quota_panel = create_quota_panel(pet)
        policy = pet.window_policy()
        for role, widget in (('composer', composer), ('history', history), ('quota', pet.quota_panel)):
            policy.watch(widget, role)
        return composer, history, pet.quota_panel

    def assert_controls_hidden(self):
        self.assertTrue(self.pet.isVisible())
        for widget in (self.pet.toolbar, self.pet.activity_cards):
            self.assertFalse(widget.isVisible())
            self.assertFalse(widget._ui_motion.intent)

    def test_default_and_restart_persistence_leave_other_preferences_and_tasks_unchanged(self):
        from PySide6.QtCore import QSettings
        pet = self.pet
        self.assertFalse(pet.pet_only)
        self.assertTrue(pet.toolbar.isVisible())
        self.assertTrue(pet.activity_cards.isVisible())
        attrs = ('auto_movement_disabled', 'fixed_pet', 'auto_hide_fullscreen',
                 'manual_hidden', 'fullscreen_hidden', 'sound_disabled', 'task_bubbles_disabled')
        before = {name: getattr(pet, name) for name in attrs}
        task = {'fixture-thread': {'status': 'running'}}
        pet.desktop_sync.running = task
        pet.set_pet_only(True)
        self.assert_controls_hidden()
        settings = QSettings(str(self.settings_path), QSettings.Format.IniFormat)
        self.assertTrue(settings.value('petOnly', False, type=bool))
        restarted = self.make_pet()
        self.assertTrue(restarted.pet_only)
        self.assertTrue(next(action for action in restarted.tray.contextMenu().actions()
                             if action.objectName() == 'BailongPetOnly').isChecked())
        self.assertFalse(restarted.toolbar.isVisible())
        self.assertFalse(restarted.activity_cards.isVisible())
        self.assertEqual({name: getattr(pet, name) for name in attrs}, before)
        self.assertIs(pet.desktop_sync.running, task)

    def test_settings_checkbox_and_context_menu_offer_a_visible_recovery_path(self):
        from PySide6.QtWidgets import QCheckBox
        pet = self.pet
        pet.show_settings()
        checkbox = pet.settings_panel.findChild(QCheckBox, 'petOnly')
        self.assertIsNotNone(checkbox)
        self.assertEqual(checkbox.text(), '独显桌宠')
        checkbox.click()
        self.assertTrue(pet.pet_only)
        self.assertTrue(pet.settings_panel.isVisible())
        self.assert_controls_hidden()
        menu = pet.context_menu()
        action = next(action for action in menu.actions() if action.objectName() == 'BailongPetOnly')
        self.assertTrue(action.isCheckable())
        self.assertTrue(action.isChecked())
        action.trigger()
        self.assertFalse(pet.pet_only)
        self.assertFalse(checkbox.isChecked())
        self.assertTrue(pet.toolbar.isVisible())
        pet.settings_panel.accept()
        settings = next(action for action in menu.actions() if action.objectName() == 'BailongSettingsAction')
        settings.trigger()
        self.assertTrue(pet.settings_panel.isVisible())
        menu.deleteLater()

    def test_hover_task_updates_speech_and_automatic_notices_cannot_reveal_panels(self):
        from PySide6.QtGui import QCursor
        pet = self.pet
        pet.set_pet_only(True)
        pet.dock_edge = 'left'
        point = pet.mapToGlobal(pet.rect().center())
        with patch.object(QCursor, 'pos', return_value=point):
            for index in range(3):
                pet.dock_controls.refresh()
                pet.toolbar.sync_visibility(True)
                pet.activity_cards.set_sessions([{'key': 'fixture', 'title': str(index), 'state': 'completed'}])
                pet.activity_cards.set_active(True)
                pet.say('互动台词', 4000)
                pet.task_progress('后台任务', '自动进展', '')
                pet.window_policy().flush()
                self.app.processEvents()
                self.assert_controls_hidden()
                self.assertFalse(pet.bubble.isVisible())
                notification = getattr(pet, 'notification_bubble', None)
                self.assertFalse(notification is not None and notification.isVisible())
                self.assertFalse(pet.dock_controls.visible)

    def test_switching_mode_closes_auxiliary_windows_and_blocks_late_motion_and_pending_notice(self):
        pet = self.pet
        composer, history, quota = self.auxiliary_windows()
        for widget in (composer, history, quota):
            widget.show()
            self.assertTrue(widget.isVisible())
        pet._ui_motion_disabled = False
        motion = composer._ui_motion
        motion.cancel()
        motion.show()
        old_animation = motion.animation
        self.assertIsNotNone(old_animation)
        pet.set_pet_only(True)
        for widget in (composer, history, quota):
            self.assertFalse(widget.isVisible())
            self.assertFalse(widget._ui_motion.intent)
        old_animation.finished.emit()  # A completion already queued before the switch.
        composer.show()
        quota.show()
        callbacks = []
        pet.window_policy().system_popup('notice', lambda remaining: callbacks.append(remaining), 12000)
        pet.window_policy().flush()
        self.app.processEvents()
        self.assertEqual(callbacks, [])
        self.assertFalse(composer.isVisible())
        self.assertFalse(quota.isVisible())
        self.assert_controls_hidden()
        # A previously queued system notice must also disappear at the mode
        # boundary, rather than resurfacing when settings closes afterwards.
        pet._ui_motion_disabled = True
        pet.set_pet_only(False)
        pet.show_settings()
        pet.window_policy().system_popup('notice', lambda remaining: callbacks.append(remaining), 12000)
        self.assertIsNotNone(pet.window_policy().pending)
        pet.set_pet_only(True)
        self.assertIsNone(pet.window_policy().pending)
        pet.settings_panel.accept()
        pet.window_policy().flush()
        self.assertEqual(callbacks, [])

    def test_manual_chat_history_quota_and_settings_work_and_grants_reset_on_mode_switch(self):
        pet = self.pet
        composer, history, quota = self.auxiliary_windows()
        pet.set_pet_only(True)
        policy = pet.window_policy()
        menu = pet.context_menu()
        next(action for action in menu.actions() if action.text() == '聊天').trigger()
        self.assertTrue(composer.isVisible())
        policy.user_open('history')
        history.show()
        self.assertTrue(history.isVisible())
        policy.user_open('quota')
        quota.display([], loading=True)
        self.assertTrue(quota.isVisible())
        pet.show_settings()
        self.assertTrue(pet.settings_panel.isVisible())
        pet.settings_panel.accept()
        pet.set_pet_only(False)
        pet.set_pet_only(True)
        for widget in (composer, history, quota):
            widget.show()
            self.assertFalse(widget.isVisible())
        self.assert_controls_hidden()
        menu.deleteLater()

    def test_leaving_mode_restores_controls_while_manual_fullscreen_and_exit_visibility_win(self):
        pet = self.pet
        for flag in ('manual_hidden', 'fullscreen_hidden'):
            pet.set_pet_only(True)
            setattr(pet, flag, True)
            pet.apply_visibility()
            pet.set_pet_only(False)
            self.assertFalse(pet.isVisible())
            self.assertFalse(pet.toolbar.isVisible())
            self.assertFalse(pet.activity_cards.isVisible())
            setattr(pet, flag, False)
            pet.apply_visibility()
            pet.toolbar.sync_visibility(True)
            pet.activity_cards.set_active(True)
            self.assertTrue(pet.isVisible())
            self.assertTrue(pet.toolbar.isVisible())
            self.assertTrue(pet.activity_cards.isVisible())
        pet.toolbar.set_active(False)
        pet.activity_cards.set_active(False)
        pet.set_pet_only(True)
        pet.set_pet_only(False)
        self.assertFalse(pet.toolbar.isVisible())
        self.assertFalse(pet.activity_cards.isVisible())
        pet.toolbar.set_active(True)
        pet.activity_cards.set_active(True)
        pet.set_pet_only(True)
        pet.exit_reason = 'codex-closed'
        pet.set_pet_only(False)
        self.assertFalse(pet.toolbar.isVisible())
        self.assertFalse(pet.activity_cards.isVisible())


if __name__ == '__main__':
    unittest.main()
