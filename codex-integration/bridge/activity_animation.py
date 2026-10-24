"""Metadata-only animation adapter for the original Bailong engine.

No Qt, network, conversation, quota or task-controller APIs are imported here.
The original task_active/generation fields are never written. All external
ownership stays in this object and is surrendered when a user/original-chat
action replaces the animation.
"""
from __future__ import annotations

from collections import deque
import time


_STATES = {'running', 'needs_input', 'completed', 'failed', 'idle', 'unknown'}
_PRIORITY = {'needs_input': 0, 'failed': 1, 'completed': 2, 'running': 3, 'unknown': 4, 'idle': 5}
_SOFT_MODES = {'idle', 'working', 'work-start'}
_COMPLETION_MAX_AGE = 60.0


class ExternalActivityDriver:
    """Drive safe animation transitions from key/state/turn_id metadata only.

    ``update`` never replays an unchanged animation. ``on_idle`` is intended
    for a LinkedPet.idle override: return immediately when it returns True,
    otherwise call super().idle(). Missing turn IDs use an observed-cycle token
    rather than treating all future turns of one chat as the same completion.
    """

    def __init__(self, original, pet, is_popup_open=None):
        self.original = original
        self.pet = pet
        self.is_popup_open = is_popup_open
        self.state = 'idle'
        self.last_action = None
        self.last_error = None
        self._active = False
        self._available = False
        self._now = time.monotonic()
        self._records = {}
        self._cycle = 0
        self._running_seen = set()
        self._running_started = set()
        self._failed_shown = set()
        self._completed_seen = set()
        self._completed_order = deque()
        self._pending_completions = deque()
        self._work_index = 0
        self._owned_stamp = None
        self._owned_action = None
        self._owned_token = None
        self._restoring = False

    @property
    def owning(self):
        return self._owns_current()

    def _stamp(self):
        item = getattr(self.pet, 'current', None)
        item_id = item.get('id') if isinstance(item, dict) else None
        return (getattr(self.pet, 'mode', None), item_id, getattr(self.pet, 'started', None))

    def _owns_current(self):
        return self._owned_stamp is not None and self._owned_stamp == self._stamp()

    def _forget_ownership(self):
        self._owned_stamp = None
        self._owned_action = None
        self._owned_token = None

    def _original_busy(self):
        chat = getattr(self.pet, 'chat', None)
        return bool(getattr(chat, 'busy', False) or getattr(self.pet, 'task_active', False))

    def _hard_blocked(self):
        if self._original_busy() or getattr(self.pet, 'dragging', False) or getattr(self.pet, 'quota_inflight', False):
            return True
        toolbar = getattr(self.pet, 'toolbar', None)
        if getattr(toolbar, '_dragging', False):
            return True
        if getattr(self.pet, 'manual_hidden', False) or getattr(self.pet, 'fullscreen_hidden', False):
            return True
        visible = getattr(self.pet, 'isVisible', None)
        if callable(visible) and not visible():
            return True
        click_timer = getattr(self.pet, 'click_timer', None)
        if click_timer is not None and click_timer.isActive():
            return True
        # Quota requests clear quota_inflight before their 12-second result
        # bubble is shown. Preserve that display lease (and timed original
        # speech/results) through both polling and natural idle boundaries.
        bubble_timer = getattr(self.pet, 'bubble_timer', None)
        bubble = getattr(self.pet, 'bubble', None)
        if (bubble_timer is not None and bubble_timer.isActive()
                and bubble is not None and bubble.isVisible()):
            return True
        if self.is_popup_open is not None and self.is_popup_open():
            return True
        for menu in (getattr(toolbar, '_action_menu', None), getattr(toolbar, '_menu', None)):
            if menu is not None and menu.isVisible():
                return True
        return False

    def _can_switch(self, idle_boundary=False):
        if self._hard_blocked():
            return False
        if idle_boundary:
            return True
        mode = getattr(self.pet, 'mode', 'idle')
        return mode == 'idle' or (self._owns_current() and mode in _SOFT_MODES)

    def _release(self):
        owned = self._owns_current()
        self._forget_ownership()
        if owned and not self._hard_blocked() and not self._restoring:
            self._restoring = True
            try:
                # Calls the host idle override, which sees no external
                # ownership/active action and can hand back to the original.
                self.pet.idle()
            finally:
                self._restoring = False

    def _clear_observations(self):
        self._records.clear()
        self._running_seen.clear()
        self._running_started.clear()
        self._failed_shown.clear()
        self._completed_seen.clear()
        self._completed_order.clear()
        self._pending_completions.clear()

    def _new_token(self, key):
        self._cycle += 1
        return (key, 'observed', self._cycle)

    def _read_records(self, sessions):
        records = {}
        for value in sessions:
            if not isinstance(value, dict):
                continue
            key, state = value.get('key'), value.get('state')
            if not isinstance(key, str) or not key or not isinstance(state, str) or state not in _STATES:
                continue
            turn = value.get('turn_id')
            old = self._records.get(key)
            if isinstance(turn, str) and turn:
                token = (key, 'turn', turn)
            elif old is not None and not (
                    state == 'running' and old['state'] in ('completed', 'idle', 'unknown')):
                token = old['token']
            else:
                token = self._new_token(key)
            # Intentionally do not retain titles, project paths, messages,
            # tool fields, results or any other input payload.
            records[key] = {'key': key, 'state': state, 'token': token}
        return records

    def _remember_completion(self, token):
        if token in self._completed_seen:
            return
        self._completed_seen.add(token)
        self._completed_order.append(token)
        self._pending_completions.append((token, self._now))
        while len(self._completed_order) > 512:
            self._completed_seen.discard(self._completed_order.popleft())

    def update(self, sessions, active=True, available=True, now=None):
        self._now = time.monotonic() if now is None else float(now)
        self._active = bool(active)
        self._available = bool(available) and isinstance(sessions, (list, tuple))
        if not self._owns_current():
            self._forget_ownership()
        if not self._active or not self._available:
            self.state = 'idle' if not self._active else 'unknown'
            self._clear_observations()
            self._release()
            return self.state

        records = self._read_records(sessions)
        for record in records.values():
            if record['state'] == 'running':
                self._running_seen.add(record['token'])
            elif record['state'] == 'completed' and record['token'] in self._running_seen:
                self._remember_completion(record['token'])
        self._records = records
        live_tokens = {record['token'] for record in records.values()}
        pending_tokens = {token for token, _ in self._pending_completions}
        self._running_seen.intersection_update(live_tokens | pending_tokens)
        self._running_started.intersection_update(live_tokens)
        self._failed_shown.intersection_update(live_tokens)
        self._pending_completions = deque(
            (token, when) for token, when in self._pending_completions
            if 0 <= self._now - when <= _COMPLETION_MAX_AGE)
        self.state = min((record['state'] for record in records.values()),
                         key=lambda state: _PRIORITY[state], default='idle')
        if self._can_switch():
            target = self._target()
            if target is not None:
                self._dispatch(target, idle_boundary=False)
            elif self._owned_action not in ('completed', 'failed'):
                self._release()
        return self.state

    def _find_item(self, wanted):
        def search(value):
            if isinstance(value, dict):
                if value.get('id') == wanted and ('file' in value or 'playbackFile' in value):
                    return value
                for child in value.values():
                    result = search(child)
                    if result is not None:
                        return result
            elif isinstance(value, (list, tuple)):
                for child in value:
                    result = search(child)
                    if result is not None:
                        return result
            return None
        return search(getattr(self.pet, 'manifest', {}))

    def _regular_work(self):
        method = getattr(self.pet, 'regular_work_animations', None)
        values = method() if callable(method) else []
        values = [item for item in values if isinstance(item, dict)
                  and ('file' in item or 'playbackFile' in item)]
        if not values:
            values = [self._find_item(key) for key in ('work02', 'work03', 'work04', 'work05')]
            values = [item for item in values if item is not None]
        if not values:
            return None
        item = values[self._work_index % len(values)]
        self._work_index += 1
        return item

    def _target(self):
        records = list(self._records.values())
        def stable_first(state):
            candidates = [item for item in records if item['state'] == state]
            if self._owns_current() and self._owned_action == state:
                owned = next((item for item in candidates if item['token'] == self._owned_token), None)
                if owned is not None:
                    return owned
            return candidates[0] if candidates else None
        waiting = stable_first('needs_input')
        if waiting is not None:
            return ('needs_input', waiting['token'])
        failed = [item for item in records if item['state'] == 'failed']
        if failed:
            unseen = next((item for item in failed if item['token'] not in self._failed_shown), None)
            return ('failed', unseen['token']) if unseen is not None else None
        if self._pending_completions:
            return ('completed', self._pending_completions[0][0])
        running = stable_first('running')
        return ('running', running['token']) if running is not None else None

    def _play(self, item, mode, state, token):
        if item is None:
            return False
        try:
            self.pet.play(item, mode)
        except Exception as error:
            self.last_error = type(error).__name__
            self._forget_ownership()
            return False
        self._owned_stamp = self._stamp()
        self._owned_action = state
        self._owned_token = token
        self.last_action = item.get('id')
        self.last_error = None
        return True

    def _dispatch(self, target, idle_boundary):
        state, token = target
        if (not idle_boundary and self._owns_current() and self._owned_action == state
                and getattr(self, '_owned_token', None) == token):
            return False
        if state == 'running':
            starting = token not in self._running_started
            item = self._find_item('work01') if starting else self._regular_work()
            if item is None and starting:
                item = self._regular_work()
            mode = 'work-start' if starting and item and item.get('id') == 'work01' else 'working'
            if self._play(item, mode, state, token):
                self._running_started.add(token)
                return True
        elif state == 'needs_input':
            item = self._find_item('work05') or self._find_item('quota')
            return self._play(item, 'working', state, token)
        elif state == 'failed':
            if token not in self._failed_shown and self._play(self._find_item('13'), 'variant', state, token):
                self._failed_shown.add(token)
                return True
        elif state == 'completed':
            if self._play(self._find_item('18'), 'task-celebration', state, token):
                self._pending_completions = deque(
                    (value, when) for value, when in self._pending_completions if value != token)
                return True
        return False

    def on_idle(self):
        """Accept control only at the original engine's natural idle boundary."""
        if self._restoring or not self._active or not self._available:
            return False
        if not self._owns_current():
            self._forget_ownership()
        if not self._can_switch(idle_boundary=True):
            return False
        target = self._target()
        if target is not None and self._dispatch(target, idle_boundary=True):
            return True
        self._forget_ownership()
        return False
