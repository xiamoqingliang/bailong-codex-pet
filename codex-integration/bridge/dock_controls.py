"""Shared dock hover policy. Task changes never reveal the panels."""
import time


class HoverLease:
    def __init__(self):
        self.edge = None
        self.revealed = False
        self.last_inside = 0.0

    def update(self, edge, over_pet, over_controls, interacting, enabled=True, now=None):
        now = time.monotonic() if now is None else now
        if edge != self.edge or not enabled:
            self.revealed = False
            self.edge = edge
        if not enabled:
            return False
        if not edge:
            return True
        if over_pet or interacting or self.revealed and over_controls:
            self.revealed = True
            self.last_inside = now
        elif self.revealed and now - self.last_inside >= 0.35:
            self.revealed = False
        return self.revealed


def create_dock_controls(pet):
    from PySide6.QtCore import QObject, QTimer
    from PySide6.QtGui import QCursor
    from PySide6.QtWidgets import QApplication

    class DockControls(QObject):
        def __init__(self):
            super().__init__(pet)
            self.lease = HoverLease()
            self.visible = not bool(pet.dock_edge)
            self.timer = QTimer(self)
            self.timer.setInterval(60)
            self.timer.timeout.connect(self.refresh)
            self.timer.start()

        def interacting(self):
            toolbar = getattr(pet, 'toolbar', None)
            settings = getattr(pet, 'settings_panel', None)
            return bool(QApplication.activePopupWidget()
                        or toolbar and toolbar._dragging
                        or settings and settings.isVisible())

        def refresh(self):
            enabled = (pet.isVisible() and not pet.manual_hidden and not pet.fullscreen_hidden
                       and getattr(pet, 'exit_reason', None) is None)
            cursor = QCursor.pos()
            rect = pet.dock_rect().toAlignedRect() if pet.dock_edge else pet.rect()
            over_pet = rect.contains(pet.mapFromGlobal(cursor))
            over_controls = (self.lease.revealed and pet.rect().contains(pet.mapFromGlobal(cursor)))
            windows = [getattr(pet, 'toolbar', None), getattr(pet, 'activity_cards', None),
                       getattr(getattr(pet, 'chat', None), 'composer', None)]
            for window in windows:
                if window is not None and window.isVisible():
                    over_controls |= window.frameGeometry().adjusted(-8, -8, 8, 8).contains(cursor)
            visible = self.lease.update(pet.dock_edge, over_pet, over_controls,
                                        self.interacting(), enabled)
            changed = visible != self.visible
            self.visible = visible
            for window in windows[:2]:
                if window is not None:
                    window.sync_visibility()
            if enabled and pet.dock_edge and any(window is not None and window.isVisible() for window in (
                    getattr(pet, 'bubble', None), getattr(pet, 'notification_bubble', None),
                    getattr(pet, 'quota_panel', None), getattr(pet, 'settings_panel', None))):
                from companion_layout import layout_companions
                layout_companions(pet)
            return changed

    return DockControls()
