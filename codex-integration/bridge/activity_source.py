"""Read only Codex thread-index metadata for the companion's activity cards.

This module never reads transcript paths, message/title fallbacks, previews,
tool items, error payloads, or logs. SQLite authorizers enforce column-level
allowlists in addition to URI read-only mode and PRAGMA query_only.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import time
import unicodedata
from urllib.parse import quote

SCHEMA_VERSION = 1
MAX_ROOTS = 128
RECENT_SECONDS = 600
STATE_COLUMNS = {
    'threads': frozenset(('id', 'name', 'archived', 'originator', 'project_id', 'updated_at_ms')),
    'projects': frozenset(('id', 'name')),
    'thread_spawn_edges': frozenset(('parent_thread_id', 'child_thread_id', 'status')),
    'thread_read_receipts': frozenset(('thread_id', 'first_unread_turn', 'last_published_turn')),
}
HISTORY_COLUMNS = {
    'thread_turns': frozenset(('thread_id', 'turn_id', 'rollout_ordinal', 'status',
                             'started_at', 'completed_at', 'duration_ms', 'root_turn_id')),
}
_IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z')
ROOT_WHERE = """t.archived = 0 AND t.originator = 'Codex Desktop'
    AND NOT EXISTS (SELECT 1 FROM thread_spawn_edges edge
                    WHERE edge.child_thread_id = t.id)"""


class UnsupportedSchema(Exception):
    pass


def _authorizer(columns):
    allowed_functions = {'count', 'max', 'coalesce'}
    def authorize(action, first, second, database, trigger):
        if action == sqlite3.SQLITE_READ:
            return (sqlite3.SQLITE_OK if first in columns
                    and (second in columns[first] or second == '') else sqlite3.SQLITE_DENY)
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_OK if second in allowed_functions else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA:
            return sqlite3.SQLITE_OK if first == 'table_info' and second in columns else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY
    return authorize


def open_index(path: Path, columns):
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True,
                                 timeout=0.1, isolation_level=None)
    connection.execute('PRAGMA query_only=ON')
    connection.row_factory = sqlite3.Row
    connection.set_authorizer(_authorizer(columns))
    return connection


def _check_schema(connection, tables):
    for table, required in tables.items():
        present = {row['name'] for row in connection.execute(f'PRAGMA table_info("{table}")')}
        if not required <= present:
            raise UnsupportedSchema(table)


def plain_label(value, fallback=''):
    if not isinstance(value, str):
        return fallback
    value = unicodedata.normalize('NFC', value)
    value = ''.join(' ' if character.isspace() else character
                    for character in value
                    if character.isspace() or not unicodedata.category(character).startswith('C'))
    value = ' '.join(value.split())[:160].strip()
    return value or fallback


def _timestamp(value, *, milliseconds=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    seconds = value / 1000 if milliseconds else value
    return float(seconds) if 0 <= seconds <= 253402300799 else None


def _identifier(value):
    return isinstance(value, str) and bool(_IDENTIFIER.fullmatch(value))


def _base(now):
    return {'schemaVersion': SCHEMA_VERSION, 'source': 'local-thread-index',
            'observedAt': datetime.fromtimestamp(now, timezone.utc).isoformat(timespec='milliseconds'),
            'available': False, 'reason': None, 'sessions': [], 'truncated': False}


def snapshot(codex_home: Path | str | None = None, *, app_started_at=None,
             now=None, limit=MAX_ROOTS):
    now = time.time() if now is None else float(now)
    result = _base(now)
    limit = min(MAX_ROOTS, max(1, int(limit)))
    home = Path(codex_home) if codex_home is not None else Path.home() / '.codex'
    app_start = _timestamp(app_started_at)
    result['appStartedAt'] = app_start
    state = history = None
    try:
        state = open_index(home / 'state_5.sqlite', STATE_COLUMNS)
        history = open_index(home / 'thread_history_1.sqlite', HISTORY_COLUMNS)
        _check_schema(state, STATE_COLUMNS)
        _check_schema(history, HISTORY_COLUMNS)
        total = state.execute(f'SELECT count(*) FROM threads t WHERE {ROOT_WHERE}').fetchone()[0]
        roots = state.execute(f"""
            SELECT t.id, t.name, t.updated_at_ms, p.name AS project_name,
                   receipt.thread_id AS receipt_thread_id,
                   receipt.first_unread_turn, receipt.last_published_turn
            FROM threads t
            LEFT JOIN projects p ON p.id = t.project_id
            LEFT JOIN thread_read_receipts receipt ON receipt.thread_id = t.id
            WHERE {ROOT_WHERE}
            ORDER BY t.updated_at_ms DESC, t.id ASC LIMIT ?
        """, (limit,)).fetchall()
        result['truncated'] = total > limit
        result['rootCount'] = total
        if not roots:
            result.update(available=True)
            return result
        by_id = {row['id']: row for row in roots if _identifier(row['id'])}
        if not by_id:
            result.update(available=True)
            return result
        placeholders = ','.join('?' for _ in by_id)
        latest = history.execute(f"""
            SELECT turn.thread_id, turn.turn_id, turn.rollout_ordinal, turn.status,
                   turn.started_at, turn.completed_at, turn.duration_ms, turn.root_turn_id
            FROM thread_turns turn
            WHERE turn.thread_id IN ({placeholders})
              AND turn.rollout_ordinal = (
                  SELECT max(previous.rollout_ordinal) FROM thread_turns previous
                  WHERE previous.thread_id = turn.thread_id)
            ORDER BY turn.rollout_ordinal DESC, turn.turn_id ASC
        """, tuple(by_id)).fetchall()
        earliest_unread = {}
        for thread_id, root in by_id.items():
            first = root['first_unread_turn']
            if _identifier(first):
                row = history.execute(
                    'SELECT rollout_ordinal FROM thread_turns WHERE thread_id = ? AND turn_id = ? LIMIT 1',
                    (thread_id, first)).fetchone()
                if row is not None:
                    earliest_unread[thread_id] = row['rollout_ordinal']
        sessions, seen = [], set()
        for turn in latest:
            thread_id, turn_id = turn['thread_id'], turn['turn_id']
            if thread_id in seen or not _identifier(turn_id):
                continue
            seen.add(thread_id)
            root = by_id[thread_id]
            started = _timestamp(turn['started_at'])
            completed = _timestamp(turn['completed_at'])
            updated = _timestamp(root['updated_at_ms'], milliseconds=True)
            raw_status = turn['status']
            mapped = {'inProgress': 'running', 'completed': 'completed',
                      'failed': 'failed', 'interrupted': 'idle'}.get(raw_status, 'unknown')
            stale_runtime = False
            unknown_reason = None
            if mapped == 'running' and app_start is not None:
                # The source records whole seconds; do not reject a turn that
                # began during the same second as the GUI process.
                if started is None or started < math.floor(app_start):
                    mapped = 'unknown'
                    stale_runtime = True
                    unknown_reason = 'before-current-app' if started is not None else 'missing-start-time'
            if mapped == 'unknown' and unknown_reason is None:
                unknown_reason = 'unsupported-index-state'
            unread = None
            if root['receipt_thread_id'] is not None:
                first = root['first_unread_turn']
                if first is None:
                    unread = False
                elif first == turn_id:
                    unread = True
                elif thread_id in earliest_unread:
                    unread = earliest_unread[thread_id] <= turn['rollout_ordinal']
            terminal_time = completed if completed is not None else updated
            recent_unread = (mapped in ('completed', 'failed') and unread is True
                             and terminal_time is not None and 0 <= now - terminal_time <= RECENT_SECONDS)
            sessions.append({
                'key': 'local:' + thread_id, 'thread_id': thread_id, 'turn_id': turn_id,
                'title': plain_label(root['name'], '未命名任务'),
                'project': plain_label(root['project_name']),
                'state': mapped, 'started_at': started, 'completed_at': completed,
                'updated_at': updated, 'duration_ms': turn['duration_ms'],
                'unread': unread, 'stale_runtime': stale_runtime,
                'unknown_reason': unknown_reason,
                'initial_visible': mapped == 'running' or recent_unread,
                'routeUri': 'codex://threads/' + quote(thread_id, safe=''),
            })
        priority = {'running': 0, 'failed': 1, 'completed': 2, 'unknown': 3, 'idle': 4}
        sessions.sort(key=lambda row: (priority[row['state']], -(row['updated_at'] or 0), row['key']))
        result.update(available=True, sessions=sessions)
        return result
    except UnsupportedSchema:
        result['reason'] = 'unsupported-index-schema'
    except sqlite3.Error as error:
        code = getattr(error, 'sqlite_errorcode', None)
        result['reason'] = ('database-busy' if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                            else 'index-unavailable')
    except (OSError, ValueError, TypeError, OverflowError):
        result['reason'] = 'index-unavailable'
    finally:
        if history is not None:
            history.close()
        if state is not None:
            state.close()
    return result


def parse_app_time(value):
    if value is None:
        return None
    try:
        number = float(value)
    except ValueError:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError('An ISO application start time must include a timezone')
        number = stamp.timestamp()
    if _timestamp(number) is None:
        raise ValueError('Invalid application start time')
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', action='store_true', required=True)
    parser.add_argument('--codex-home', type=Path)
    parser.add_argument('--app-started-at')
    args = parser.parse_args(argv)
    try:
        app_start = parse_app_time(args.app_started_at)
    except (ValueError, OverflowError):
        parser.error('Invalid --app-started-at; use epoch seconds or timezone-aware ISO 8601')
    print(json.dumps(snapshot(args.codex_home, app_started_at=app_start), ensure_ascii=True,
                     separators=(',', ':'), allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
