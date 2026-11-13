import json
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import activity_runtime as runtime_module
from activity_runtime import ActivityReducer, ActivityRuntime, apply_ui_hints


def task(state, key='a', turn='t1', initial=False, title='项目甲'):
    return {'key': key, 'turn_id': turn, 'state': state, 'title': title, 'initial_visible': initial}


class ActivityRuntimeTests(unittest.TestCase):
    def test_click_only_opens_known_native_chat_routes(self):
        runtime = ActivityRuntime.__new__(ActivityRuntime)
        runtime.active = runtime.available = True
        runtime.routes = {'valid': 'codex://threads/abc-123', 'web': 'https://example.com',
                          'escape': 'codex://threads/%2e%2e%2fsecret', 'blank': 'codex://threads/'}
        opened = []
        runtime.opener = lambda uri: opened.append(uri) or True
        self.assertTrue(runtime.activate('valid'))
        for key in ('web', 'escape', 'blank', 'missing'):
            self.assertFalse(runtime.activate(key))
        runtime.available = False
        self.assertFalse(runtime.activate('valid'))
        self.assertEqual(opened, ['codex://threads/abc-123'])

    def test_no_historical_completion_spam(self):
        reducer = ActivityReducer()
        self.assertEqual(reducer.accept([task('completed')], 0), [])

    def test_observed_completion_is_visible_then_expires(self):
        reducer = ActivityReducer()
        self.assertEqual(len(reducer.accept([task('running')], 0)), 1)
        self.assertEqual(len(reducer.accept([task('completed')], 2)), 1)
        self.assertEqual(len(reducer.accept([task('completed')], 61)), 1)
        self.assertEqual(reducer.accept([task('completed')], 63), [])

    def test_new_turn_does_not_inherit_completion(self):
        reducer = ActivityReducer()
        reducer.accept([task('running')], 0)
        self.assertEqual(reducer.accept([task('completed', turn='t2')], 2), [])

    def test_failure_and_initial_unread_are_shown(self):
        reducer = ActivityReducer()
        self.assertEqual(len(reducer.accept([task('failed', initial=True)], 0)), 1)

    def test_unavailable_source_is_unknown_not_completed(self):
        reducer = ActivityReducer()
        reducer.accept([task('running')], 0)
        self.assertEqual(reducer.unknown()[0]['state'], 'unknown')
        self.assertEqual(reducer.cards[0]['state'], 'running')

    def test_ui_hints_supplement_running_and_promote_old_states(self):
        hints = [{'title': '项目甲', 'state': 'needs_input'}]
        self.assertEqual(apply_ui_hints([task('running')], hints)[0]['state'], 'needs_input')
        for state in ('idle', 'completed', 'failed', 'unknown'):
            with self.subTest(state=state):
                original = dict(task(state), routeUri='codex://threads/root-task')
                updated = apply_ui_hints([original], hints)[0]
                self.assertEqual(updated['state'], 'needs_input')
                self.assertEqual(updated['stateEvidence'], 'native-ui-label')
                self.assertTrue(updated['activity_token'].startswith('native-ui:'))
                for name in ('key', 'turn_id', 'title', 'routeUri'):
                    self.assertEqual(updated[name], original[name])
                self.assertEqual(original['state'], state, 'source metadata must remain unchanged')
        duplicate = [task('running'), task('running', key='b')]
        self.assertTrue(all(row['state'] == 'running' for row in apply_ui_hints(duplicate, hints)))

    def test_old_interrupted_root_is_visible_with_explicit_native_running_evidence(self):
        root = dict(task('idle', key='local:3d-root', title='示例旧索引任务'),
                    routeUri='codex://threads/3d-root')
        updated = apply_ui_hints([root], [{'title': root['title'], 'state': 'running'}])
        visible = ActivityReducer().accept(updated, 1)
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]['state'], 'running')
        self.assertEqual(visible[0]['key'], root['key'])
        self.assertEqual(visible[0]['turn_id'], root['turn_id'])
        self.assertEqual(visible[0]['routeUri'], root['routeUri'])

    def test_without_supported_native_activity_old_roots_are_not_revived(self):
        for state in ('idle', 'completed', 'failed', 'unknown'):
            for hint_state in (None, 'completed', 'idle', 'unsupported', 'failed'):
                with self.subTest(state=state, hint_state=hint_state):
                    original = task(state)
                    hints = [] if hint_state is None else [{'title': original['title'], 'state': hint_state}]
                    updated = apply_ui_hints([original], hints)[0]
                    self.assertEqual(updated, original)
                    self.assertEqual(ActivityReducer().accept([updated], 1), [])

    def test_duplicate_or_unnamed_titles_cannot_be_promoted(self):
        duplicate = [task('idle'), task('completed', key='b')]
        hints = [{'title': '项目甲', 'state': 'running'}]
        self.assertEqual(apply_ui_hints(duplicate, hints), duplicate)
        for title in ('', ' ', '未命名任务'):
            original = task('idle', title=title)
            self.assertEqual(apply_ui_hints([original], [{'title': title, 'state': 'running'}]), [original])
        conflicting = [{'title': '项目甲', 'state': 'running'}, {'title': '项目甲', 'state': 'needs_input'}]
        self.assertEqual(apply_ui_hints([task('idle')], conflicting), [task('idle')])

    def test_running_metadata_and_native_failure_keep_existing_completion_behavior(self):
        original = task('running')
        self.assertEqual(apply_ui_hints([original], []), [original])
        reducer = ActivityReducer()
        reducer.accept([original], 0)
        failed = apply_ui_hints([original], [{'title': original['title'], 'state': 'failed'}])[0]
        self.assertNotIn('activity_token', failed)
        self.assertEqual(reducer.accept([failed], 1)[0]['state'], 'failed')

    def test_native_promotion_expiry_never_replays_old_terminal_state(self):
        for state in ('completed', 'failed'):
            for initial in (False, True):
                with self.subTest(state=state, initial=initial):
                    original = task(state, initial=initial)
                    promoted = apply_ui_hints([original], [{'title': original['title'], 'state': 'running'}])
                    reducer = ActivityReducer()
                    self.assertEqual(len(reducer.accept(promoted, 0)), 1)
                    self.assertEqual(reducer.accept([original], 1), [])
                    self.assertEqual(reducer.accept([original], 2), [])
                    self.assertEqual(reducer.terminal_until, {})

    def test_index_catching_up_to_running_restores_real_completion_tracking(self):
        reducer = ActivityReducer()
        old = task('completed', initial=True)
        reducer.accept(apply_ui_hints([old], [{'title': old['title'], 'state': 'running'}]), 0)
        reducer.accept([task('running')], 1)
        self.assertEqual(reducer.accept([task('completed')], 2)[0]['state'], 'completed')
        self.assertEqual(reducer.accept([task('completed')], 63), [])


