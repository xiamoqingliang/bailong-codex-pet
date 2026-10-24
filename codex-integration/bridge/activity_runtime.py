"""Non-blocking local task metadata feed for the companion UI.

The only persisted diagnostics are counts and availability. Task titles/routes
stay in memory, and no chat transcript or tool payload is read here.
"""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
from pathlib import Path
import subprocess
import time
from urllib.parse import urlparse, unquote

from activity_source import snapshot

BASE = Path(__file__).resolve().parent
POLL_SECONDS = 2.0
STALE_SECONDS = 8.0
TERMINAL_SECONDS = 60.0


def apply_ui_hints(sessions, hints):
    """Only supplement an in-progress task whose title uniquely identifies it."""
    counts = Counter(item.get('title') for item in sessions)
    by_title = {item.get('title'): item.get('state') for item in hints
                if isinstance(item, dict) and item.get('state') in ('running', 'needs_input', 'failed')}
    result = []
    for item in sessions:
        updated = dict(item)
        title = item.get('title')
        if item.get('state') == 'running' and counts[title] == 1 and title in by_title:
            updated['state'] = by_title[title]
            updated['stateEvidence'] = 'native-ui-label'
        result.append(updated)
    return result


class ActivityReducer:
    """Keep active tasks and bounded new completion/failure notifications."""
    def __init__(self):
        self.previous = {}
        self.terminal_until = {}
        self.cards = []

    def accept(self, sessions, now):
        cards = []
        current = {}
        for item in sessions:
            key = item.get('key')
            if not key:
                continue
            token = (key, item.get('turn_id'))
            state = item.get('state')
            previous = self.previous.get(token)
            if state in ('completed', 'failed') and previous in ('running', 'needs_input'):
                self.terminal_until[token] = now + TERMINAL_SECONDS
            current[token] = state
            show = state in ('running', 'needs_input')
            if state in ('completed', 'failed'):
                show = bool(item.get('initial_visible') or self.terminal_until.get(token, 0) > now)
            if show:
                cards.append(dict(item))
        self.previous = current
        self.terminal_until = {token: deadline for token, deadline in self.terminal_until.items()
                               if token in current and deadline > now}
        self.cards = cards
        return cards

    def unknown(self):
        return [dict(item, state='unknown') for item in self.cards]


