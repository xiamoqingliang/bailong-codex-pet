"""Half-speed companion transitions with stable layout targets."""
OPEN_MS = 640
CLOSE_MS = 440
MOVE_MS = 400
RESIZE_MS = 400
CONTENT_MS = 400
DOCK_MS = 600
SWAP_OUT_MS = 320
SWAP_PAUSE_MS = 80
SWAP_IN_MS = 400
CONTROL_ROLES = {'toolbar', 'cards'}
RESIZE_ROLES = {'quota', 'settings'}


def visible(widget):
    if widget is None:
        return False
    motion = getattr(widget, '_ui_motion', None)
    return widget.isVisible() and (motion is None or motion.intent)


def geometry(widget):
    motion = getattr(widget, '_ui_motion', None)
    point = motion.target if motion is not None else widget.pos()
    size = motion.target_size if motion is not None and motion.role in RESIZE_ROLES else widget.size()
    return point.x(), point.y(), size.width(), size.height()


def move_window(widget, point, owner, role):
    attach(widget, owner, role).move_to(point)


def resize_window(widget, size, owner, role):
    attach(widget, owner, role).resize_to(size)


def fade_content(widget, owner):
    """Fade a replacement once; callers keep streamed text updates immediate."""
    if not getattr(owner, '_ui_motion_enabled', False) or getattr(owner, '_ui_motion_disabled', False):
        return None
    from PySide6.QtCore import QPropertyAnimation, QEasingCurve
    from PySide6.QtWidgets import QGraphicsOpacityEffect
    old = getattr(widget, '_ui_content_animation', None)
    if old is not None:
        old.stop()
        old.deleteLater()
    effect = widget.graphicsEffect()
    if not isinstance(effect, QGraphicsOpacityEffect):
        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
    effect.setOpacity(0)
    animation = QPropertyAnimation(effect, b'opacity', widget)
    animation.setDuration(CONTENT_MS)
    animation.setStartValue(0.0)
    animation.setEndValue(1.0)
    animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
    widget._ui_content_animation = animation
    animation.start()
    return animation


class MotionWindowMixin:
    def show(self):
        motion = getattr(self, '_ui_motion', None)
        if motion is None:
            return super().show()
        motion.show()

    def hide(self):
        motion = getattr(self, '_ui_motion', None)
        if motion is None:
            return super().hide()
        motion.hide()

    def accept(self):
        motion = getattr(self, '_ui_motion', None)
        if motion is None:
            return super().accept()
        settings = getattr(motion.owner, 'settings', None)
        if settings is not None:
            settings.sync()
        motion.hide(lambda: super(MotionWindowMixin, self).accept())

    def reject(self):
        motion = getattr(self, '_ui_motion', None)
        if motion is None:
            return super().reject()
        motion.hide(lambda: super(MotionWindowMixin, self).reject())


