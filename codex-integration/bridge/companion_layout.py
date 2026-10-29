"""Place companion windows in screen space without intersecting the character."""
from __future__ import annotations


def intersects(a, b, gap=0):
    return (a[0] < b[0] + b[2] + gap and a[0] + a[2] + gap > b[0]
            and a[1] < b[1] + b[3] + gap and a[1] + a[3] + gap > b[1])


def place_window(size, owner, area, obstacles=(), preferred=None, gap=8):
    width, height = size
    left, top, aw, ah = area
    right, bottom = left + aw, top + ah
    if width > aw or height > ah:
        return None
    blocked = [owner, *obstacles]
    preferred = preferred or (owner[0] + (owner[2] - width) // 2, owner[1] + owner[3] + gap)
    xs = {left, right - width, preferred[0]}
    ys = {top, bottom - height, preferred[1]}
    for x, y, w, h in blocked:
        xs.update((x - width - gap, x + w + gap, x, x + w - width))
        ys.update((y - height - gap, y + h + gap, y, y + h - height))
    candidates = []
    for x in xs:
        for y in ys:
            rect = (x, y, width, height)
            if x < left or y < top or x + width > right or y + height > bottom:
                continue
            if any(intersects(rect, obstacle, gap // 2) for obstacle in blocked):
                continue
            score = (abs(x - preferred[0]) + abs(y - preferred[1]), abs(y - preferred[1]), x, y)
            candidates.append((score, (x, y)))
    return min(candidates)[1] if candidates else None


def rectangle(widget):
    from ui_motion import geometry
    return geometry(widget)


def companion_owner(pet):
    """Docked pets occupy a small visible head, not their transparent canvas."""
    if getattr(pet, 'dock_edge', None):
        import time
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QRegion
        started = getattr(pet, 'dock_started', 0)
        transitioning = time.monotonic() - started < .3 or bool(getattr(pet, 'dock_exit', None))
        key = (pet.dock_edge, pet.width(), pet.height(), started)
        cached = getattr(pet, '_companion_dock_bounds', None)
        if cached is not None and cached[0] == key and not transitioning:
            rect = cached[1]
        else:
            # The upstream dock_rect is a mouse hotspot; horns and tail can
            # extend beyond it. Measure the painted alpha bounds once settled.
            pixmap = pet.grab()
            mask = QRegion(pixmap.mask()).boundingRect()
            ratio = pixmap.devicePixelRatio()
            rect = QRectF(mask.x() / ratio, mask.y() / ratio,
                          mask.width() / ratio, mask.height() / ratio).toAlignedRect()
            if rect.isEmpty():
                rect = pet.dock_rect().toAlignedRect()
            if not transitioning:
                pet._companion_dock_bounds = (key, rect)
        point = pet.mapToGlobal(rect.topLeft())
        return point.x(), point.y(), rect.width(), rect.height()
    return rectangle(pet)


def place_vertical_window(size, owner, area, obstacles=(), preferred_side='below', gap=8):
    """Toolbar/cards stay centered above or below the complete pet canvas."""
    width, height = size
    sx, sy, sw, sh = area
    if width > sw or height > sh:
        return None
    x = max(sx, min(owner[0] + (owner[2] - width) // 2, sx + sw - width))
    ys = {owner[1] - height - gap, owner[1] + owner[3] + gap, sy, sy + sh - height}
    for ox, oy, ow, oh in obstacles:
        ys.update((oy - height - gap, oy + oh + gap))
    candidates = []
    for y in ys:
        above = y + height <= owner[1] - gap // 2
        below = y >= owner[1] + owner[3] + gap // 2
        if not (above or below) or y < sy or y + height > sy + sh:
            continue
        rect = (x, y, width, height)
        if any(intersects(rect, obstacle, gap // 2) for obstacle in (owner, *obstacles)):
            continue
        side = 'above' if above else 'below'
        distance = owner[1] - y - height if above else y - owner[1] - owner[3]
        candidates.append(((side != preferred_side, distance, y), (x, y)))
    return min(candidates)[1] if candidates else None


def place_side_window(size, owner, area, obstacles=(), gap=8, preferred_side=None,
                      allowed_sides=('left', 'right'), centered=False):
    """Side panels; centered settings clear obstacles horizontally, never diagonally."""
    width, height = size
    sx, sy, sw, sh = area
    if width > sw or height > sh:
        return None
    preferred_y = owner[1] + (owner[3] - height) // 2
    ys = {max(sy, min(preferred_y, sy + sh - height))}
    if not centered:
        ys.update((sy, sy + sh - height, owner[1], owner[1] + owner[3] - height))
        for ox, oy, ow, oh in obstacles:
            ys.update((oy - height - gap, oy + oh + gap))
    left_space = owner[0] - sx
    right_space = sx + sw - owner[0] - owner[2]
    if preferred_side not in ('left', 'right'):
        preferred_side = 'right' if right_space >= left_space else 'left'
    candidates = []
    for side, anchor_x in (('left', owner[0] - width - gap), ('right', owner[0] + owner[2] + gap)):
        if side not in allowed_sides:
            continue
        for y in ys:
            if y < sy or y + height > sy + sh:
                continue
            xs = {anchor_x}
            if centered:
                for ox, oy, ow, oh in obstacles:
                    if y < oy + oh + gap and y + height + gap > oy:
                        xs.add(min(anchor_x, ox - width - gap) if side == 'left'
                               else max(anchor_x, ox + ow + gap))
            for x in xs:
                if x < sx or x + width > sx + sw:
                    continue
                rect = (x, y, width, height)
                if any(intersects(rect, obstacle, gap if centered else gap // 2)
                       for obstacle in (owner, *obstacles)):
                    continue
                candidates.append(((side != preferred_side, abs(y - preferred_y),
                                    abs(x - anchor_x), y), (x, y)))
    return min(candidates)[1] if candidates else None


def vertical_group_layout(owner, area, bubble_size=None, toolbar_size=None, card_size=None, gap=8,
                          task_notification=False, obstacles=()):
    """Task notices use the sides; interaction speech stays next to the pet vertically."""
    result = {'bubble': None, 'toolbar': None, 'activity_cards': None}
    occupied = list(obstacles)
    if bubble_size is not None and not task_notification:
        width, height = bubble_size
        sx, sy, sw, sh = area
        x = max(sx, min(owner[0] + (owner[2] - width) // 2, sx + sw - width))
        for y in (owner[1] - height - gap, owner[1] + owner[3] + gap):
            if width <= sw and sy <= y and y + height <= sy + sh:
                result['bubble'] = (x, y)
                occupied.append((x, y, width, height))
                break
        # When speech is above the pet, keep the task card immediately above
        # that speech instead of allowing the toolbar to displace either one.
        if card_size is not None and result['bubble'] is not None and result['bubble'][1] < owner[1]:
            point = place_vertical_window(card_size, owner, area, occupied, preferred_side='above', gap=gap)
            if point is not None and point[1] < owner[1]:
                result['activity_cards'] = point
                occupied.append((*point, *card_size))
    if toolbar_size is not None:
        point = place_vertical_window(toolbar_size, owner, area, occupied, preferred_side='below', gap=gap)
        result['toolbar'] = point
        if point is not None:
            occupied.append((*point, *toolbar_size))
    if card_size is not None and result['activity_cards'] is None:
        result['activity_cards'] = place_vertical_window(card_size, owner, area, occupied,
                                                       preferred_side='above', gap=gap)
    if bubble_size is not None and task_notification:
        side_owner = speech_side_bounds(owner, result, toolbar_size, card_size)
        result['bubble'] = place_side_window(bubble_size, side_owner, area, gap=gap)
    return result


def speech_side_bounds(owner, positions, toolbar_size=None, card_size=None):
    """Reserve the horizontal overhang of the vertically attached controls."""
    left, right = owner[0], owner[0] + owner[2]
    for name, size in (('toolbar', toolbar_size), ('activity_cards', card_size)):
        point = positions.get(name)
        if point is not None and size is not None:
            left = min(left, point[0])
            right = max(right, point[0] + size[0])
    return left, owner[1], right - left, owner[3]


def is_task_notification(pet):
    """Classify the content, independently of whether a task is currently running."""
    bubble = getattr(pet, 'bubble', None)
    return (not getattr(pet, '_quota_speech_active', False) and
            bool(getattr(bubble, 'is_task_bubble', False) or getattr(bubble, 'is_task_progress', False)))


def layout_companions(pet, toolbar=None, cards=None):
    """Apply the common layout once, independently of widget refresh order."""
    if getattr(pet, '_companion_layout_busy', False):
        return
    from PySide6.QtWidgets import QApplication
    from ui_motion import visible, move_window
    screen = pet.screen() or QApplication.primaryScreen()
    if screen is None:
        return
    toolbar = toolbar if toolbar is not None else getattr(pet, 'toolbar', None)
    cards = cards if cards is not None else getattr(pet, 'activity_cards', None)
    bubble = getattr(pet, 'bubble', None)
    pet._companion_layout_busy = True
    try:
        area = screen.availableGeometry()
        bounds = (area.x(), area.y(), area.width(), area.height())
        owner = companion_owner(pet)
        toolbar_size = (toolbar.width(), toolbar.height()) if toolbar is not None else None
        card_size = ((cards.width(), cards.height()) if cards is not None and
                     (bool(cards.sessions) or visible(cards)) else None)
        task_notification = is_task_notification(pet)
        if visible(bubble):
            fixed_size = getattr(bubble, 'fixed_message_size', None)
            natural_width = round(getattr(bubble, 'base_width', 256) * getattr(bubble, 'scale_percent', 100) / 100)
            room = area.width()
            if task_notification:
                side = owner
                room = max(side[0] - area.left(), area.right() + 1 - side[0] - side[2]) - 8
                if room < min(natural_width, 180) and hasattr(pet, 'ensure_quota_side_space'):
                    pet.ensure_quota_side_space(min(natural_width, 180))
                    owner = companion_owner(pet)
                    side = owner
                    room = max(side[0] - area.left(), area.right() + 1 - side[0] - side[2]) - 8
            if fixed_size is not None:
                bubble.setFixedSize(*fixed_size)
            elif room > 0:
                bubble.setFixedWidth(min(natural_width, room))
            bubble.ensurePolished()
            bubble.layout.invalidate()
            bubble.layout.activate()
            bubble.adjustSize()
            bubble.layout.activate()
            if fixed_size is None and bubble.height() > area.height():
                from PySide6.QtWidgets import QTextBrowser
                for browser in bubble.findChildren(QTextBrowser):
                    browser.setFixedHeight(max(32, browser.height() - (bubble.height() - area.height())))
                bubble.layout.activate()
                bubble.adjustSize()
        bubble_size = (bubble.width(), bubble.height()) if visible(bubble) else None
        points = vertical_group_layout(owner, bounds, bubble_size, toolbar_size, card_size,
                                       task_notification=task_notification)
        chat = getattr(pet, 'chat', None)
        composer = getattr(chat, 'composer', None) if getattr(chat, 'enabled', False) else None
        if composer is not None:
            blocked = [(*points['bubble'], *bubble_size)] if points['bubble'] is not None else []
            point = place_vertical_window((composer.width(), composer.height()), owner, bounds, blocked)
            if point is not None:
                move_window(composer, point, pet, 'composer')
                points = vertical_group_layout(owner, bounds, bubble_size, toolbar_size, card_size,
                                               task_notification=task_notification,
                                               obstacles=[(*point, composer.width(), composer.height())])
        for key, widget in (('bubble', bubble), ('toolbar', toolbar), ('activity_cards', cards)):
            if widget is None:
                continue
            point = points[key]
            if key != 'bubble':
                widget._space_available = point is not None
            elif point is None and bubble_size is not None:
                widget.hide()
            if point is not None:
                move_window(widget, point, pet, {'bubble': 'speech', 'toolbar': 'toolbar', 'activity_cards': 'cards'}[key])
        notification = getattr(pet, 'notification_bubble', None)
        if visible(notification):
            width = round(notification.base_width * notification.scale_percent / 100)
            room = max(owner[0] - bounds[0], bounds[0] + bounds[2] - owner[0] - owner[2]) - 8
            if room > 0:
                notification.setFixedWidth(min(width, room))
            notification.layout.invalidate()
            notification.layout.activate()
            notification.adjustSize()
            if notification.height() > area.height():
                from PySide6.QtWidgets import QTextBrowser
                for browser in notification.findChildren(QTextBrowser):
                    browser.setFixedHeight(max(32, browser.height() - notification.height() + area.height()))
                notification.adjustSize()
            blocked = visible_obstacles(pet, ('bubble', 'toolbar', 'activity_cards', 'composer', 'settings_panel'))
            point = place_side_window((notification.width(), notification.height()), owner, bounds, blocked)
            if point is not None:
                move_window(notification, point, pet, 'notice')
            else:
                notification.hide()
    finally:
        pet._companion_layout_busy = False
    if bubble is not None:
        observe_speech_layout(pet)
    policy = getattr(pet, '_window_policy', None)
    if policy is not None:
        policy.schedule_stack()
    if not getattr(pet, '_companion_aux_layout_busy', False):
        pet._companion_aux_layout_busy = True
        try:
            for name in ('settings_panel', 'quota_panel'):
                panel = getattr(pet, name, None)
                if visible(panel):
                    panel.reposition()
        finally:
            pet._companion_aux_layout_busy = False


def observe_speech_layout(pet, bubble=None):
    """Reflow after actual text/height changes, including the first show."""
    bubble = bubble if bubble is not None else pet.bubble
    if getattr(bubble, '_companion_layout_observer', None) is not None:
        return
    from PySide6.QtCore import QObject, QEvent, QTimer
    class SpeechObserver(QObject):
        def __init__(self):
            super().__init__(pet)
            self.pending = False
            bubble.installEventFilter(self)

        def refresh(self):
            self.pending = False
            if getattr(pet, 'exit_reason', None) is None:
                layout_companions(pet)

        def eventFilter(self, watched, event):
            if not getattr(pet, '_companion_layout_busy', False):
                if event.type() in (QEvent.Type.Show, QEvent.Type.Resize):
                    layout_companions(pet)
                elif event.type() in (QEvent.Type.Hide, QEvent.Type.LayoutRequest):
                    if not self.pending:
                        self.pending = True
                        QTimer.singleShot(0, self.refresh)
            return super().eventFilter(watched, event)
    bubble._companion_layout_observer = SpeechObserver()


def visible_obstacles(pet, names):
    from ui_motion import visible
    result = []
    for name in names:
        widget = (getattr(getattr(pet, 'chat', None), 'composer', None) if name == 'composer'
                  else getattr(pet, name, None))
        if visible(widget):
            result.append(rectangle(widget))
    return result
