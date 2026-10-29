"""Mirror bound Codex conversations, preserving user/assistant chronology.

Public messages and command/file-change metadata form one timeline. Tool
output and diffs are read on demand. This reader does not open rollout files,
execute tools, send prompts, or change any Codex database.
"""
from __future__ import annotations
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
import hashlib
import html
import importlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
from file_diff import file_placeholder, create_file_views

IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z')
UUID = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
ACTIVE = {'inProgress', 'running', 'pending', 'waitingForApproval', 'waitingForInput'}
ITEM_TYPES = "('userMessage','agentMessage','reasoning','commandExecution','fileChange')"
MAX_DETAIL_CHARS = 1024 * 1024


def code_excerpt(value, max_lines=8, max_chars=800):
    value = text(value)
    lines = value.splitlines()
    excerpt = '\n'.join(lines[:max_lines])[:max_chars]
    return excerpt, len(lines) > max_lines or len(excerpt) < len(value.rstrip('\r\n'))


def output_excerpt(value):
    value = text(value)
    lines = value.splitlines()
    if len(lines) <= 20 and len(value) <= 2000:
        return value, False
    start = '\n'.join(lines[:12])[:1000]
    end = '\n'.join(lines[-8:])[-1000:]
    return start + '\n…\n' + end, True


def tool_token(thread_id, item_id):
    return hashlib.sha256((thread_id + '\0' + item_id).encode('utf-8')).hexdigest()[:32]


def timeline_groups(messages):
    groups = []
    for message in messages:
        if (message.get('role') == 'tool' and groups
                and groups[-1][0].get('role') == 'tool'
                and groups[-1][0].get('toolType') == message.get('toolType')):
            groups[-1].append(message)
        else:
            groups.append([message])
    return groups


def tool_status(message):
    status = message.get('status')
    if message.get('toolType') == 'commandExecution' and message.get('exitCode') not in (None, 0):
        return '执行失败'
    return {'inProgress': '执行中', 'running': '执行中', 'completed': '已完成',
            'failed': '执行失败', 'declined': '已拒绝', 'interrupted': '已中断',
            'waitingForApproval': '等待确认', 'waitingForInput': '等待输入'}.get(status, '状态待更新')


def valid_id(value):
    return isinstance(value, str) and bool(IDENTIFIER.fullmatch(value))


def text(value):
    return value if isinstance(value, str) else ''


def timestamp(value, milliseconds=False):
    if not isinstance(value, (int, float)):
        return ''
    try:
        return datetime.fromtimestamp(value / 1000 if milliseconds else value).isoformat(timespec='seconds')
    except (ValueError, OverflowError, OSError):
        return ''


def open_readonly(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=0.15)
    connection.execute('PRAGMA query_only=ON')
    connection.row_factory = sqlite3.Row
    connection.execute('BEGIN')
    return connection