class NativeHintCollectionTests(unittest.TestCase):
    def setUp(self):
        self.runtime = ActivityRuntime.__new__(ActivityRuntime)
        self.runtime._ui_cache = []
        self.runtime._ui_at = 0.0
        self.runtime._ui_context = None
        self.rows = [task('idle', key='local:root', title='示例旧索引任务')]
        self.now = 100.0
        self.snapshot_patch = patch.object(runtime_module, 'snapshot', side_effect=lambda **kw: {
            'available': True, 'sessions': [dict(row) for row in self.rows]})
        self.snapshot_patch.start()
        self.addCleanup(self.snapshot_patch.stop)
        self.clock_patch = patch.object(runtime_module.time, 'monotonic', side_effect=lambda: self.now)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)

    def response(self, payload=None):
        if payload is None:
            payload = {'available': True, 'hints': [{'title': self.rows[0]['title'], 'state': 'running'}]}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload).encode('utf-8'))

    def test_query_covers_unique_known_roots_in_every_state_and_keeps_existing_limits(self):
        self.rows += [task(state, key=state, title=state) for state in ('running', 'completed', 'failed', 'unknown')]
        self.rows += [task('idle', key='duplicate-a', title='same'), task('idle', key='duplicate-b', title='same'),
                      task('idle', key='unnamed', title='未命名任务'), task('idle', key='blank', title='')]
        with patch.object(runtime_module.subprocess, 'run', return_value=self.response()) as run:
            result = self.runtime._collect(123, 90.0)
        sent = json.loads(run.call_args.kwargs['input'])['titles']
        self.assertEqual(set(sent), {'示例旧索引任务', 'running', 'completed', 'failed', 'unknown'})
        self.assertEqual(run.call_args.kwargs['timeout'], 2.5)
        self.assertEqual(result['uiPromotionCount'], 1)
        self.assertEqual(result['sessions'][0]['state'], 'running')
        self.assertEqual(result['sessions'][0]['turn_id'], 't1')

    def test_native_query_is_bounded_by_root_limit(self):
        self.rows = [task('idle', key=str(i), title=f'root-{i:03}') for i in range(runtime_module.MAX_ROOTS + 10)]
        with patch.object(runtime_module.subprocess, 'run', return_value=self.response()) as run:
            self.runtime._collect(123, 90.0)
        self.assertEqual(len(json.loads(run.call_args.kwargs['input'])['titles']), runtime_module.MAX_ROOTS)

    def test_expired_cache_is_cleared_on_timeout_unavailable_or_invalid_response(self):
        failures = (subprocess.TimeoutExpired('fixture', 2.5), self.response({'available': False, 'hints': []}),
                    self.response([]), self.response({'available': True, 'hints': 'invalid'}))
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                self.runtime._ui_context = None
                self.now = 100.0
                with patch.object(runtime_module.subprocess, 'run', return_value=self.response()) as run:
                    self.assertEqual(self.runtime._collect(123, 90.0)['uiPromotionCount'], 1)
                    self.now = 103.9
                    self.assertEqual(self.runtime._collect(123, 90.0)['uiPromotionCount'], 1)
                    self.assertEqual(run.call_count, 1)
                self.now = 104.0
                kwargs = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
                with patch.object(runtime_module.subprocess, 'run', **kwargs):
                    result = self.runtime._collect(123, 90.0)
                self.assertEqual(result['sessions'][0]['state'], 'idle')
                self.assertEqual(result['uiPromotionCount'], 0)
                self.assertEqual(self.runtime._ui_cache, [])

    def test_cache_never_crosses_host_birth_or_thread_identity(self):
        with patch.object(runtime_module.subprocess, 'run', return_value=self.response()) as run:
            self.runtime._collect(123, 90.0)
            self.now = 101.0
            self.runtime._collect(123, 91.0)
            self.assertEqual(run.call_count, 2, 'same PID with new host birth requires a new native observation')
            self.rows[0]['key'] = 'local:different-root-with-same-title'
            self.runtime._collect(123, 91.0)
            self.assertEqual(run.call_count, 3, 'a reused title must not inherit another root hint')

    def test_no_unique_titles_never_queries_native_ui(self):
        self.rows = [task('idle', title='未命名任务')]
        with patch.object(runtime_module.subprocess, 'run') as run:
            result = self.runtime._collect(123, 90.0)
        run.assert_not_called()
        self.assertEqual(result['uiPromotionCount'], 0)


if __name__ == '__main__':
    unittest.main()