class ActivityRuntime:
    def __init__(self, original, pet, cards, animation, opener=None):
        self.original = original
        self.pet = pet
        self.cards = cards
        self.animation = animation
        self.opener = opener
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='bailong-activity')
        self.future = None
        self.reducer = ActivityReducer()
        self.routes = {}
        self.sessions = []
        self.animation_sessions = []
        self.last_good_at = None
        self.next_poll = 0.0
        self.active = False
        self.context = None
        self.epoch = 0
        self.available = False
        self.reason = 'not-started'
        self.truncated = False
        self.ui_hint_count = 0
        self._ui_cache = []
        self._ui_at = 0.0
        self._ui_context = None
        self.closed = False
        self.last_open_succeeded = None
        self.last_open_at = None

    def _collect(self, process_id, app_started_at):
        result = snapshot(app_started_at=app_started_at)
        result['uiHintCount'] = 0
        if not result.get('available'):
            return result
        sessions = result.get('sessions', [])
        title_counts = Counter(item.get('title') for item in sessions)
        titles = sorted({item['title'] for item in sessions if item.get('state') == 'running'
                         and item.get('title') != '未命名任务' and title_counts[item.get('title')] == 1})
        context = (process_id, tuple(titles))
        now = time.monotonic()
        if titles and (context != self._ui_context or now - self._ui_at >= 4):
            self._ui_context, self._ui_at, self._ui_cache = context, now, []
            powershell = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
            try:
                completed = subprocess.run(
                    [str(powershell), '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                     str(BASE / 'activity_ui_hints.ps1'), '-TargetProcessId', str(process_id)],
                    input=json.dumps({'titles': titles}, ensure_ascii=True).encode('ascii'),
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=2.5,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                if completed.returncode == 0 and len(completed.stdout) <= 65536:
                    payload = json.loads(completed.stdout.decode('utf-8-sig'))
                    if payload.get('available') and isinstance(payload.get('hints'), list):
                        self._ui_cache = payload['hints']
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        elif not titles:
            self._ui_cache = []
        result['sessions'] = apply_ui_hints(sessions, self._ui_cache)
        result['uiHintCount'] = len(self._ui_cache)
        return result

    def poll(self, active, state):
        if self.closed:
            return
        now = time.monotonic()
        context = (state.get('pid'), state.get('appStartedAt')) if state else None
        if bool(active) != self.active or (active and context != self.context):
            self.active = bool(active)
            self.context = context
            self.epoch += 1
            self.reducer = ActivityReducer()
            self.sessions = []
            self.animation_sessions = []
            self.routes = {}
            self.available = False
            self.last_good_at = None
            self.next_poll = 0
            self.cards.set_sessions([])
            self.animation.update([], active=False, available=False)
        if self.future is not None and self.future[0].done():
            future, epoch = self.future
            self.future = None
            if active and epoch == self.epoch:
                try:
                    data = future.result()
                except Exception:
                    data = {'available': False, 'reason': 'metadata-reader-error'}
                if data.get('available'):
                    self.last_good_at = now
                    self.available = True
                    self.reason = None
                    self.truncated = bool(data.get('truncated'))
                    self.ui_hint_count = data.get('uiHintCount', 0)
                    self.sessions = data.get('sessions', [])
                    visible = self.reducer.accept(self.sessions, now)
                    self.animation_sessions = visible
                    self.routes = {item['key']: item.get('routeUri') for item in visible}
                    self.cards.set_sessions(visible)
                    self.animation.update(self.animation_sessions, active=self.pet.isVisible(), available=True)
                else:
                    self.reason = data.get('reason', 'metadata-unavailable')
        if active and self.future is None and now >= self.next_poll and context and context[0]:
            self.future = (self.executor.submit(self._collect, *context), self.epoch)
            self.next_poll = now + POLL_SECONDS
        if active and (self.last_good_at is None or now - self.last_good_at > STALE_SECONDS):
            self.available = False
            self.cards.set_sessions(self.reducer.unknown())
            self.animation.update([], active=self.pet.isVisible(), available=False)
        elif active and self.available:
            self.animation.update(self.animation_sessions, active=self.pet.isVisible(), available=True)
        self.cards.set_active(bool(active))
        self.cards.sync_visibility()

    def activate(self, key):
        if not self.active or not self.available:
            return False
        uri = self.routes.get(key)
        if not isinstance(uri, str):
            return False
        parsed = urlparse(uri)
        identifier = unquote(parsed.path.removeprefix('/'))
        if (parsed.scheme != 'codex' or parsed.netloc != 'threads' or parsed.query or parsed.fragment
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,199}', identifier)):
            return False
        if self.opener:
            result = bool(self.opener(uri))
        else:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            result = QDesktopServices.openUrl(QUrl(uri))
        self.last_open_succeeded = bool(result)
        self.last_open_at = time.time()
        return bool(result)

    def diagnostics(self):
        return {'activitySource': 'local-thread-index', 'activityAvailable': self.available,
                'activityReason': self.reason, 'activityCount': len(self.cards.sessions),
                'activityStates': dict(Counter(item['state'] for item in self.cards.sessions)),
                'activitySourceTruncated': self.truncated, 'activityUiHintCount': self.ui_hint_count,
                'activityLastOpenSucceeded': self.last_open_succeeded, 'activityLastOpenAt': self.last_open_at,
                'activityWaitingCoverage': 'available-native-ui-labels'}

    def close(self):
        self.closed = True
        self.cards.set_active(False)
        self.animation.update([], active=False, available=False)
        self.executor.shutdown(wait=False, cancel_futures=True)