class ConversationReader:
    def __init__(self, home=None):
        self.home = Path(home or os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        self.cache = {}

    def read(self, thread_ids):
        requested = {value for value in thread_ids if valid_id(value)}
        result = {'available': False, 'threads': {}}
        if not requested:
            return {'available': True, 'threads': {}}
        state = history = None
        try:
            state = open_readonly(self.home / 'state_5.sqlite')
            history = open_readonly(self.home / 'thread_history_1.sqlite')
            rows = state.execute('SELECT id,rollout_path,name,title,cwd,archived FROM threads').fetchall()
            by_id = {row['id']: row for row in rows if valid_id(row['id'])}
            aliases = {}
            for row in rows:
                ids = UUID.findall(text(row['rollout_path']))
                if ids:
                    aliases.setdefault(ids[-1], []).append(row)
            for key in requested:
                row = by_id.get(key)
                if row is None and len(aliases.get(key, [])) > 1:
                    continue  # An ambiguous rollout alias is not a deletion.
                if row is None and len(aliases.get(key, [])) == 1:
                    row = aliases[key][0]
                if row is None:
                    result['threads'][key] = {'threadId': key, 'visible': False, 'reason': 'deleted'}
                    continue
                metadata = {'threadId': row['id'], 'visible': not bool(row['archived']),
                            'title': text(row['name']) or text(row['title']) or '未命名对话',
                            'cwd': text(row['cwd']), 'reason': 'archived' if row['archived'] else None}
                if row['archived']:
                    result['threads'][key] = metadata
                    continue
                ids = UUID.findall(text(row['rollout_path']))
                actual = ids[-1] if ids else row['id']
                turns = history.execute('''SELECT turn_id,status,error_json,started_at,completed_at,rollout_ordinal
                    FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal,started_at,turn_id''', (actual,)).fetchall()
                if not turns and actual != row['id']:
                    actual = row['id']
                    turns = history.execute('''SELECT turn_id,status,error_json,started_at,completed_at,rollout_ordinal
                        FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal,started_at,turn_id''', (actual,)).fetchall()
                revision = history.execute(f'''SELECT count(*),max(updated_at_ordinal),sum(updated_at_ordinal) FROM thread_items
                    WHERE thread_id=? AND item_type IN {ITEM_TYPES}''', (actual,)).fetchone()
                fingerprint = (tuple(tuple(turn) for turn in turns), tuple(revision))
                cached = self.cache.get(actual)
                if cached is not None and cached[0] == fingerprint:
                    public_turns = cached[1]
                else:
                    public_turns = self._read_turns(history, actual, turns)
                    self.cache[actual] = (fingerprint, public_turns)
                metadata.update(actualThreadId=actual, turns=public_turns, historyAvailable=bool(turns))
                result['threads'][key] = metadata
            result['available'] = True
        except (OSError, sqlite3.Error, ValueError, TypeError):
            result = {'available': False, 'threads': {}}
        finally:
            if state is not None:
                state.close()
            if history is not None:
                history.close()
        return result

    def _read_turns(self, connection, thread_id, turns):
        # Large output/diffs stay in Codex until the user expands that item.
        # reasoning.content and image payloads are never selected.
        rows = connection.execute(f'''SELECT turn_id,item_id,item_type,created_at_ms,rollout_ordinal,updated_at_ordinal,
            CASE WHEN item_type='userMessage' THEN (
              SELECT json_group_array(json_extract(part.value,'$.text'))
              FROM json_each(json_extract(item_json,'$.content')) part
              WHERE json_extract(part.value,'$.type') IN ('text','inputText','input_text')) END AS user_texts,
            CASE WHEN item_type='agentMessage' THEN json_extract(item_json,'$.text') END AS assistant_text,
            CASE WHEN item_type='agentMessage' THEN json_extract(item_json,'$.phase') END AS phase,
            CASE WHEN item_type='reasoning' THEN json_extract(item_json,'$.summary') END AS public_summary,
            CASE WHEN item_type='commandExecution' THEN substr(json_extract(item_json,'$.command'),1,16000) END AS command,
            CASE WHEN item_type='commandExecution' THEN json_extract(item_json,'$.cwd') END AS cwd,
            CASE WHEN item_type IN ('commandExecution','fileChange') THEN json_extract(item_json,'$.status') END AS tool_status,
            CASE WHEN item_type='commandExecution' THEN json_extract(item_json,'$.exitCode') END AS exit_code,
            CASE WHEN item_type='commandExecution' THEN json_extract(item_json,'$.durationMs') END AS duration_ms,
            CASE WHEN item_type='commandExecution' THEN length(json_extract(item_json,'$.aggregatedOutput')) END AS output_length,
            CASE WHEN item_type='fileChange' THEN (
              SELECT json_group_array(json_object('path',json_extract(change.value,'$.path'),
                'kind',json_extract(change.value,'$.kind.type'),'movePath',json_extract(change.value,'$.kind.move_path')))
              FROM json_each(json_extract(item_json,'$.changes')) change) END AS changes
            FROM thread_items WHERE thread_id=? AND item_type IN {ITEM_TYPES}
            ORDER BY rollout_ordinal,item_id''', (thread_id,)).fetchall()
        by_turn = {}
        for item in rows:
            by_turn.setdefault(item['turn_id'], []).append(item)
        result = []
        for row in turns:
            if not valid_id(row['turn_id']):
                continue
            messages, summaries = [], []
            for item in by_turn.get(row['turn_id'], []):
                role, body = None, ''
                if item['item_type'] == 'userMessage':
                    values = json.loads(item['user_texts'] or '[]')
                    body = '\n'.join(value for value in values if isinstance(value, str))
                    role = 'user'
                elif item['item_type'] == 'agentMessage' and item['phase'] in ('commentary', 'final_answer', 'final', None):
                    body, role = text(item['assistant_text']), 'assistant'
                elif item['item_type'] == 'reasoning' and item['public_summary']:
                    value = item['public_summary']
                    try:
                        value = json.loads(value)
                    except (ValueError, TypeError):
                        pass
                    values = value if isinstance(value, list) else [value]
                    public = [text(part.get('text')) if isinstance(part, dict) else text(part) for part in values]
                    summaries.extend(part for part in public if part)
                    body, role = '\n\n'.join(part for part in public if part), 'summary'
                elif item['item_type'] in ('commandExecution', 'fileChange'):
                    changes = json.loads(item['changes'] or '[]')
                    messages.append({'id': item['item_id'], 'role': 'tool', 'toolType': item['item_type'],
                        'text': text(item['command']) if item['item_type'] == 'commandExecution' else
                                '\n'.join(text(change.get('path')) for change in changes),
                        'command': text(item['command']), 'cwd': text(item['cwd']),
                        'status': item['tool_status'], 'exitCode': item['exit_code'],
                        'durationMs': item['duration_ms'], 'outputLength': item['output_length'] or 0,
                        'changes': changes, 'revision': item['updated_at_ordinal'],
                        'created': timestamp(item['created_at_ms'], True), 'ordinal': item['rollout_ordinal']})
                if body and role:
                    messages.append({'id': item['item_id'], 'role': role, 'text': body,
                                     'phase': item['phase'] if role == 'assistant' else None,
                                     'created': timestamp(item['created_at_ms'], True),
                                     'ordinal': item['rollout_ordinal']})
            answers = [item['text'] for item in messages if item['role'] == 'assistant']
            finals = [item['text'] for item in messages if item['role'] == 'assistant' and item['phase'] in ('final_answer', 'final')]
            error = ''
            if row['error_json']:
                try:
                    value = json.loads(row['error_json'])
                    error = text(value.get('message')) if isinstance(value, dict) else text(value)
                except (ValueError, TypeError):
                    error = text(row['error_json'])
            result.append({'codexTurnId': row['turn_id'], 'codexOrdinal': row['rollout_ordinal'],
                           'created': timestamp(row['started_at']),
                           'status': 'running' if row['status'] in ACTIVE else row['status'],
                           'prompt': '\n\n'.join(item['text'] for item in messages if item['role'] == 'user'),
                           'messages': messages, 'summary': '\n\n'.join(part for part in summaries if part),
                           'output': '\n\n'.join(answers),
                           'result': '\n\n'.join(finals) or (answers[-1] if answers and row['status'] == 'completed' else ''),
                           'error': error if row['status'] in ('failed', 'interrupted') else ''})
        return result

    def read_tool_detail(self, thread_id, item_id):
        records = self.read([thread_id])
        record = records.get('threads', {}).get(thread_id)
        if not record or not record.get('visible'):
            return {'available': False}
        connection = None
        try:
            connection = open_readonly(self.home / 'thread_history_1.sqlite')
            row = connection.execute('''SELECT item_type,
                substr(json_extract(item_json,'$.command'),1,?),
                substr(json_extract(item_json,'$.aggregatedOutput'),1,?),
                substr(json_extract(item_json,'$.aggregatedOutput'),-?),
                length(json_extract(item_json,'$.aggregatedOutput'))
                FROM thread_items WHERE thread_id=? AND item_id=?
                  AND item_type IN ('commandExecution','fileChange')''',
                (MAX_DETAIL_CHARS, MAX_DETAIL_CHARS, MAX_DETAIL_CHARS // 2, record['actualThreadId'], item_id)).fetchone()
            if row is None:
                return {'available': False}
            if row[0] == 'commandExecution':
                output = text(row[2])
                truncated = (row[4] or 0) > MAX_DETAIL_CHARS
                if truncated:
                    output = output[:MAX_DETAIL_CHARS // 2] + '\n\n…（输出较长，已省略中间部分）…\n\n' + text(row[3])
                return {'available': True, 'command': text(row[1]), 'output': output, 'truncated': truncated}
            changes = connection.execute('''SELECT json_extract(change.value,'$.path'),
                json_extract(change.value,'$.kind.type'),json_extract(change.value,'$.kind.move_path'),
                json_extract(change.value,'$.diff')
                FROM thread_items item,json_each(json_extract(item.item_json,'$.changes')) change
                WHERE item.thread_id=? AND item.item_id=? AND item.item_type='fileChange' ''',
                (record['actualThreadId'], item_id))
            result = []
            for change in changes:
                diff = text(change[3])
                result.append({'path': text(change[0]), 'kind': change[1], 'movePath': text(change[2]) or None,
                               'diff': diff})
            return {'available': True, 'changes': result, 'truncated': False}
        except (OSError, sqlite3.Error, ValueError, TypeError):
            return {'available': False}
        finally:
            if connection is not None:
                connection.close()


def legacy_turn_id(chat, turn):
    if valid_id(turn.get('codexTurnId')):
        return turn['codexTurnId']
    key = chat.get('desktopTurnKey')
    if len(chat.get('turns', [])) == 1 and isinstance(key, str):
        value = key.partition(':')[2]
        return value if valid_id(value) else None
    return None


def coalesce_conversations(conversations, aliases=None, preferred_id=None):
    aliases = aliases or {}
    groups, output, redirects = {}, [], {}
    for chat in conversations:
        thread = chat.get('threadId')
        key = aliases.get(thread, thread)
        if not valid_id(key):
            output.append(chat)
        else:
            groups.setdefault(key, []).append(chat)
    for thread, entries in groups.items():
        chosen = next((chat for chat in entries if chat['id'] == preferred_id), None)
        chosen = chosen or next((chat for chat in entries if not chat.get('desktopSync')), entries[0])
        merged, known = [], {}
        for chat in entries:
            redirects[chat['id']] = chosen['id']
            for turn in chat.get('turns', []):
                key = legacy_turn_id(chat, turn)
                if key:
                    turn['codexTurnId'] = key
                if key and key in known:
                    previous = known[key]
                    if len(text(turn.get('output'))) > len(text(previous.get('output'))):
                        previous.update(turn)
                else:
                    merged.append(turn)
                    if key:
                        known[key] = turn
        chosen['threadId'] = thread
        chosen['turns'] = merged
        chosen.pop('desktopTurnKey', None)
        output.append(chosen)
    output.sort(key=lambda chat: chat.get('created', ''))
    return output, redirects


def merge_thread(chat, record, running_turn=None):
    before = deepcopy(chat)
    chat['threadId'] = record['threadId']
    chat['codexHidden'] = not record['visible']
    chat['codexHiddenReason'] = record.get('reason')
    if record['visible']:
        chat['title'] = ('Codex同步 · ' if chat.get('desktopSync') else '') + record['title']
        chat['cwd'] = record['cwd'] or chat.get('cwd', '')
    if record.get('historyAvailable'):
        old = chat.get('turns', [])
        by_id = {turn.get('codexTurnId'): turn for turn in old if turn.get('codexTurnId')}
        remaining = [turn for turn in old if not turn.get('codexTurnId')]
        ordered = []
        prompt_counts = Counter(turn['prompt'] for turn in record['turns'])
        for incoming in record['turns']:
            turn = by_id.get(incoming['codexTurnId'])
            if turn is None and prompt_counts[incoming['prompt']] == 1:
                turn = next((value for value in remaining if value.get('prompt') == incoming['prompt']), None)
                if turn is not None:
                    remaining.remove(turn)
            turn = turn if turn is not None else {'events': []}
            # Projection can lag a newly created turn; do not erase text that
            # the Desktop stream has already delivered for that same turn.
            values = deepcopy(incoming)
            if not incoming['messages'] and turn.get('messages'):
                for key in ('messages', 'prompt', 'output', 'result', 'summary'):
                    values[key] = turn.get(key, values[key])
            turn.update(values)
            ordered.append(turn)
        ordered.extend(remaining)
        if running_turn is not None and not any(turn is running_turn for turn in ordered):
            ordered.append(running_turn)
        chat['turns'] = ordered
        chat['codexHistoryVersion'] = 1
    return before != chat


def make_controller_class(original_class, *, home=None, background=True, reader_factory=None):
    class LinkedController(original_class):
        _bailong_conversation_sync = True

        def __init__(self, pet, directory=None):
            self._history_redirects = {}
            self._render_key = None
            self._render_signature = None
            self._render_html = None
            self._history_backup_done = False
            self._active_removed = False
            self._expanded_tools = set()
            self._expanded_tool_groups = set()
            self._seen_tool_groups = set()
            self._group_targets = set()
            self._expanded_summaries = set()
            self._summary_targets = set()
            self._tool_details = {}
            self._tool_targets = {}
            self._preserve_tool_scroll = False
            self._history_anchor = None
            self._history_follow_latest = True
            self._history_scroll_pending = False
            self._history_restore_busy = False
            self._opening_history = False
            self._history_horizontal_position = 0
            self._history_render_busy = False
            self._file_targets = {}
            self.file_views = None
            super().__init__(pet, directory=directory)
            policy = getattr(pet, 'window_policy', None)
            if policy is not None:
                policy().watch(self.composer, 'composer')
            from PySide6.QtCore import Qt
            from bailong_theme import SCROLLBAR_STYLE
            self.window.setStyleSheet(self.window.styleSheet() + SCROLLBAR_STYLE)
            self.composer.setStyleSheet(self.composer.styleSheet() + SCROLLBAR_STYLE)
            self.window.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.file_views = create_file_views(self.window.view)
            self.window.view.anchorClicked.connect(self._toggle_tool_detail)
            bar = self.window.view.verticalScrollBar()
            bar.rangeChanged.connect(self._queue_history_scroll)
            bar.actionTriggered.connect(self._history_user_scroll)
            bar.sliderPressed.connect(self._history_user_scroll)
            bar.sliderReleased.connect(self._remember_history_scroll)
            horizontal = self.window.view.horizontalScrollBar()
            horizontal.valueChanged.connect(self._remember_history_horizontal)
            horizontal.rangeChanged.connect(self._queue_history_scroll)
            self.window.view.installEventFilter(self)
            self.window.view.viewport().installEventFilter(self)
            self._coalesce()
            self.conversation_sync = ConversationSync(self, (reader_factory or ConversationReader)(home), background=background)
            self.refresh_list()

        def toggle(self):
            policy = getattr(self.pet, 'window_policy', None)
            if policy is not None and not self.enabled:
                policy().user_open('composer')
            super().toggle()
            if policy is not None:
                policy().schedule_stack()

        def position(self):
            toolbar = getattr(self.pet, 'toolbar', None)
            if self.enabled and toolbar is not None:
                # Share the obstacle-aware placement used during pet dragging;
                # the upstream positioner always puts the composer underneath.
                toolbar.sync_geometry()
                return
            super().position()

        def _backup_history(self):
            if self._history_backup_done or not self.store.path.exists():
                return
            target = self.store.directory / 'backups' / f'conversations-before-thread-sync-{time.time_ns()}.json'
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream:
                stream.write(self.store.path.read_bytes())
            self._history_backup_done = True

        def _coalesce(self, aliases=None):
            before = deepcopy(self.store.conversations)
            preferred = self.running_chat or self.active
            chats, redirects = coalesce_conversations(self.store.conversations, aliases,
                                                       preferred['id'] if preferred else None)
            if chats != before:
                self._backup_history()
                self.store.conversations = chats
                self._history_redirects.update(redirects)
                if self.active:
                    selected = redirects.get(self.active['id'], self.active['id'])
                    self.active = next((chat for chat in chats if chat['id'] == selected), None)
                self.dirty = True

        def refresh_list(self):
            from PySide6.QtCore import Qt
            from PySide6.QtWidgets import QListWidgetItem
            visible = [chat for chat in self.store.conversations if not chat.get('codexHidden')]
            if self.active and self.active.get('codexHidden'):
                self.active = None
                self._active_removed = True
            self.window.list.blockSignals(True)
            self.window.list.clear()
            for chat in reversed(visible):
                item = QListWidgetItem(chat['title'])
                item.setToolTip(chat['title'])
                item.setData(Qt.ItemDataRole.UserRole, chat['id'])
                self.window.list.addItem(item)
                if self.active is chat:
                    self.window.list.setCurrentItem(item)
            self.window.list.blockSignals(False)
            self.render()
            if self._active_removed and self.active is None:
                self.window.status.setText('此对话已在 Codex 中移除，请选择其他对话或新建对话。')

        def select(self, chat_id):
            previous_id = self.active.get('id') if self.active else None
            chat_id = self._history_redirects.get(chat_id, chat_id)
            self.active = next((chat for chat in self.store.conversations
                                if chat['id'] == chat_id and not chat.get('codexHidden')), None)
            if self.active is not None:
                self._active_removed = False
            if (self.active.get('id') if self.active else None) != previous_id:
                self._history_anchor = None
                self._history_follow_latest = True
                self._history_horizontal_position = 0
            self.render()
            sync = getattr(self, 'conversation_sync', None)
            if sync is not None:
                sync.request()

        def new_chat(self):
            super().new_chat()
            if self.active is not None and not self.active.get('codexHidden'):
                self._active_removed = False

        def submit(self, prompt):
            if self._active_removed and self.active is None:
                self.pet.say('这段对话已在 Codex 中移除，请先选择或新建对话。')
                return
            super().submit(prompt)

        def receive(self, kind, value):
            if kind == 'codex_turn' and self.running_turn is not None and valid_id(value):
                self.running_turn['codexTurnId'] = value
            super().receive(kind, value)
            if kind in ('thread', 'codex_turn'):
                self.conversation_sync.request()

        def _toggle_tool_detail(self, url):
            if url.scheme() not in ('bailong-tool', 'bailong-summary', 'bailong-group') or url.query() or url.fragment() or url.host():
                return
            token = url.path()
            targets = (self._group_targets if url.scheme() == 'bailong-group' else
                       self._summary_targets if url.scheme() == 'bailong-summary' else self._tool_targets)
            if token not in targets:
                return
            self._capture_history_anchor(url.toString())
            if url.scheme() == 'bailong-group':
                if token not in self._group_targets:
                    return
                if token in self._expanded_tool_groups:
                    self._expanded_tool_groups.remove(token)
                else:
                    self._expanded_tool_groups.add(token)
            elif url.scheme() == 'bailong-summary':
                if token not in self._summary_targets:
                    return
                if token in self._expanded_summaries:
                    self._expanded_summaries.remove(token)
                else:
                    self._expanded_summaries.add(token)
            else:
                if token not in self._tool_targets:
                    return
                if token in self._expanded_tools:
                    self._expanded_tools.remove(token)
                else:
                    self._expanded_tools.add(token)
                    entry = self._tool_details.get(token)
                    if entry is not None and entry.get('state') == 'error':
                        self._tool_details.pop(token, None)
            self._preserve_tool_scroll = True
            self._render_signature = None
            self.render()

        def _history_link_cursor(self, href):
            from PySide6.QtGui import QTextCursor
            document = self.window.view.document()
            block = document.begin()
            while block.isValid():
                iterator = block.begin()
                while not iterator.atEnd():
                    fragment = iterator.fragment()
                    if fragment.isValid() and fragment.charFormat().anchorHref() == href:
                        cursor = QTextCursor(document)
                        cursor.setPosition(fragment.position())
                        return cursor
                    iterator += 1
                block = block.next()
            return None

        def _capture_history_anchor(self, href):
            cursor = self._history_link_cursor(href)
            self._history_follow_latest = False
            if cursor is not None and self.active is not None:
                self._history_anchor = {'href': href, 'chat': self.active['id'],
                                        'offset': self.window.view.cursorRect(cursor).top()}

        def _queue_history_scroll(self, *args):
            if self._history_scroll_pending or self._history_restore_busy:
                return
            from PySide6.QtCore import QTimer
            self._history_scroll_pending = True
            QTimer.singleShot(0, self._apply_history_scroll)

        def _apply_history_scroll(self):
            self._history_scroll_pending = False
            if self._history_restore_busy or getattr(self, 'closing', False):
                return
            self._history_restore_busy = True
            try:
                view = self.window.view
                view.document().documentLayout().documentSize()
                bar = view.verticalScrollBar()
                anchor = self._history_anchor
                if anchor is not None and self.active and anchor['chat'] == self.active['id']:
                    cursor = self._history_link_cursor(anchor['href'])
                    if cursor is not None:
                        view.setTextCursor(cursor)
                        delta = view.cursorRect(cursor).top() - anchor['offset']
                        bar.setValue(bar.value() + delta)
                elif self._history_follow_latest:
                    bar.setValue(bar.maximum())
                horizontal = view.horizontalScrollBar()
                target = min(self._history_horizontal_position, horizontal.maximum())
                horizontal.setValue(target)
                self._history_horizontal_position = target
            finally:
                self._history_restore_busy = False

        def _history_user_scroll(self, *args):
            from PySide6.QtCore import QTimer
            self._history_anchor = None
            self._history_follow_latest = False
            QTimer.singleShot(0, self._remember_history_scroll)

        def _remember_history_scroll(self):
            bar = self.window.view.verticalScrollBar()
            self._history_follow_latest = bar.value() >= bar.maximum() - 20

        def _remember_history_horizontal(self, value):
            if not self._history_restore_busy and not self._history_render_busy:
                self._history_horizontal_position = value

        def eventFilter(self, watched, event):
            from PySide6.QtCore import QEvent, Qt
            if watched in (self.window.view, self.window.view.viewport()):
                if event.type() in (QEvent.Type.Wheel, QEvent.Type.MouseButtonPress):
                    self._history_user_scroll()
                elif event.type() == QEvent.Type.KeyPress and event.key() in (
                        Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown,
                        Qt.Key.Key_Home, Qt.Key.Key_End, Qt.Key.Key_Space):
                    self._history_user_scroll()
            return super().eventFilter(watched, event)

        def _summary_html(self, thread_id, item_id, body, title='思考摘要（公开）'):
            token = tool_token('summary:' + thread_id, item_id)
            self._summary_targets.add(token)
            expanded = token in self._expanded_summaries
            result = '<h3 style="color:#89739f">' + html.escape(title) + '</h3>'
            result += '<p><a style="color:#8061a3" href="bailong-summary:' + token + '">' + ('收起' if expanded else '展开') + '</a></p>'
            if expanded:
                result += '<p style="white-space:pre-wrap">' + html.escape(body) + '</p>'
            return result

        def accept_tool_detail(self, token, key, result):
            entry = self._tool_details.get(token)
            if entry is None or entry['key'] != key:
                return
            entry['state'] = 'ready' if result.get('available') else 'error'
            if result.get('available') or not (entry.get('value') or {}).get('available'):
                entry['value'] = result
            if token in self._expanded_tools and token in self._tool_targets:
                self._preserve_tool_scroll = bool(entry.get('user_open'))
                self._render_signature = None
                self.render()

        def _tool_html(self, thread_id, message, requests):
            token = tool_token(thread_id, text(message.get('id')))
            key = (thread_id, message['id'], message.get('revision'))
            self._tool_targets[token] = key
            expanded = token in self._expanded_tools
            command = message['toolType'] == 'commandExecution'
            status = tool_status(message)
            color = '#ac5475' if status == '执行失败' else '#6b4d91'
            parts = ['<h3 style="color:' + color + '">' + ('命令执行' if command else '文件编辑') + ' · ' + status + '</h3>']
            parts.append('<p><a style="color:#8061a3" href="bailong-tool:' + token + '">' + ('收起详情' if expanded else '展开详情') + '</a></p>')
            if not expanded:
                return ''.join(parts)
            metadata = []
            if command:
                if message.get('cwd'):
                    metadata.append('目录：' + message['cwd'])
                if message.get('exitCode') is not None:
                    metadata.append('退出码：' + str(message['exitCode']))
                duration = message.get('durationMs')
                if isinstance(duration, (int, float)) and duration >= 0:
                    metadata.append('耗时：' + (f'{duration / 1000:.1f} 秒' if duration >= 1000 else f'{duration:g} 毫秒'))
                if message.get('outputLength'):
                    metadata.append('输出：' + str(message['outputLength']) + ' 字符')
            else:
                changes = message.get('changes', [])
                for change in changes[:5]:
                    kind = {'add': '新增', 'delete': '删除', 'update': '修改', 'move': '移动'}.get(change.get('kind'), '变更')
                    path = text(change.get('path'))
                    if change.get('movePath'):
                        kind = '移动'
                        path += ' → ' + text(change['movePath'])
                    metadata.append(kind + '：' + path)
                if len(changes) > 5:
                    metadata.append(f'另有 {len(changes) - 5} 个文件')
            if metadata:
                parts.append('<p style="color:#89739f">' + (' · ' if command else '<br>').join(html.escape(value) for value in metadata) + '</p>')
            entry = self._tool_details.get(token)
            if entry is None or entry['key'] != key:
                previous = entry.get('value') if entry is not None else None
                entry = {'key': key, 'state': 'loading', 'value': previous,
                         'user_open': self._preserve_tool_scroll}
                self._tool_details[token] = entry
                requests.append((token, key))
            detail = entry.get('value') or {}
            if detail.get('available'):
                def pre(body):
                    return '<pre style="font-family:Consolas,monospace;background-color:#f0eaf8;white-space:pre-wrap">' + html.escape(body) + '</pre>'
                if command:
                    snippet, shortened = code_excerpt(detail.get('command'), max_lines=6, max_chars=500)
                    parts.append('<h4>命令' + ('（节选）' if shortened else '') + '</h4>' + pre(snippet))
                    if shortened:
                        parts.append('<p style="color:#89739f">长代码仅显示节选。</p>')
                    output, output_shortened = output_excerpt(detail.get('output'))
                    parts.append('<h4>输出' + ('（节选）' if output_shortened else '') + '</h4>' + pre(output or '暂无输出。'))
                else:
                    for index, change in enumerate(detail.get('changes', [])):
                        path = change['path'] + (' → ' + change['movePath'] if change.get('movePath') else '')
                        file_key = tool_token('file:' + thread_id, message['id'] + ':' + str(index) + ':' + change['path'])
                        self._file_targets[file_key] = change
                        parts.append('<h4>' + html.escape(path) + '</h4>' + file_placeholder(file_key))
                if detail.get('truncated'):
                    parts.append('<p>内容较长，当前详情保留了部分内容。</p>')
            if entry['state'] == 'loading':
                parts.append('<p>正在更新详情…</p>' if detail else '<p>正在读取详情…</p>')
            elif entry['state'] == 'error':
                parts.append('<p>暂时无法读取详情，可收起后重新展开。</p>')
            return ''.join(parts)

        def _tool_group_html(self, thread_id, messages, requests):
            if len(messages) == 1:
                return self._tool_html(thread_id, messages[0], requests)
            kind = messages[0]['toolType']
            token = tool_token('group:' + thread_id, kind + ':' + messages[0]['id'])
            self._group_targets.add(token)
            if token not in self._seen_tool_groups:
                self._seen_tool_groups.add(token)
                if any(tool_token(thread_id, message['id']) in self._expanded_tools for message in messages):
                    self._expanded_tool_groups.add(token)
            counts = Counter(tool_status(message) for message in messages)
            status = next(iter(counts)) if len(counts) == 1 else '，'.join(f'{count}项{label}' for label, count in counts.items())
            color = '#ac5475' if '执行失败' in counts else '#6b4d91'
            expanded = token in self._expanded_tool_groups
            title = ('命令执行' if kind == 'commandExecution' else '文件编辑') + f' · {len(messages)} 项 · ' + status
            content = ['<h3 style="color:' + color + '">' + html.escape(title) + '</h3>',
                       '<p><a style="color:#8061a3" href="bailong-group:' + token + '">' +
                       ('收起记录' if expanded else f'展开 {len(messages)} 项记录') + '</a></p>']
            if expanded:
                for index, message in enumerate(messages, 1):
                    content.append('<p style="color:#89739f">第 ' + str(index) + ' 项</p>')
                    content.append(self._tool_html(thread_id, message, requests))
            return ''.join(content)

        def render(self):
            chat = self.active
            if hasattr(self.window, 'set_chat_header'):
                self.window.set_chat_header(chat)
            if not chat:
                self._render_signature = None
                self._render_html = None
                self._summary_targets = set()
                self._tool_targets = {}
                self._group_targets = set()
                self._history_anchor = None
                self._file_targets = {}
                if self.file_views is not None:
                    self.file_views.sync({})
                return super().render()
            displayed = [(turn.get('messages'), turn.get('status'), turn.get('error'),
                          turn.get('prompt') if not turn.get('messages') else None,
                          turn.get('summary') if not turn.get('messages') else None,
                          turn.get('events') if not turn.get('messages') else None,
                          turn.get('output') if not turn.get('messages') else None,
                          turn.get('result') if not turn.get('messages') else None) for turn in chat['turns']]
            signature = hashlib.sha256(json.dumps(displayed, ensure_ascii=False, sort_keys=True).encode('utf-8')).digest()
            self.window.path_label.setText('任务文件夹：' + chat.get('cwd', ''))
            if self._render_key != chat['id']:
                self._history_anchor = None
                self._history_follow_latest = True
            if self._render_key == chat['id'] and signature == self._render_signature:
                return
            def block(title, body, color='#38294d'):
                return '<h3 style="color:' + color + '">' + html.escape(title) + '</h3><p style="white-space:pre-wrap">' + html.escape(body) + '</p>'
            content = []
            requests = []
            self._tool_targets = {}
            self._summary_targets = set()
            self._group_targets = set()
            self._file_targets = {}
            thread_key = chat.get('threadId') or chat['id']
            for turn_index, turn in enumerate(chat['turns']):
                messages = turn.get('messages')
                if messages:
                    for entries in timeline_groups(messages):
                        message = entries[0]
                        if message['role'] == 'tool':
                            content.append(self._tool_group_html(chat['threadId'], entries, requests))
                            continue
                        if message['role'] == 'user':
                            title = '主人' + (' · ' + message['created'] if message.get('created') else '')
                            content.append(block(title, message['text'], '#6f4697'))
                        elif message['role'] == 'summary':
                            content.append(self._summary_html(thread_key, message['id'], message['text']))
                        else:
                            content.append(block('白龙 · 回复' if message.get('phase') in ('final_answer', 'final') else '白龙 · 进展', message['text']))
                else:
                    content.append(block('主人 · ' + turn.get('created', ''), text(turn.get('prompt')), '#6f4697'))
                    legacy_key = (turn.get('codexTurnId') or turn.get('created') or 'turn') + ':' + str(turn_index)
                    if turn.get('summary'):
                        content.append(self._summary_html(thread_key, 'legacy:' + legacy_key, text(turn['summary'])))
                    if turn.get('events'):
                        content.append(self._summary_html(thread_key, 'events:' + legacy_key,
                                                         '\n\n'.join(map(str, turn['events'])), '执行记录'))
                    body = text(turn.get('output')) or text(turn.get('result'))
                    if body:
                        content.append(block('白龙 · 回复', body))
                if turn.get('status') in ('failed', 'interrupted') and turn.get('error'):
                    content.append(block('本轮未完成', text(turn['error'])))
            bar = self.window.view.verticalScrollBar()
            near_bottom = bar.value() >= bar.maximum() - 20
            if self._history_anchor is None and not self._opening_history and self._render_key == chat['id']:
                self._history_follow_latest = near_bottom
            at_bottom = (not self._preserve_tool_scroll and self._history_anchor is None
                         and (self._opening_history or self._render_key != chat['id'] or near_bottom))
            previous = bar.value()
            horizontal = self.window.view.horizontalScrollBar()
            if self._render_key == chat['id'] and not self._opening_history:
                self._history_horizontal_position = horizontal.value()
            self.window.path_label.setText('任务文件夹：' + chat.get('cwd', ''))
            rendered = ''.join(content)
            if self._render_key != chat['id'] or rendered != self._render_html:
                self._history_render_busy = True
                try:
                    if self.file_views is not None:
                        self.file_views.before_document_reset()
                    self.window.view.setHtml(rendered)
                finally:
                    self._history_render_busy = False
            if self.file_views is not None:
                self.file_views.sync(self._file_targets)
            if self._render_key != chat['id'] or rendered != self._render_html:
                bar.setValue(bar.maximum() if at_bottom else min(previous, bar.maximum()))
            self._render_html = rendered
            self._render_key, self._render_signature = chat['id'], signature
            self._preserve_tool_scroll = False
            self._apply_history_scroll()
            self._queue_history_scroll()
            sync = getattr(self, 'conversation_sync', None)
            if sync is not None:
                for token, key in requests:
                    sync.request_tool_detail(token, key)

        def open_history(self, chat_id=None):
            self._history_anchor = None
            self._history_follow_latest = True
            self._history_horizontal_position = 0
            self._opening_history = True
            try:
                self.conversation_sync.request()
                super().open_history(chat_id)
            finally:
                self._opening_history = False
            self._apply_history_scroll()
            self._queue_history_scroll()

            policy = getattr(self.pet, 'window_policy', None)
            if policy is not None:
                policy().watch(self.window)
                policy().schedule_stack()

        def shutdown(self):
            sync = getattr(self, 'conversation_sync', None)
            if sync is not None:
                sync.close()
            return super().shutdown()

    return LinkedController


class ConversationSync:
    def __init__(self, controller, reader, *, background=True):
        from PySide6.QtCore import QTimer
        self.controller, self.reader = controller, reader
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='bailong-history') if background else None
        self.future = None
        self.detail_futures = {}
        self.closed = False
        self.available = None
        self.error_type = None
        self.timer = QTimer(controller)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.poll)
        if background:
            self.timer.start()
            QTimer.singleShot(0, self.poll)

    def request(self):
        if self.executor is not None and not self.closed:
            self.poll()

    def poll(self):
        if self.closed:
            return
        if self.future is not None and self.future.done():
            future, self.future = self.future, None
            try:
                self.apply(future.result())
            except Exception as error:
                # Keep the last confirmed history. Do not convert a failed
                # read to deletion, nor persist transcript-bearing tracebacks.
                self.available = False
                self.error_type = type(error).__name__
        for token, (key, future) in list(self.detail_futures.items()):
            if future.done():
                self.detail_futures.pop(token, None)
                try:
                    result = future.result()
                except Exception:
                    result = {'available': False}
                self.controller.accept_tool_detail(token, key, result)
        if self.future is None and self.executor is not None:
            ids = tuple(chat.get('threadId') for chat in self.controller.store.conversations if valid_id(chat.get('threadId')))
            if ids:
                self.future = self.executor.submit(self.reader.read, ids)

    def request_tool_detail(self, token, key):
        if self.closed:
            return
        existing = self.detail_futures.get(token)
        if existing is not None and existing[0] == key:
            return
        if existing is not None:
            existing[1].cancel()
        if self.executor is None:
            self.controller.accept_tool_detail(token, key, self.reader.read_tool_detail(key[0], key[1]))
        else:
            self.detail_futures[token] = (key, self.executor.submit(self.reader.read_tool_detail, key[0], key[1]))

    def apply(self, snapshot):
        if self.closed or not snapshot.get('available'):
            self.available = False
            return
        self.available = True
        self.error_type = None
        controller = self.controller
        records = snapshot['threads']
        aliases = {key: record['threadId'] for key, record in records.items()}
        controller._coalesce(aliases)
        changed = False
        for chat in controller.store.conversations:
            record = records.get(chat.get('threadId')) or next((value for value in records.values()
                        if value['threadId'] == chat.get('threadId')), None)
            if record is None:
                continue
            if chat.get('codexHistoryVersion') != 1:
                controller._backup_history()
            running = controller.running_turn if controller.running_chat is chat else None
            changed |= merge_thread(chat, record, running)
        if changed:
            controller.dirty = True
            controller.refresh_list()

    def close(self):
        self.closed = True
        self.timer.stop()
        if self.executor is not None:
            self.executor.shutdown(wait=False, cancel_futures=True)


def install_conversation_sync(original=None):
    ui = importlib.import_module('chat_ui')
    sync = importlib.import_module('desktop_task_sync')
    from chat_ui_style import install_chat_ui
    install_chat_ui(ui)
    if getattr(ui.ChatController, '_bailong_conversation_sync', False):
        return ui.ChatController
    ui.ChatController = make_controller_class(ui.ChatController)

    sync.DesktopTaskSync.history = notification_history
    sync.DesktopTaskSync.excluded = excluded_thread_ids
    return ui.ChatController


def excluded_thread_ids(self):
    controller = getattr(self.pet, 'chat', None)
    chat = getattr(controller, 'running_chat', None)
    return {chat['threadId']} if chat and valid_id(chat.get('threadId')) and controller.running_turn is not None else set()

def notification_history(self, snapshot, record):
    controller = self.pet.chat_controller()
    if controller is None:
        return
    thread, turn_id = snapshot['thread'], snapshot['turn']
    chat = next((value for value in controller.store.conversations if value.get('threadId') == thread), None)
    if chat is None:
        chat = {'id': uuid.uuid4().hex, 'threadId': thread, 'title': 'Codex同步 · ' + snapshot['title'],
                'cwd': snapshot['cwd'], 'created': timestamp(snapshot['started']) or controller.store.now(),
                'turns': [], 'mode': 'work', 'desktopSync': True}
        controller.store.conversations.append(chat)
        if controller.active is None and not controller._active_removed:
            controller.active = chat
    turn = next((value for value in chat['turns'] if value.get('codexTurnId') == turn_id), None)
    if turn is None:
        turn = {'codexTurnId': turn_id, 'prompt': '', 'created': timestamp(snapshot['started']),
                'status': 'running', 'output': '', 'result': '', 'events': []}
        chat['turns'].append(turn)
    if not turn.get('messages'):
        turn.update(output=snapshot['final'], result=snapshot['final'],
                    status='running' if snapshot['status'] in ACTIVE else snapshot['status'])
    record.update(chat_id=chat['id'], chat=chat, turn=turn,
                  seen={tuple(value) for value in turn.get('syncNotices', [])},
                  repeat_times=turn.get('syncRepeatTimes', {}))
    controller.dirty = True
    controller.refresh_list()
    controller.conversation_sync.request()