def attach(widget, owner, role):
    motion = getattr(widget, '_ui_motion', None)
    if motion is not None:
        return motion
    from PySide6.QtCore import (QObject, QPoint, QSize, QRect, QEvent, QTimer, QAbstractAnimation,
                               QPropertyAnimation, QParallelAnimationGroup, QSequentialAnimationGroup, QEasingCurve, Qt)
    from PySide6.QtWidgets import QWidget, QApplication
    import weakref
    import time

    class WindowMotion(QObject):
        def __init__(self):
            super().__init__(widget)
            self.widget, self.owner, self.role = widget, owner, role
            self.target = QPoint(widget.pos())
            self.target_size = QSize(widget.size())
            self.size_animation = None
            self.intent = widget.isVisible()
            self.closing = False
            self.animation = None
            self.kind = None
            self._applying = False
            self._preparing = False
            self._done = None
            self._stamp = self.owner_stamp()
            self._target_side = None
            initial_screen = widget.screen()
            self._destination_screen = initial_screen.name() if initial_screen is not None else None
            self._generation = 0
            self.clock = time.monotonic
            self._dock_until = 0
            self.expiry_at = None
            self.expiry_timer = QTimer(self)
            self.expiry_timer.setSingleShot(True)
            self.expiry_timer.setTimerType(Qt.TimerType.PreciseTimer)
            self.expiry_timer.timeout.connect(self.hide)
            self._transparent = widget.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            widget.installEventFilter(self)
            if getattr(owner, '_ui_motions', None) is None:
                owner._ui_motions = weakref.WeakSet()
            owner._ui_motions.add(self)
            widget.destroyed.connect(lambda: owner._ui_motions.discard(self))

        def owner_stamp(self):
            screen = owner.screen()
            screen_key = ((screen.name(), screen.devicePixelRatio(), screen.availableGeometry().getRect())
                          if screen is not None else None)
            return (owner.x(), owner.y(), owner.width(), owner.height(), getattr(owner, 'dock_edge', None),
                    screen_key)

        def enabled(self):
            enabled = (getattr(owner, '_ui_motion_enabled', False)
                       and not getattr(owner, '_ui_motion_disabled', False)
                       and getattr(owner, 'exit_reason', None) is None)
            return enabled and (self.role == 'history' or (owner.isVisible()
                                and not getattr(owner, 'manual_hidden', False)
                                and not getattr(owner, 'fullscreen_hidden', False)))

        def side(self, point):
            from companion_layout import companion_owner
            x, y, w, h = companion_owner(owner)
            size = self.target_size if self.role in RESIZE_ROLES else widget.size()
            if point.x() + size.width() <= x:
                return 'left'
            if point.x() >= x + w:
                return 'right'
            if point.y() + size.height() <= y:
                return 'above'
            return 'below'

        def home(self, target, side=None):
            point = QPoint(target)
            if self.role == 'history':
                return point
            side = side or self.side(target)
            axis = widget.width() if side in ('left', 'right') else widget.height()
            distance = min(128, max(24, round(axis * .85)))
            delta = {'left': (distance, 0), 'right': (-distance, 0),
                     'above': (0, distance), 'below': (0, -distance)}[side]
            point += QPoint(*delta)
            screen = owner.screen()
            if screen is not None:
                area = screen.availableGeometry()
                point.setX(max(area.left(), min(point.x(), area.right() + 1 - widget.width())))
                point.setY(max(area.top(), min(point.y(), area.bottom() + 1 - widget.height())))
            return point

        def active(self):
            return self.animation is not None and self.animation.state() == QAbstractAnimation.State.Running

        def remaining(self):
            return max(0, self.animation.duration() - self.animation.currentTime()) if self.active() else 0

        def stop(self):
            self._generation += 1
            if self.animation is not None:
                old, self.animation = self.animation, None
                old.stop()
                old.deleteLater()
            self._done = None

        def apply_position(self, point):
            self._applying = True
            try:
                QWidget.move(widget, point)
            finally:
                self._applying = False

        def raw_hide(self, callback=None):
            self.stop_size()
            self.expiry_timer.stop()
            self.expiry_at = None
            self._applying = True
            try:
                if callback is not None:
                    callback()
                else:
                    QWidget.hide(widget)
            finally:
                self._applying = False
            widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, self._transparent)
            widget.setWindowOpacity(1)

        def tween(self, group, point, opacity, duration, curve):
            parallel = QParallelAnimationGroup()
            if self.role != 'history':
                position = QPropertyAnimation(widget, b'pos')
                position.setDuration(duration)
                position.setStartValue(QPoint(widget.pos()))
                position.setEndValue(QPoint(point))
                position.setEasingCurve(curve)
                parallel.addAnimation(position)
            if opacity is not None:
                alpha = QPropertyAnimation(widget, b'windowOpacity')
                alpha.setDuration(duration)
                alpha.setStartValue(widget.windowOpacity())
                alpha.setEndValue(opacity)
                alpha.setEasingCurve(curve)
                parallel.addAnimation(alpha)
            group.addAnimation(parallel)
            return parallel

        def start(self, group, kind, callback=None):
            self.animation, self.kind = group, kind
            group.setParent(self)
            generation = self._generation
            self._done = callback
            def finished():
                if generation != self._generation:
                    return
                self.animation = None
                self._done = None
                if callback is not None:
                    callback()
                elif self.intent:
                    self.apply_position(self.target)
                    widget.setWindowOpacity(1)
                    widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, self._transparent)
                group.deleteLater()
            group.finished.connect(finished)
            group.start()

        def wait_for_close(self):
            pairs = ({'settings', 'composer'}, {'settings', 'quota'}, {'settings', 'notice'}, {'notice', 'quota'})
            return max((other.remaining() for other in owner._ui_motions if other is not self and other.closing
                        and {self.role, other.role} in pairs), default=0)

        def show(self):
            if self.intent and widget.isVisible() and not self.closing:
                return
            was_visible = widget.isVisible()
            self.stop()
            self.intent, self.closing = True, False
            if self.role in RESIZE_ROLES and self.target_size != widget.size():
                self.resize_to(self.target_size)
            self._preparing = True
            if self.role not in CONTROL_ROLES and self.enabled() and not was_visible:
                widget.setWindowOpacity(0)
            self._applying = True
            try:
                QWidget.show(widget)
            finally:
                self._applying = False
                self._preparing = False
            if self.role == 'history':
                self.target = QPoint(widget.pos())
            self._stamp = self.owner_stamp()
            if not self.enabled():
                self.apply_position(self.target)
                widget.setWindowOpacity(1)
                return
            if not was_visible:
                self.apply_position(self.home(self.target))
            group = QSequentialAnimationGroup()
            wait = self.wait_for_close()
            if wait:
                group.addPause(wait)
            opacity = None if self.role in CONTROL_ROLES and widget.windowOpacity() >= .999 else 1.0
            self.tween(group, self.target, opacity, OPEN_MS, QEasingCurve.Type.OutCubic)
            if self.role != 'history':
                widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            self.start(group, 'open')

        def hide(self, callback=None):
            if self.role in CONTROL_ROLES:
                self.intent, self.closing = False, False
                self.stop()
                return self.raw_hide(callback)
            if self.closing:
                if self.expiry_at is not None and self.clock() >= self.expiry_at:
                    self.finish()
                return
            self.intent = False
            self.stop()
            self.stop_size()
            if not widget.isVisible() or not self.enabled():
                self.closing = False
                return self.raw_hide(callback)
            self.closing = True
            self.expiry_timer.stop()
            widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            group = QSequentialAnimationGroup()
            remaining = (max(0, int((self.expiry_at - self.clock()) * 1000))
                         if self.expiry_at is not None else CLOSE_MS)
            self.tween(group, self.home(self.target), 0.0, min(CLOSE_MS, remaining), QEasingCurve.Type.InCubic)
            def finished():
                self.closing = False
                self.raw_hide(callback)
            self.start(group, 'close', finished)

        def arm_expiry(self, milliseconds):
            self.expiry_timer.stop()
            self.expiry_at = self.clock() + milliseconds / 1000 if milliseconds else None
            if milliseconds and self.enabled():
                # Retraction is inside the original display lease, not added
                # after it. The existing owner timer remains authoritative.
                self.expiry_timer.start(max(0, milliseconds - CLOSE_MS))

        def retract_for_fade(self):
            if not self.enabled() or not widget.isVisible() or self.closing:
                return
            self.stop()
            self.intent, self.closing = False, True
            group = QSequentialAnimationGroup()
            self.tween(group, self.home(self.target), None, CLOSE_MS, QEasingCurve.Type.InCubic)
            self.start(group, 'fade-close', lambda: None)

        def stop_size(self):
            if self.size_animation is not None:
                old, self.size_animation = self.size_animation, None
                old.stop()
                old.deleteLater()

        def resize_to(self, value):
            size = QSize(*value) if isinstance(value, (tuple, list)) else QSize(value)
            size = size.expandedTo(widget.minimumSize()).boundedTo(widget.maximumSize())
            if size == self.target_size and self.size_animation is not None:
                return
            self.target_size = size
            self.stop_size()
            if self.closing:
                return
            if widget.size() == size:
                return
            if (self.role not in RESIZE_ROLES or not self.enabled() or not self.intent
                    or not widget.isVisible() or self.closing):
                self._applying = True
                try:
                    QWidget.resize(widget, size)
                finally:
                    self._applying = False
                return
            animation = QPropertyAnimation(widget, b'size', self)
            animation.setDuration(RESIZE_MS)
            animation.setStartValue(QSize(widget.size()))
            animation.setEndValue(size)
            animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
            self.size_animation = animation
            def finished():
                if self.size_animation is animation:
                    self.size_animation = None
                animation.deleteLater()
            animation.finished.connect(finished)
            animation.start()

        def move_to(self, value):
            point = QPoint(*value) if isinstance(value, (tuple, list)) else QPoint(value)
            old = QPoint(self.target)
            stamp = self.owner_stamp()
            previous = self._stamp
            target_side = self.side(point)
            source_side = self._target_side or self.side(old)
            swapping = self.active() and self.kind == 'swap'
            source = old
            if swapping and self.animation.currentTime() < SWAP_OUT_MS + SWAP_PAUSE_MS:
                source = self._swap_source
                source_side = self._swap_source_side
            opposite = {source_side, target_side} in ({'left', 'right'}, {'above', 'below'})
            dragging = bool(getattr(owner, 'dragging', False) or getattr(getattr(owner, 'toolbar', None), '_dragging', False))
            if dragging:
                self._dock_until = 0
            elif stamp[4] != previous[4]:
                self._dock_until = self.clock() + DOCK_MS / 1000
            docking = self.clock() < self._dock_until
            screen_changed = stamp[5] != previous[5]
            following = dragging or (stamp[:2] != previous[:2] and not docking)
            destination_screen = QApplication.screenAt(QRect(point, widget.size()).center())
            destination_key = destination_screen.name() if destination_screen is not None else None
            screen_changed = screen_changed or (destination_key is not None and destination_key != self._destination_screen)
            self._destination_screen = destination_key
            self._stamp = stamp
            self.target = point
            old_target_side, self._target_side = self._target_side, target_side
            if self._preparing:
                return self.apply_position(point)
            if self.closing:
                if following or screen_changed:
                    self.finish()
                return
            # Dragging follows immediately on the same side, but must not
            # bypass an actual side change or interrupt its shared timeline.
            continuing_swap = swapping and target_side == old_target_side and not docking
            if not widget.isVisible() or not self.enabled() or screen_changed or (following and not opposite and not swapping):
                self.stop()
                self.apply_position(point)
                if self.intent:
                    if screen_changed and self.enabled():
                        widget.setWindowOpacity(0)
                        group = QSequentialAnimationGroup()
                        self.tween(group, point, 1.0, OPEN_MS, QEasingCurve.Type.OutCubic)
                        self.start(group, 'screen')
                    else:
                        widget.setWindowOpacity(1)
                        widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, self._transparent)
                return
            if continuing_swap:
                delta = QPoint(stamp[0] - previous[0], stamp[1] - previous[1])
                if not delta.isNull():
                    self._swap_source += delta
                    outgoing = self._swap_out_position
                    outgoing.setStartValue(outgoing.startValue() + delta)
                    outgoing.setEndValue(outgoing.endValue() + delta)
                home = self.home(point, target_side)
                self._swap_jump.setStartValue(home)
                self._swap_jump.setEndValue(home)
                self._swap_in_position.setStartValue(home)
                self._swap_in_position.setEndValue(point)
                return
            if point == old and self.active():
                return
            if widget.pos() == point:
                return
            self.stop()
            group = QSequentialAnimationGroup()
            if opposite and not docking:
                self._swap_source = QPoint(source)
                self._swap_source_side = source_side
                widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
                outgoing = self.tween(group, self.home(source, source_side), 0.0, SWAP_OUT_MS, QEasingCurve.Type.InCubic)
                self._swap_out_position = outgoing.animationAt(0)
                group.addPause(SWAP_PAUSE_MS)
                jump = QPropertyAnimation(widget, b'pos')
                jump.setDuration(0)
                jump.setStartValue(self.home(point))
                jump.setEndValue(self.home(point))
                group.addAnimation(jump)
                self._swap_jump = jump
                phase = self.tween(group, point, 1.0, SWAP_IN_MS, QEasingCurve.Type.OutCubic)
                self._swap_in_position = phase.animationAt(0)
                phase.animationAt(0).setStartValue(self.home(point))
                phase.animationAt(1).setStartValue(0.0)
                self.start(group, 'swap')
            else:
                opacity = None if widget.windowOpacity() >= .999 else 1.0
                duration = max(1, round((self._dock_until - self.clock()) * 1000)) if docking else MOVE_MS
                self.tween(group, point, opacity, duration, QEasingCurve.Type.InOutCubic)
                self.start(group, 'dock' if docking else 'move')

        def finish(self):
            if self.active():
                self.animation.setCurrentTime(self.animation.duration())
            if self.size_animation is not None:
                self.size_animation.setCurrentTime(self.size_animation.duration())

        def cancel(self):
            self.stop()
            self.stop_size()
            self.intent, self.closing = False, False
            self.raw_hide()

        def eventFilter(self, watched, event):
            if not self._applying:
                if event.type() == QEvent.Type.Hide:
                    self.stop()
                    self.stop_size()
                    self.expiry_timer.stop()
                    self.expiry_at = None
                    self.intent, self.closing = False, False
                elif event.type() == QEvent.Type.Show:
                    self.intent = True
                elif event.type() == QEvent.Type.Resize and self.size_animation is None:
                    self.target_size = QSize(widget.size())
                elif event.type() == QEvent.Type.Move and (self.role == 'history' or not self.active()) and not self.closing:
                    self.target = QPoint(widget.pos())
            return super().eventFilter(watched, event)

    motion = WindowMotion()
    widget._ui_motion = motion
    return motion
