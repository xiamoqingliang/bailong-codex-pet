"""Read-only fixtures for full history, native continuation and deletion."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from conversation_sync import (ConversationReader, coalesce_conversations, merge_thread,
                               make_controller_class, notification_history, excluded_thread_ids,
                               tool_token, MAX_DETAIL_CHARS, ConversationSync, timeline_groups)


class DatabaseFixture:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.state = self.root / 'state_5.sqlite'
        self.history = self.root / 'thread_history_1.sqlite'
        self.write(self.state, '''CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT,
                   name TEXT,title TEXT,cwd TEXT,archived INTEGER)''')
        self.write(self.history, '''CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,status TEXT,
                   error_json TEXT,started_at INTEGER,completed_at INTEGER,rollout_ordinal INTEGER,
                   PRIMARY KEY(thread_id,turn_id))''')
        self.write(self.history, '''CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,
                   item_type TEXT,created_at_ms INTEGER,rollout_ordinal INTEGER,updated_at_ordinal INTEGER,
                   item_json TEXT,PRIMARY KEY(thread_id,item_id))''')

    @staticmethod
    def write(path, sql, args=()):
        connection = sqlite3.connect(path)
        try:
            connection.execute(sql, args)
            connection.commit()
        finally:
            connection.close()

    def thread(self, key='thread-1', actual=None):
        self.write(self.state, 'INSERT OR REPLACE INTO threads VALUES(?,?,?,?,?,0)',
                   (key, f'rollout-{actual or key}.jsonl', '测试会话', '测试会话', str(self.root / 'work')))

    def turn(self, key='turn-1', status='completed', ordinal=1, thread='thread-1'):
        self.write(self.history, 'INSERT OR REPLACE INTO thread_turns VALUES(?,?,?,?,?,?,?)',
                   (thread, key, status, None, 1800000000 + ordinal, 1800000010 + ordinal, ordinal))

    def item(self, key, item, ordinal, turn='turn-1', thread='thread-1', revision=None):
        self.write(self.history, 'INSERT OR REPLACE INTO thread_items VALUES(?,?,?,?,?,?,?,?)',
                   (thread, turn, key, item['type'], 1800000000000 + ordinal, ordinal,
                    revision if revision is not None else ordinal, json.dumps(item, ensure_ascii=False)))

    def user(self, key, body, ordinal, **options):
        self.item(key, {'type': 'userMessage', 'content': [{'type': 'text', 'text': body}]}, ordinal, **options)

    def agent(self, key, body, ordinal, phase='final_answer', **options):
        self.item(key, {'type': 'agentMessage', 'phase': phase, 'text': body}, ordinal, **options)

    def command(self, key='cmd-1', ordinal=2, status='completed', output='测试输出', exit_code=0, revision=None):
        self.item(key, {'type': 'commandExecution', 'command': 'python -m pytest', 'cwd': str(self.root),
                       'status': status, 'exitCode': exit_code, 'durationMs': 1250,
                       'aggregatedOutput': output}, ordinal, revision=revision)

    def change(self, key='file-1', ordinal=3, diff='-old\n+new', revision=None):
        self.item(key, {'type': 'fileChange', 'status': 'completed', 'changes': [
            {'path': 'D:/项目/app.py', 'kind': {'type': 'update', 'move_path': None}, 'diff': diff}]},
            ordinal, revision=revision)


def cached_chat(key='local-1', thread='thread-1', turn='turn-1'):
    return {'id': key, 'threadId': thread, 'desktopSync': True, 'desktopTurnKey': thread + ':' + turn,
            'created': '2026-10-28T10:00:00', 'cwd': '.', 'title': '旧缓存', 'mode': 'work',
            'turns': [{'prompt': '旧摘要占位', 'created': '', 'status': 'completed',
                       'output': '旧摘要', 'result': '旧摘要', 'events': []}]}


class ReaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='BailongConversation-')
        self.addCleanup(temporary.cleanup)
        self.fixture = DatabaseFixture(Path(temporary.name) / 'codex')
        self.fixture.thread()
        self.reader = ConversationReader(self.fixture.root)

    def test_all_turns_and_native_followup_are_read_in_chronological_order(self):
        for i in range(1, 6):
            self.fixture.turn(f'turn-{i}', ordinal=i)
            self.fixture.user(f'u{i}', f'问题{i}', i * 10, turn=f'turn-{i}')
            self.fixture.agent(f'a{i}', f'回复{i}', i * 10 + 1, turn=f'turn-{i}')
        result = self.reader.read(['thread-1'])
        self.assertTrue(result['available'])
        turns = result['threads']['thread-1']['turns']
        self.assertEqual(len(turns), 5)
        self.assertEqual([turn['prompt'] for turn in turns], [f'问题{i}' for i in range(1, 6)])
        self.fixture.turn('turn-6', ordinal=6)
        self.fixture.user('u6', '在 Codex 中追加', 60, turn='turn-6')
        self.fixture.agent('a6', '追加回复', 61, turn='turn-6')
        self.assertEqual(self.reader.read(['thread-1'])['threads']['thread-1']['turns'][-1]['result'], '追加回复')

    def test_same_turn_steering_and_stream_updates_are_not_duplicated(self):
        self.fixture.turn(status='inProgress')
        self.fixture.user('u1', '先做第一步', 10)
        self.fixture.agent('a1', '第一步进展', 20, phase='commentary')
        self.fixture.user('u2', '再补充一个要求', 30)
        self.fixture.agent('a2', '回复前半', 40, phase='commentary')
        self.reader.read(['thread-1'])
        self.fixture.agent('a2', '回复前半和后半', 40, phase='commentary', revision=41)
        turn = self.reader.read(['thread-1'])['threads']['thread-1']['turns'][0]
        self.assertEqual([message['role'] for message in turn['messages']], ['user', 'assistant', 'user', 'assistant'])
        self.assertEqual(turn['messages'][-1]['text'], '回复前半和后半')
        self.assertEqual(len(turn['messages']), 4)

    def test_reasoning_body_and_images_are_excluded_and_tool_output_is_lazy(self):
        self.fixture.turn()
        self.fixture.item('u1', {'type': 'userMessage', 'content': [
            {'type': 'text', 'text': '用户公开文字'}, {'type': 'image', 'url': 'PRIVATE_IMAGE_PAYLOAD'}]}, 1)
        self.fixture.item('r1', {'type': 'reasoning', 'summary': [{'text': '公开摘要'}],
                               'content': [{'text': 'PRIVATE_REASONING_BODY'}]}, 2)
        self.fixture.item('c1', {'type': 'commandExecution', 'aggregatedOutput': 'PRIVATE_TOOL_OUTPUT'}, 3)
        self.fixture.agent('a1', '公开回复', 4)
        serialized = json.dumps(self.reader.read(['thread-1']), ensure_ascii=False)
        self.assertIn('公开摘要', serialized)
        self.assertNotIn('PRIVATE_REASONING_BODY', serialized)
        self.assertNotIn('PRIVATE_IMAGE_PAYLOAD', serialized)
        self.assertNotIn('PRIVATE_TOOL_OUTPUT', serialized)

    def test_update_to_an_earlier_item_invalidates_cache_without_a_new_maximum(self):
        self.fixture.turn()
        self.fixture.user('u1', '问题', 1)
        self.fixture.agent('a1', '旧进展', 10, phase='commentary')
        self.fixture.agent('a2', '最后回复', 100)
        self.reader.read(['thread-1'])
        self.fixture.agent('a1', '更新后的进展', 10, phase='commentary', revision=11)
        messages = self.reader.read(['thread-1'])['threads']['thread-1']['turns'][0]['messages']
        self.assertEqual(messages[1]['text'], '更新后的进展')

    def test_commands_and_file_edits_share_message_order_and_details_are_lazy(self):
        self.fixture.turn()
        self.fixture.user('u1', '执行并修复', 1)
        self.fixture.command(output='只有展开才读取的输出')
        self.fixture.change(diff='-旧代码\n+只有展开才读取的新代码')
        self.fixture.agent('a1', '已处理', 4)
        result = self.reader.read(['thread-1'])
        messages = result['threads']['thread-1']['turns'][0]['messages']
        self.assertEqual([message['role'] for message in messages], ['user', 'tool', 'tool', 'assistant'])
        self.assertEqual(messages[1]['toolType'], 'commandExecution')
        self.assertEqual(messages[1]['exitCode'], 0)
        self.assertEqual(messages[2]['changes'][0]['path'], 'D:/项目/app.py')
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn('只有展开才读取的输出', serialized)
        self.assertNotIn('只有展开才读取的新代码', serialized)
        self.assertEqual(self.reader.read_tool_detail('thread-1', 'cmd-1')['output'], '只有展开才读取的输出')
        self.assertEqual(self.reader.read_tool_detail('thread-1', 'file-1')['changes'][0]['diff'],
                         '-旧代码\n+只有展开才读取的新代码')

    def test_tool_updates_replace_same_item_and_do_not_change_overall_turn_status(self):
        self.fixture.turn(status='inProgress')
        self.fixture.command(status='inProgress', exit_code=None, output='开始')
        self.reader.read(['thread-1'])
        self.fixture.command(status='completed', exit_code=1, output='调试错误', revision=5)
        turn = self.reader.read(['thread-1'])['threads']['thread-1']['turns'][0]
        self.assertEqual(turn['status'], 'running')
        self.assertEqual(len(turn['messages']), 1)
        self.assertEqual(turn['messages'][0]['exitCode'], 1)
        self.assertEqual(self.reader.read_tool_detail('thread-1', 'cmd-1')['output'], '调试错误')

    def test_long_output_keeps_both_ends_with_explicit_truncation(self):
        self.fixture.turn()
        self.fixture.command(output='开始-' + 'x' * MAX_DETAIL_CHARS + '-结束')
        detail = self.reader.read_tool_detail('thread-1', 'cmd-1')
        self.assertTrue(detail['truncated'])
        self.assertTrue(detail['output'].startswith('开始-'))
        self.assertTrue(detail['output'].endswith('-结束'))
        self.assertIn('已省略中间部分', detail['output'])

    def test_deleted_thread_does_not_expose_tool_details_from_residual_history(self):
        self.fixture.turn()
        self.fixture.command()
        self.fixture.write(self.fixture.state, 'DELETE FROM threads WHERE id=?', ('thread-1',))
        self.assertFalse(self.reader.read_tool_detail('thread-1', 'cmd-1')['available'])

    def test_added_file_details_are_not_truncated_by_command_output_limit(self):
        self.fixture.turn()
        content = '新增文件开头\n' + '内容' * (MAX_DETAIL_CHARS // 2 + 10) + '\n新增文件末尾'
        self.fixture.item('file-added', {'type': 'fileChange', 'status': 'completed', 'changes': [
            {'path': 'new.py', 'kind': {'type': 'add'}, 'diff': content}]}, 2)
        detail = self.reader.read_tool_detail('thread-1', 'file-added')
        self.assertFalse(detail['truncated'])
        self.assertEqual(detail['changes'][0]['diff'], content)

    def test_deleted_and_archived_threads_are_hidden_even_if_history_remains(self):
        self.fixture.turn()
        self.fixture.user('u1', '旧问题', 1)
        self.fixture.write(self.fixture.state, 'UPDATE threads SET archived=1 WHERE id=?', ('thread-1',))
        self.assertFalse(self.reader.read(['thread-1'])['threads']['thread-1']['visible'])
        self.fixture.write(self.fixture.state, 'DELETE FROM threads WHERE id=?', ('thread-1',))
        record = self.reader.read(['thread-1'])['threads']['thread-1']
        self.assertFalse(record['visible'])
        self.assertEqual(record['reason'], 'deleted')

    def test_missing_database_is_not_a_deleted_thread(self):
        self.fixture.state.rename(self.fixture.state.with_suffix('.offline'))
        result = self.reader.read(['thread-1'])
        self.assertFalse(result['available'])
        self.assertEqual(result['threads'], {})
        self.assertFalse(self.fixture.state.exists())

    def test_logical_thread_and_rollout_identity_are_resolved(self):
        actual = '12345678-1234-1234-1234-123456789abc'
        self.fixture.thread('logical-thread', actual)
        self.fixture.turn(thread=actual)
        self.fixture.user('u1', '原会话', 1, thread=actual)
        result = self.reader.read(['logical-thread', actual])
        self.assertEqual(result['threads'][actual]['threadId'], 'logical-thread')
        self.assertEqual(result['threads']['logical-thread']['turns'][0]['prompt'], '原会话')

    def test_ambiguous_rollout_alias_is_not_mistaken_for_deletion(self):
        actual = '12345678-1234-1234-1234-123456789abc'
        self.fixture.thread('logical-one', actual)
        self.fixture.thread('logical-two', actual)
        result = self.reader.read([actual])
        self.assertTrue(result['available'])
        self.assertNotIn(actual, result['threads'])


class MergeTests(unittest.TestCase):
    def test_only_adjacent_matching_tool_types_are_grouped(self):
        messages = [{'id': str(index), 'role': role, 'toolType': kind} for index, (role, kind) in enumerate([
            ('tool', 'commandExecution'), ('tool', 'commandExecution'),
            ('tool', 'fileChange'), ('tool', 'fileChange'),
            ('assistant', None), ('tool', 'fileChange'), ('summary', None), ('tool', 'fileChange')])]
        groups = timeline_groups(messages)
        self.assertEqual([len(group) for group in groups], [2, 2, 1, 1, 1, 1])
        self.assertEqual([message['id'] for group in groups for message in group], [message['id'] for message in messages])
    def test_per_turn_duplicates_merge_into_one_conversation_with_stable_id(self):
        first, second = cached_chat(), cached_chat('local-2', turn='turn-2')
        chats, aliases = coalesce_conversations([first, second], preferred_id='local-2')
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0]['id'], 'local-2')
        self.assertEqual([turn['codexTurnId'] for turn in chats[0]['turns']], ['turn-1', 'turn-2'])
        self.assertEqual(aliases['local-1'], 'local-2')

    def test_notification_sync_reuses_thread_and_only_excludes_own_running_turn(self):
        controller = SimpleNamespace(store=SimpleNamespace(conversations=[], now=lambda: '2026-10-28'),
            active=None, _active_removed=False, running_chat=None, running_turn=None,
            refresh_list=lambda: None, conversation_sync=SimpleNamespace(request=lambda: None))
        sync = SimpleNamespace(pet=SimpleNamespace(chat_controller=lambda: controller, chat=controller))
        for turn in ('turn-1', 'turn-2'):
            snapshot = {'thread': 'thread-1', 'turn': turn, 'title': '同一会话', 'cwd': '.',
                        'started': 1800000000, 'status': 'completed', 'final': '回复'}
            notification_history(sync, snapshot, {})
        self.assertEqual(len(controller.store.conversations), 1)
        self.assertEqual(len(controller.store.conversations[0]['turns']), 2)
        self.assertEqual(excluded_thread_ids(sync), set())
        controller.running_chat = controller.store.conversations[0]
        controller.running_turn = controller.running_chat['turns'][-1]
        self.assertEqual(excluded_thread_ids(sync), {'thread-1'})


class ControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from bridge_loader import load_original_module
        original = load_original_module()
        import chat_ui
        from PySide6.QtWidgets import QWidget
        cls.app = original.QApplication.instance() or original.QApplication(['conversation-sync-test'])
        from bailong_theme import configure_fonts
        from PySide6.QtGui import QFontDatabase
        configure_fonts(cls.app)
        mono = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts/consola.ttf'
        if mono.exists():
            QFontDatabase.addApplicationFont(str(mono))
        cls.Controller = make_controller_class(chat_ui.ChatController, background=False)
        class Pet(QWidget):
            def __init__(self):
                super().__init__()
                self.speech = []
            def say(self, *args): self.speech.append(args)
        cls.Pet = Pet

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='BailongConversationUI-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = DatabaseFixture(self.root / 'codex')
        self.fixture.thread()
        self.fixture.turn()
        self.fixture.user('u1', '第一个问题', 1)
        self.fixture.agent('a1', '第一个回复', 2)
        directory = self.root / 'pet-data'
        directory.mkdir()
        self.before = [cached_chat(), cached_chat('duplicate-2', turn='turn-2')]
        self.raw = json.dumps(self.before, ensure_ascii=False).encode('utf-8')
        (directory / 'conversations.json').write_bytes(self.raw)
        self.pet = self.Pet()
        self.controller = self.Controller(self.pet, directory=directory)
        self.controller.conversation_sync.reader = ConversationReader(self.fixture.root)
        self.addCleanup(self.dispose)

    def dispose(self):
        self.controller.shutdown()
        self.controller.window.deleteLater()
        self.controller.composer.deleteLater()
        self.pet.deleteLater()
        self.app.processEvents()

    def refresh(self):
        sync = self.controller.conversation_sync
        ids = [chat.get('threadId') for chat in self.controller.store.conversations]
        sync.apply(sync.reader.read(ids))

    def test_ui_has_one_entry_continuous_user_and_assistant_messages_and_backup(self):
        self.refresh()
        controller = self.controller
        self.assertEqual(controller.window.list.count(), 1)
        self.fixture.turn('turn-2', ordinal=2)
        self.fixture.user('u2', '原生 Codex 追加问题', 20, turn='turn-2')
        self.fixture.agent('a2', '原生追加回复', 21, turn='turn-2')
        self.refresh()
        content = controller.window.view.toPlainText()
        for value in ('第一个问题', '第一个回复', '原生 Codex 追加问题', '原生追加回复'):
            self.assertIn(value, content)
        self.assertLess(content.index('第一个回复'), content.index('原生 Codex 追加问题'))
        self.assertEqual(controller.window.list.count(), 1)
        self.assertEqual(len(controller.active['turns']), 2)
        backups = list((controller.store.directory / 'backups').glob('*.json'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.raw)

    def test_delete_removes_list_entry_without_losing_cache_or_sending_to_another_chat(self):
        self.refresh()
        self.fixture.write(self.fixture.state, 'DELETE FROM threads WHERE id=?', ('thread-1',))
        self.refresh()
        self.assertEqual(self.controller.window.list.count(), 0)
        self.assertIsNone(self.controller.active)
        self.assertTrue(self.controller.store.conversations[0]['codexHidden'])
        self.controller.submit('不应发出的草稿')
        self.assertIsNone(self.controller.worker)
        self.assertTrue(self.pet.speech)
        self.assertEqual(len(self.controller.store.conversations), 1)

    def test_unavailable_index_preserves_visible_list(self):
        self.refresh()
        self.fixture.state.rename(self.fixture.state.with_suffix('.offline'))
        self.refresh()
        self.assertEqual(self.controller.window.list.count(), 1)
        self.assertFalse(self.controller.store.conversations[0]['codexHidden'])

    def test_same_turn_user_steering_and_assistant_updates_render_in_order(self):
        self.fixture.user('u2', '<b>追加要求</b>', 3)
        self.fixture.agent('a2', '收到追加要求', 4)
        self.refresh()
        content = self.controller.window.view.toPlainText()
        values = ['第一个问题', '第一个回复', '<b>追加要求</b>', '收到追加要求']
        self.assertEqual(sorted(content.index(value) for value in values), [content.index(value) for value in values])
        before = deepcopy(self.controller.active)
        self.refresh()
        self.assertEqual(self.controller.active, before)

    def test_background_updates_preserve_reading_position(self):
        self.fixture.agent('a1', '较长的既有回复\n' * 150, 2)
        self.refresh()
        self.controller.window.show()
        self.app.processEvents()
        bar = self.controller.window.view.verticalScrollBar()
        self.assertGreater(bar.maximum(), 100)
        bar.setValue(30)
        self.fixture.user('u2', '继续追加', 3)
        self.fixture.agent('a2', '新的回复', 4)
        self.refresh()
        self.assertEqual(bar.value(), 30)

    def test_every_history_open_starts_at_latest_after_reading_old_content(self):
        self.fixture.agent('a1', '历史中的较长回复\n' * 160, 2)
        self.refresh()
        controller = self.controller
        controller.open_history()
        self.app.processEvents()
        bar = controller.window.view.verticalScrollBar()
        self.assertGreater(bar.maximum(), 100)
        self.assertEqual(bar.value(), bar.maximum())
        bar.setValue(30)
        controller._history_user_scroll()
        self.app.processEvents()
        self.assertEqual(bar.value(), 30)
        controller.window.hide()
        controller.open_history()
        self.app.processEvents()
        self.assertEqual(bar.value(), bar.maximum())

    def test_detail_click_anchor_survives_layout_and_changes_above_it(self):
        from PySide6.QtCore import QUrl
        self.fixture.agent('a1', '前面的历史内容\n' * 100, 2, phase='commentary')
        self.fixture.command(ordinal=3, output='命令输出\n' * 60)
        self.fixture.agent('a2', '后面的历史内容\n' * 100, 4)
        self.refresh()
        controller = self.controller
        controller.open_history()
        self.app.processEvents()
        view = controller.window.view
        bar = view.verticalScrollBar()
        href = 'bailong-tool:' + tool_token('thread-1', 'cmd-1')
        cursor = controller._history_link_cursor(href)
        self.assertIsNotNone(cursor)
        bar.setValue(bar.value() + view.cursorRect(cursor).top() - 80)
        before = view.cursorRect(cursor).top()
        view.anchorClicked.emit(QUrl(href))
        self.app.processEvents()
        self.assertAlmostEqual(view.cursorRect(controller._history_link_cursor(href)).top(), before, delta=2)
        self.fixture.agent('a1', '前面增加的内容\n' * 200, 2, phase='commentary', revision=8)
        self.refresh()
        self.app.processEvents()
        self.assertAlmostEqual(view.cursorRect(controller._history_link_cursor(href)).top(), before, delta=2)
        view.anchorClicked.emit(QUrl(href))
        self.app.processEvents()
        self.assertAlmostEqual(view.cursorRect(controller._history_link_cursor(href)).top(), before, delta=2)

    def test_late_first_detail_does_not_pull_view_away_from_latest_clicked_item(self):
        from PySide6.QtCore import QUrl
        self.fixture.command(ordinal=2, output='第一项输出')
        self.fixture.agent('a1', '中间的内容\n' * 100, 3, phase='commentary')
        self.fixture.command('cmd-2', ordinal=4, output='第二项输出')
        self.fixture.agent('a2', '后续内容\n' * 100, 5)
        self.refresh()
        controller = self.controller
        controller.open_history()
        self.app.processEvents()
        view = controller.window.view
        first = tool_token('thread-1', 'cmd-1')
        second_href = 'bailong-tool:' + tool_token('thread-1', 'cmd-2')
        view.anchorClicked.emit(QUrl('bailong-tool:' + first))
        self.app.processEvents()
        cursor = controller._history_link_cursor(second_href)
        bar = view.verticalScrollBar()
        bar.setValue(bar.value() + view.cursorRect(cursor).top() - 80)
        view.anchorClicked.emit(QUrl(second_href))
        self.app.processEvents()
        before = view.cursorRect(controller._history_link_cursor(second_href)).top()
        key = controller._tool_details[first]['key']
        controller.accept_tool_detail(first, key, {'available': True, 'command': '原命令', 'output': '较长输出\n' * 80})
        self.app.processEvents()
        self.assertEqual(controller._history_anchor['href'], second_href)
        self.assertAlmostEqual(view.cursorRect(controller._history_link_cursor(second_href)).top(), before, delta=2)

    def test_tool_cards_expand_collapse_and_escape_code_without_executing_it(self):
        from PySide6.QtCore import QUrl
        self.fixture.command(output='调试输出 <script>保持原样</script>', exit_code=1)
        self.fixture.change(diff='-旧代码\n+新代码 <b>保持原样</b>')
        self.refresh()
        view = self.controller.window.view
        self.assertIn('命令执行 · 执行失败', view.toPlainText())
        self.assertIn('文件编辑 · 已完成', view.toPlainText())
        self.assertNotIn('退出码：1', view.toPlainText())
        self.assertNotIn('python -m pytest', view.toPlainText())
        self.assertNotIn('D:/项目/app.py', view.toPlainText())
        self.assertNotIn('调试输出', view.toPlainText())
        token = tool_token('thread-1', 'cmd-1')
        view.anchorClicked.emit(QUrl('bailong-tool:' + token))
        self.assertIn('退出码：1', view.toPlainText())
        self.assertIn('调试输出 <script>保持原样</script>', view.toPlainText())
        self.assertIn('收起详情', view.toPlainText())
        view.anchorClicked.emit(QUrl('bailong-tool:' + token))
        self.assertNotIn('调试输出', view.toPlainText())
        view.anchorClicked.emit(QUrl('bailong-tool:' + tool_token('thread-1', 'file-1')))
        self.assertIn('新代码 <b>保持原样</b>', self.controller.file_views.toPlainText())
        panel = next(iter(self.controller.file_views.panels.values()))
        self.assertEqual([label.text() for label in panel.labels], ['原文件', '新文件'])

    def test_expanded_tool_refreshes_in_place_and_rejects_stale_detail(self):
        from PySide6.QtCore import QUrl
        self.fixture.command(status='inProgress', exit_code=None, output='初始输出')
        self.refresh()
        token = tool_token('thread-1', 'cmd-1')
        self.controller.window.view.anchorClicked.emit(QUrl('bailong-tool:' + token))
        old_key = self.controller._tool_details[token]['key']
        self.fixture.command(output='最新输出', revision=7)
        self.refresh()
        view = self.controller.window.view
        self.assertIn('最新输出', view.toPlainText())
        self.assertEqual(view.toPlainText().count('命令执行 ·'), 1)
        self.controller.accept_tool_detail(token, old_key, {'available': True, 'output': '过期输出'})
        self.assertNotIn('过期输出', view.toPlainText())
        self.assertIn('最新输出', view.toPlainText())

    def test_unknown_detail_links_do_not_read_another_item(self):
        from PySide6.QtCore import QUrl
        self.refresh()
        before = self.controller.window.view.toPlainText()
        self.controller.window.view.anchorClicked.emit(QUrl('bailong-tool:unknown'))
        self.controller.window.view.anchorClicked.emit(QUrl('file:///not-opened'))
        self.assertEqual(self.controller.window.view.toPlainText(), before)

    def test_public_summary_is_collapsed_until_clicked_and_stays_open_on_updates(self):
        from PySide6.QtCore import QUrl
        self.fixture.item('r1', {'type': 'reasoning', 'summary': [{'text': '默认不显示的摘要'}],
                               'content': [{'text': '永不显示的内部内容'}]}, 3)
        self.refresh()
        view = self.controller.window.view
        self.assertIn('思考摘要（公开）', view.toPlainText())
        self.assertNotIn('默认不显示的摘要', view.toPlainText())
        token = tool_token('summary:thread-1', 'r1')
        view.anchorClicked.emit(QUrl('bailong-summary:' + token))
        self.assertIn('默认不显示的摘要', view.toPlainText())
        self.fixture.item('r1', {'type': 'reasoning', 'summary': [{'text': '更新后的公开摘要'}],
                               'content': [{'text': '永不显示的内部内容'}]}, 3, revision=4)
        self.refresh()
        self.assertIn('更新后的公开摘要', view.toPlainText())
        self.assertNotIn('永不显示的内部内容', view.toPlainText())
        view.anchorClicked.emit(QUrl('bailong-summary:' + token))
        self.assertNotIn('更新后的公开摘要', view.toPlainText())
        self.refresh()
        self.assertNotIn('更新后的公开摘要', view.toPlainText())

    def test_legacy_live_summary_is_also_collapsed(self):
        from PySide6.QtCore import QUrl
        chat = self.controller.active
        chat['turns'] = [{'created': '2026-10-29T01:00:00', 'prompt': '问题', 'status': 'running',
                          'summary': '旧版接收的公开摘要', 'output': '正在回复', 'events': ['执行进展']}]
        self.controller.render()
        view = self.controller.window.view
        self.assertNotIn('旧版接收的公开摘要', view.toPlainText())
        self.assertNotIn('执行进展', view.toPlainText())
        token = tool_token('summary:thread-1', 'legacy:2026-10-29T01:00:00:0')
        view.anchorClicked.emit(QUrl('bailong-summary:' + token))
        self.assertIn('旧版接收的公开摘要', view.toPlainText())

    def test_tool_updates_do_not_expand_collapsed_details(self):
        self.fixture.command(output='未展开的输出')
        self.fixture.change(diff='未展开的代码差异')
        self.refresh()
        self.fixture.command(output='更新后的输出', revision=8)
        self.refresh()
        content = self.controller.window.view.toPlainText()
        for body in ('未展开的输出', '未展开的代码差异', '更新后的输出', 'python -m pytest'):
            self.assertNotIn(body, content)
        self.assertFalse(self.controller._expanded_tools)
        self.assertFalse(self.controller._tool_details)

    def test_collapsed_content_updates_do_not_reset_visible_document(self):
        from unittest.mock import patch
        self.fixture.command(output='第一段输出')
        self.fixture.item('r1', {'type': 'reasoning', 'summary': [{'text': '第一版摘要'}]}, 4)
        self.refresh()
        view = self.controller.window.view
        with patch.object(view, 'setHtml', wraps=view.setHtml) as replace_document:
            self.fixture.command(output='更新后的后台输出', revision=6)
            self.fixture.item('r1', {'type': 'reasoning', 'summary': [{'text': '更新后的摘要'}]}, 4, revision=7)
            self.refresh()
            replace_document.assert_not_called()
        self.assertNotIn('更新后的摘要', view.toPlainText())

    def test_adjacent_commands_merge_and_group_collapse_is_not_reversed_by_updates(self):
        from PySide6.QtCore import QUrl
        self.fixture.command(output='第一项输出')
        self.fixture.command('cmd-2', ordinal=3, output='第二项输出')
        self.refresh()
        view = self.controller.window.view
        self.assertEqual(view.toPlainText().count('命令执行 ·'), 1)
        self.assertIn('2 项', view.toPlainText())
        self.assertFalse(self.controller._tool_details)
        group = next(iter(self.controller._group_targets))
        view.anchorClicked.emit(QUrl('bailong-group:' + group))
        self.assertIn('第 1 项', view.toPlainText())
        self.assertNotIn('第一项输出', view.toPlainText())
        view.anchorClicked.emit(QUrl('bailong-tool:' + tool_token('thread-1', 'cmd-1')))
        self.assertIn('第一项输出', view.toPlainText())
        view.anchorClicked.emit(QUrl('bailong-group:' + group))
        self.fixture.command('cmd-2', ordinal=3, output='新输出', revision=9)
        self.refresh()
        self.assertNotIn('第一项输出', view.toPlainText())
        self.assertNotIn(group, self.controller._expanded_tool_groups)

    def test_open_group_stays_open_when_another_command_arrives(self):
        from PySide6.QtCore import QUrl
        self.fixture.command()
        self.fixture.command('cmd-2', ordinal=3)
        self.refresh()
        group = next(iter(self.controller._group_targets))
        self.controller.window.view.anchorClicked.emit(QUrl('bailong-group:' + group))
        self.fixture.command('cmd-3', ordinal=4)
        self.refresh()
        self.assertIn(group, self.controller._expanded_tool_groups)
        self.assertIn('第 3 项', self.controller.window.view.toPlainText())

    def test_assistant_message_between_commands_prevents_grouping(self):
        self.fixture.command(ordinal=2)
        self.fixture.agent('a1', '中间的对话', 3)
        self.fixture.command('cmd-2', ordinal=4)
        self.refresh()
        self.assertFalse(self.controller._group_targets)
        self.assertEqual(self.controller.window.view.toPlainText().count('命令执行 ·'), 2)

    def test_adjacent_file_edits_group_with_individual_diffs(self):
        from PySide6.QtCore import QUrl
        self.fixture.change(diff='-旧版本\n+第一次修改')
        self.fixture.change('file-2', ordinal=4, diff='-第一次修改\n+第二次修改')
        self.refresh()
        view = self.controller.window.view
        self.assertEqual(view.toPlainText().count('文件编辑 ·'), 1)
        group = next(iter(self.controller._group_targets))
        view.anchorClicked.emit(QUrl('bailong-group:' + group))
        for key in ('file-1', 'file-2'):
            view.anchorClicked.emit(QUrl('bailong-tool:' + tool_token('thread-1', key)))
        code = self.controller.file_views.toPlainText()
        self.assertLess(code.index('第一次修改'), code.index('第二次修改'))

    def test_file_comparison_shows_late_edits_and_entire_added_file(self):
        from PySide6.QtCore import QUrl
        diff = '@@ -1,1 +1,1 @@\n-开头旧值\n+开头新值\n@@ -200,1 +200,1 @@\n-末尾旧值\n+末尾新值'
        self.fixture.change(diff=diff)
        added = '\n'.join(f'新增文件完整行{i}' for i in range(60))
        self.fixture.item('file-added', {'type': 'fileChange', 'status': 'completed', 'changes': [
            {'path': 'new.py', 'kind': {'type': 'add'}, 'diff': added}]}, 4)
        self.refresh()
        view = self.controller.window.view
        group = next(iter(self.controller._group_targets))
        view.anchorClicked.emit(QUrl('bailong-group:' + group))
        for key in ('file-1', 'file-added'):
            view.anchorClicked.emit(QUrl('bailong-tool:' + tool_token('thread-1', key)))
        content = self.controller.file_views.toPlainText()
        for expected in ('开头旧值', '末尾旧值', '末尾新值', '新增文件完整行0', '新增文件完整行59'):
            self.assertIn(expected, content)
        self.assertNotIn('代码差异仅显示节选', content)
        self.controller.window.resize(1200, 1000)
        self.controller.window.show()
        self.app.processEvents()
        href = 'bailong-tool:' + tool_token('thread-1', 'file-1')
        cursor = self.controller._history_link_cursor(href)
        bar = view.verticalScrollBar()
        bar.setValue(bar.value() + view.cursorRect(cursor).top() - 20)
        folder = Path(__file__).resolve().parents[2] / 'analysis/ui-preview'
        folder.mkdir(parents=True, exist_ok=True)
        self.assertTrue(self.controller.window.grab().save(str(folder / 'file-before-after.png')))

    def test_file_code_does_not_wrap_and_bottom_horizontal_scroll_is_available(self):
        from PySide6.QtCore import QUrl, Qt
        line = 'LONG_FILE_LINE_' + 'value = value + 123; ' * 100
        self.fixture.agent('a1', '普通聊天内容继续自动换行。 ' * 100, 2)
        self.fixture.change(diff='@@ -1 +1 @@\n-' + line + '\n+' + line + 'updated')
        self.refresh()
        controller = self.controller
        controller.window.resize(900, 700)
        controller.open_history()
        controller.window.view.anchorClicked.emit(QUrl('bailong-tool:' + tool_token('thread-1', 'file-1')))
        self.app.processEvents()
        view = controller.window.view
        view.document().documentLayout().documentSize()
        self.assertEqual(view.horizontalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.assertEqual(view.horizontalScrollBar().maximum(), 0)
        self.assertGreater(view.document().find('普通聊天内容').block().layout().lineCount(), 1)
        panel = next(iter(controller.file_views.panels.values()))
        code = panel.before
        self.assertGreater(code.horizontalScrollBar().maximum(), 100)
        self.assertTrue(code.horizontalScrollBar().isVisible())
        cursor = code.document().find('LONG_FILE_LINE_')
        self.assertFalse(cursor.isNull())
        self.assertEqual(cursor.block().layout().lineCount(), 1)
        chat_x = view.cursorRect(view.document().find('第一个问题')).left()
        label_positions = [label.pos() for label in panel.labels]
        code.horizontalScrollBar().setValue(150)
        self.assertEqual(code.horizontalScrollBar().value(), 150)
        self.assertEqual(view.horizontalScrollBar().value(), 0)
        self.assertEqual(view.cursorRect(view.document().find('第一个问题')).left(), chat_x)
        self.assertEqual([label.pos() for label in panel.labels], label_positions)
        self.fixture.change(diff='@@ -1 +1 @@\n-' + line + '\n+' + line + 'latest', revision=8)
        self.refresh()
        self.app.processEvents()
        self.assertEqual(code.horizontalScrollBar().value(), 150)
        self.assertEqual(view.horizontalScrollBar().maximum(), 0)
        controller._history_user_scroll()
        cursor = controller._history_link_cursor('bailong-tool:' + tool_token('thread-1', 'file-1'))
        vertical = view.verticalScrollBar()
        vertical.setValue(vertical.value() + view.cursorRect(cursor).top() - 20)
        self.app.processEvents()
        folder = Path(__file__).resolve().parents[2] / 'analysis/ui-preview'
        folder.mkdir(parents=True, exist_ok=True)
        self.assertTrue(controller.window.grab().save(str(folder / 'file-horizontal-scroll.png')))

    def test_complete_added_file_keeps_long_source_line_unwrapped(self):
        from PySide6.QtCore import QUrl
        line = 'ADDED_LONG_LINE_' + 'source_text ' * 200
        self.fixture.item('file-added', {'type': 'fileChange', 'status': 'completed', 'changes': [
            {'path': 'new.py', 'kind': {'type': 'add'}, 'diff': line + '\n最后一行'}]}, 3)
        self.refresh()
        controller = self.controller
        controller.open_history()
        controller.window.view.anchorClicked.emit(QUrl('bailong-tool:' + tool_token('thread-1', 'file-added')))
        self.app.processEvents()
        view = controller.window.view
        view.document().documentLayout().documentSize()
        panel = next(iter(controller.file_views.panels.values()))
        self.assertGreater(panel.after.horizontalScrollBar().maximum(), 100)
        self.assertEqual(panel.after.document().find('ADDED_LONG_LINE_').block().layout().lineCount(), 1)
        self.assertIn('最后一行', panel.after.toPlainText())
        self.assertEqual(panel.before.toPlainText(), '')
        self.assertIn('原文件', panel.labels[0].text())
        self.assertIn('新文件', panel.labels[1].text())
        self.assertEqual(view.horizontalScrollBar().maximum(), 0)

    def test_file_panes_fit_viewport_after_resize_and_disappear_when_collapsed(self):
        from PySide6.QtCore import QUrl
        self.fixture.change(diff='@@ -1 +1 @@\n-' + 'old_code ' * 100 + '\n+' + 'new_code ' * 100)
        self.refresh()
        controller = self.controller
        controller.window.resize(1000, 760)
        controller.open_history()
        href = 'bailong-tool:' + tool_token('thread-1', 'file-1')
        controller.window.view.anchorClicked.emit(QUrl(href))
        self.app.processEvents()
        panel = next(iter(controller.file_views.panels.values()))
        self.assertTrue(panel.isVisible())
        for width in (800, 1200):
            controller.window.resize(width, 760)
            self.app.processEvents()
            self.app.processEvents()
            self.assertLessEqual(panel.x() + panel.width(), controller.window.view.viewport().width())
            self.assertEqual(controller.window.view.horizontalScrollBar().maximum(), 0)
            self.assertEqual([label.text() for label in panel.labels], ['原文件', '新文件'])
        controller.window.view.anchorClicked.emit(QUrl(href))
        self.app.processEvents()
        self.assertFalse(panel.isVisible())
        self.assertFalse(controller.file_views.active)

    def test_file_details_resize_keeps_latest_clicked_file_position(self):
        from PySide6.QtCore import QUrl
        self.fixture.change(diff='-old\n+new')
        self.fixture.change('file-2', ordinal=4, diff='-second old\n+second new')
        self.fixture.agent('a2', '后续对话\n' * 100, 5)
        self.refresh()
        controller = self.controller
        controller.open_history()
        view = controller.window.view
        group = next(iter(controller._group_targets))
        view.anchorClicked.emit(QUrl('bailong-group:' + group))
        first = tool_token('thread-1', 'file-1')
        second_href = 'bailong-tool:' + tool_token('thread-1', 'file-2')
        view.anchorClicked.emit(QUrl('bailong-tool:' + first))
        self.app.processEvents()
        cursor = controller._history_link_cursor(second_href)
        bar = view.verticalScrollBar()
        bar.setValue(bar.value() + view.cursorRect(cursor).top() - 80)
        view.anchorClicked.emit(QUrl(second_href))
        self.app.processEvents()
        before = view.cursorRect(controller._history_link_cursor(second_href)).top()
        controller.accept_tool_detail(first, controller._tool_details[first]['key'], {
            'available': True, 'changes': [{'kind': 'add', 'path': 'long.py', 'diff': '更多代码\n' * 100}]})
        self.app.processEvents()
        self.assertAlmostEqual(view.cursorRect(controller._history_link_cursor(second_href)).top(), before, delta=2)
        panels = [controller.file_views.panels[token] for token in controller.file_views.order]
        self.assertGreaterEqual(panels[1].y(), panels[0].y() + panels[0].height())

    def test_long_command_and_output_are_excerpts_after_expanding(self):
        from PySide6.QtCore import QUrl
        command = '\n'.join(f'print("代码第{i}行")' for i in range(40))
        output = '\n'.join(f'输出第{i}行' for i in range(50)) + '\n最后的错误原因'
        self.fixture.item('cmd-long', {'type': 'commandExecution', 'command': command,
            'status': 'completed', 'exitCode': 1, 'aggregatedOutput': output}, 3)
        self.refresh()
        view = self.controller.window.view
        self.assertNotIn('代码第0行', view.toPlainText())
        view.anchorClicked.emit(QUrl('bailong-tool:' + tool_token('thread-1', 'cmd-long')))
        content = view.toPlainText()
        self.assertIn('命令（节选）', content)
        self.assertIn('代码第0行', content)
        self.assertNotIn('代码第30行', content)
        self.assertIn('输出（节选）', content)
        self.assertNotIn('输出第25行', content)
        self.assertIn('最后的错误原因', content)

    def test_real_background_reader_loads_expanded_output_on_gui_thread(self):
        from PySide6.QtCore import QUrl
        import time
        self.fixture.command(output='后台读取的调试输出')
        self.refresh()
        self.controller.conversation_sync.close()
        sync = ConversationSync(self.controller, ConversationReader(self.fixture.root), background=True)
        self.controller.conversation_sync = sync
        try:
            token = tool_token('thread-1', 'cmd-1')
            self.controller.window.view.anchorClicked.emit(QUrl('bailong-tool:' + token))
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                sync.poll()
                self.app.processEvents()
                if '后台读取的调试输出' in self.controller.window.view.toPlainText():
                    break
                time.sleep(0.01)
            self.assertIn('后台读取的调试输出', self.controller.window.view.toPlainText())
        finally:
            sync.close()
            sync.executor.shutdown(wait=True)

    def test_tool_preview_artifact(self):
        from PySide6.QtCore import QUrl
        self.fixture.command(output='FAILED test_config.py::test_defaults\nExpected: 100\nActual: 80', exit_code=1)
        self.fixture.command('cmd-2', ordinal=3, output='检查完成')
        self.fixture.change(ordinal=4, diff='--- app.py\n+++ app.py\n@@ -1 +1 @@\n-size = 80\n+size = 100')
        self.fixture.change('file-2', ordinal=5, diff='-enabled = False\n+enabled = True')
        self.fixture.agent('a1', '已根据检查结果调整文件。', 7)
        self.refresh()
        self.controller.window.resize(1100, 900)
        self.controller.window.show()
        self.app.processEvents()
        self.fixture.item('r-preview', {'type': 'reasoning', 'summary': [{'text': '此摘要只会在手动展开后显示。'}],
                                     'content': []}, 6)
        self.refresh()
        folder = Path(__file__).resolve().parents[2] / 'analysis/ui-preview'
        folder.mkdir(parents=True, exist_ok=True)
        self.controller.window.view.verticalScrollBar().setValue(0)
        self.assertTrue(self.controller.window.grab().save(str(folder / 'chat-default-collapsed.png')))
        for token in list(self.controller._group_targets):
            self.controller.window.view.anchorClicked.emit(QUrl('bailong-group:' + token))
        for item in ('cmd-1', 'file-1'):
            self.controller.window.view.anchorClicked.emit(QUrl('bailong-tool:' + tool_token('thread-1', item)))
        self.app.processEvents()
        self.controller.window.view.verticalScrollBar().setValue(0)
        self.assertTrue(self.controller.window.grab().save(str(folder / 'chat-tools-expanded.png')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
