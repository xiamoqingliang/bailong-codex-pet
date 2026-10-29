import unittest

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

    def test_ui_hints_only_override_unique_running_titles(self):
        hints = [{'title': '项目甲', 'state': 'needs_input'}]
        self.assertEqual(apply_ui_hints([task('running')], hints)[0]['state'], 'needs_input')
        self.assertEqual(apply_ui_hints([task('completed')], hints)[0]['state'], 'completed')
        duplicate = [task('running'), task('running', key='b')]
        self.assertTrue(all(row['state'] == 'running' for row in apply_ui_hints(duplicate, hints)))


if __name__ == '__main__':
    unittest.main()
