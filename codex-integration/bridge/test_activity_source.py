"""Read-only metadata projection tests using synthetic SQLite indexes only."""
from pathlib import Path
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import activity_source as source


class ActivitySourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.state_path = self.home / 'state_5.sqlite'
        self.history_path = self.home / 'thread_history_1.sqlite'
        self.state = sqlite3.connect(self.state_path)
        self.state.executescript('''
            CREATE TABLE threads(id TEXT PRIMARY KEY, name TEXT, archived INTEGER,
                originator TEXT, project_id TEXT, updated_at_ms INTEGER,
                title TEXT, first_user_message TEXT, preview TEXT, cwd TEXT);
            CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT, metadata TEXT);
            CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT, status TEXT);
            CREATE TABLE thread_read_receipts(thread_id TEXT PRIMARY KEY, first_unread_turn TEXT, last_published_turn TEXT);
            INSERT INTO projects VALUES('p1','项目 A','PRIVATE PROJECT PAYLOAD');
        ''')
        self.history = sqlite3.connect(self.history_path)
        self.history.executescript('''
            CREATE TABLE thread_turns(thread_id TEXT, turn_id TEXT, rollout_ordinal INTEGER,
                status TEXT, started_at INTEGER, completed_at INTEGER, duration_ms INTEGER,
                root_turn_id TEXT, error_json TEXT);
            CREATE TABLE thread_items(thread_id TEXT, item_json TEXT);
            INSERT INTO thread_items VALUES('root','PRIVATE TOOL AND MESSAGE BODY');
        ''')

    def tearDown(self):
        self.state.close()
        self.history.close()
        self.temporary.cleanup()

    def thread(self, thread_id, name='Task', archived=0, originator='Codex Desktop', updated=1990):
        self.state.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (thread_id, name, archived, originator, 'p1', updated * 1000,
                            'PRIVATE PROMPT FALLBACK', 'PRIVATE USER MESSAGE', 'PRIVATE PREVIEW', 'PRIVATE PATH'))

    def turn(self, thread_id, turn_id='turn-1', ordinal=1, status='inProgress', started=1900, completed=None):
        self.history.execute('INSERT INTO thread_turns VALUES(?,?,?,?,?,?,?,?,?)',
                             (thread_id, turn_id, ordinal, status, started, completed, 1234, None,
                              'PRIVATE ERROR PAYLOAD'))

    def receipt(self, thread_id, unread=None, published='turn-1'):
        self.state.execute('INSERT INTO thread_read_receipts VALUES(?,?,?)', (thread_id, unread, published))

    def snapshot(self, **kwargs):
        self.state.commit()
        self.history.commit()
        return source.snapshot(self.home, now=2000, app_started_at=1800, **kwargs)

    def test_root_child_orphan_archived_and_non_desktop_filter(self):
        for thread_id in ('root', 'child', 'archived', 'cli'):
            self.thread(thread_id, archived=int(thread_id == 'archived'),
                        originator='CLI' if thread_id == 'cli' else 'Codex Desktop')
            self.turn(thread_id)
        self.state.execute("INSERT INTO thread_spawn_edges VALUES('root','child','completed')")
        self.turn('orphan')
        result = self.snapshot()
        self.assertTrue(result['available'], result)
        self.assertEqual([row['thread_id'] for row in result['sessions']], ['root'])

    def test_latest_ordinal_wins_not_latest_timestamp(self):
        self.thread('root')
        self.turn('root', 'older', 1, started=1999)
        self.turn('root', 'newer', 2, 'completed', started=1900, completed=1950)
        self.receipt('root', 'newer', 'newer')
        item = self.snapshot()['sessions'][0]
        self.assertEqual((item['turn_id'], item['state']), ('newer', 'completed'))
        self.assertTrue(item['unread'])
        self.assertTrue(item['initial_visible'])

    def test_blank_name_never_falls_back_to_user_prompt_or_preview(self):
        self.thread('root', name=None)
        self.turn('root')
        result = self.snapshot()
        self.assertEqual(result['sessions'][0]['title'], '未命名任务')
        self.assertEqual(result['sessions'][0]['project'], '项目 A')
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_unread_old_terminal_not_initially_visible(self):
        self.thread('root')
        self.turn('root', status='failed', started=1000, completed=1200)
        self.receipt('root', 'turn-1')
        item = self.snapshot()['sessions'][0]
        self.assertEqual(item['state'], 'failed')
        self.assertTrue(item['unread'])
        self.assertFalse(item['initial_visible'])

    def test_read_receipt_missing_and_first_unread_ordinal(self):
        self.thread('root')
        self.turn('root', 'first', 1, 'completed', completed=1901)
        self.turn('root', 'second', 2, 'completed', completed=1990)
        self.assertIsNone(self.snapshot()['sessions'][0]['unread'])
        self.receipt('root', 'first', 'second')
        self.assertTrue(self.snapshot()['sessions'][0]['unread'])

    def test_unknown_first_unread_turn_is_not_guessed(self):
        self.thread('root')
        self.turn('root', status='completed', completed=1990)
        self.receipt('root', 'not-indexed-yet')
        self.assertIsNone(self.snapshot()['sessions'][0]['unread'])

    def test_old_in_progress_is_unknown_and_never_visible(self):
        self.thread('root')
        self.turn('root', started=1700)
        item = self.snapshot()['sessions'][0]
        self.assertEqual(item['state'], 'unknown')
        self.assertTrue(item['stale_runtime'])
        self.assertFalse(item['initial_visible'])

    def test_same_second_start_is_not_marked_stale(self):
        self.thread('root')
        self.turn('root', started=1800)
        self.state.commit(); self.history.commit()
        item = source.snapshot(self.home, now=2000, app_started_at=1800.9)['sessions'][0]
        self.assertEqual(item['state'], 'running')

    def test_interrupted_is_idle_not_success(self):
        self.thread('root')
        self.turn('root', status='interrupted', completed=1990)
        self.receipt('root', 'turn-1')
        item = self.snapshot()['sessions'][0]
        self.assertEqual(item['state'], 'idle')
        self.assertFalse(item['initial_visible'])

    def test_limit_and_timestamp_units(self):
        for i in range(3):
            self.thread(f'root-{i}', updated=1900 + i)
            self.turn(f'root-{i}', started=1800 + i)
        result = self.snapshot(limit=2)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['rootCount'], 3)
        self.assertEqual(len(result['sessions']), 2)
        self.assertEqual(result['sessions'][0]['updated_at'], 1902)
        self.assertTrue(result['sessions'][0]['routeUri'].startswith('codex://threads/'))

    def test_column_authorizer_blocks_payloads_and_writes(self):
        self.thread('root'); self.turn('root'); self.state.commit(); self.history.commit()
        connection = source.open_index(self.state_path, source.STATE_COLUMNS)
        try:
            self.assertEqual(connection.execute('SELECT name FROM threads').fetchone()[0], 'Task')
            for statement in ('SELECT title FROM threads', 'SELECT first_user_message FROM threads',
                              'SELECT preview FROM threads', 'SELECT cwd FROM threads',
                              "UPDATE threads SET name='changed'", 'PRAGMA query_only=OFF'):
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(statement)
        finally:
            connection.close()
        connection = source.open_index(self.history_path, source.HISTORY_COLUMNS)
        try:
            for statement in ('SELECT error_json FROM thread_turns', 'SELECT item_json FROM thread_items'):
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(statement)
        finally:
            connection.close()

    def test_missing_schema_and_database_do_not_create_files(self):
        missing = self.home / 'missing'
        result = source.snapshot(missing, now=2000)
        self.assertFalse(result['available'])
        self.assertFalse(missing.exists())
        self.state.execute('DROP TABLE projects')
        self.state.commit()
        result = source.snapshot(self.home, now=2000)
        self.assertEqual(result['reason'], 'unsupported-index-schema')

    def test_locked_index_is_unknown_not_completed(self):
        self.thread('root'); self.turn('root'); self.state.commit(); self.history.commit()
        self.state.execute('BEGIN EXCLUSIVE')
        try:
            result = source.snapshot(self.home, now=2000)
            self.assertFalse(result['available'])
            self.assertEqual(result['reason'], 'database-busy')
            self.assertEqual(result['sessions'], [])
        finally:
            self.state.rollback()

    def test_labels_and_start_time_parser(self):
        label = source.plain_label('  A\n B\u202e ' + 'x' * 200)
        self.assertTrue(label.startswith('A B '))
        self.assertEqual(len(label), 160)
        self.assertNotIn('\u202e', label)
        self.assertEqual(source.parse_app_time('1970-01-01T00:30:00Z'), 1800)
        with self.assertRaises(ValueError): source.parse_app_time('2026-01-01T00:00:00')

    def test_cli_is_ascii_and_read_only(self):
        self.thread('root', name='中文标题'); self.turn('root'); self.state.commit(); self.history.commit()
        result = subprocess.run([sys.executable, str(Path(source.__file__)), '--snapshot',
                                 '--codex-home', str(self.home), '--app-started-at', '1800'],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(byte < 128 for byte in result.stdout))
        self.assertEqual(json.loads(result.stdout)['sessions'][0]['title'], '中文标题')


if __name__ == '__main__':
    unittest.main()
