"""No GUI or live data: contract tests for the external animation owner."""
from types import SimpleNamespace
import unittest

from activity_animation import ExternalActivityDriver


class Timer:
    active = False
    def isActive(self):
        return self.active


class FakePet:
    def __init__(self):
        self.chat = SimpleNamespace(busy=False)
        self.toolbar = SimpleNamespace(_dragging=False, _action_menu=None)
        self.task_active = False
        self.generation = 7
        self.dragging = False
        self.quota_inflight = False
        self.manual_hidden = False
        self.fullscreen_hidden = False
        self.click_timer = Timer()
        self.bubble_timer = Timer()
        self.bubble_visible = False
        self.bubble = SimpleNamespace(isVisible=lambda: self.bubble_visible)
        self.mode = 'idle'
        self.current = {'id': 'idle', 'file': 'idle.webp'}
        self.started = 0
        self.plays = []
        self.idles = 0
        self.manifest = {
            'workAnimations': [{'id': f'work{i:02}', 'file': f'work{i:02}.webp'} for i in range(1, 6)],
            'variants': [{'id': '13', 'file': 'angry.webp'}, {'id': '18', 'file': 'celebrate.webp'}],
            'quotaAnimation': {'id': 'quota', 'file': 'quota.webp'},
        }
        self.driver = None

    def play(self, item, mode):
        self.current = item
        self.mode = mode
        self.started += 1
        self.plays.append((item['id'], mode))

    def idle(self):
        if self.driver is not None and self.driver.on_idle():
            return
        self.idles += 1
        self.mode = 'idle'
        self.current = {'id': 'idle', 'file': 'idle.webp'}
        self.started += 1

    def regular_work_animations(self):
        return self.manifest['workAnimations'][1:]

    def isVisible(self):
        return True

    def __getattr__(self, name):
        if name in ('task_started', 'task_finished', 'task_completed', 'task_result',
                    'task_progress', 'say', 'choose', 'quota'):
            raise AssertionError('External driver called forbidden original API: ' + name)
        raise AttributeError(name)


def session(state, key='task', turn='turn-1'):
    return {'key': key, 'state': state, 'turn_id': turn, 'title': 'not retained',
            'message': 'not retained', 'tool_input': 'not retained'}


class ActivityAnimationTests(unittest.TestCase):
    def setUp(self):
        self.pet = FakePet()
        self.popup = False
        self.driver = ExternalActivityDriver(None, self.pet, lambda: self.popup)
        self.pet.driver = self.driver

    def tearDown(self):
        self.assertEqual(self.pet.generation, 7)

    def test_running_starts_once_then_advances_only_on_idle(self):
        for value in range(20):
            self.driver.update([session('running')], now=value)
        self.assertEqual(self.pet.plays, [('work01', 'work-start')])
        self.pet.idle()
        self.pet.idle()
        self.assertEqual(self.pet.plays[-2:], [('work02', 'working'), ('work03', 'working')])
        self.assertFalse(self.pet.task_active)
        self.assertNotIn('title', repr(self.driver._records))

    def test_initial_completed_is_ignored(self):
        self.driver.update([session('completed')], now=0)
        self.pet.idle()
        self.assertEqual(self.pet.plays, [])

    def test_quota_result_survives_polling_and_idle_until_its_timer_expires(self):
        self.driver.update([session('running')], now=0)
        self.pet.mode = 'idle'
        self.pet.current = {'id': 'idle', 'file': 'idle.webp'}
        self.pet.started += 1
        self.pet.quota_inflight = False
        self.pet.bubble_visible = True
        self.pet.bubble_timer.active = True
        before = list(self.pet.plays)
        for tick in range(1, 12):
            self.driver.update([session('running')], now=tick)
            self.pet.idle()
        self.assertEqual(self.pet.plays, before)
        self.assertTrue(self.pet.bubble_timer.isActive())
        self.pet.bubble_visible = False
        self.pet.bubble_timer.active = False
        self.driver.update([session('running')], now=13)
        self.assertEqual(self.pet.plays[-1], ('work02', 'working'))

    def test_completion_waits_for_visible_result_instead_of_overwriting_it(self):
        self.driver.update([session('running')], now=0)
        self.pet.bubble_visible = True
        self.pet.bubble_timer.active = True
        self.driver.update([session('completed')], now=1)
        self.assertNotIn(('18', 'task-celebration'), self.pet.plays)
        self.pet.bubble_visible = False
        self.pet.bubble_timer.active = False
        self.driver.update([session('completed')], now=12)
        self.assertEqual(self.pet.plays[-1], ('18', 'task-celebration'))

    def test_reordering_concurrent_tasks_does_not_restart_the_animation(self):
        a, b = session('running', key='a'), session('running', key='b')
        self.driver.update([a, b], now=0)
        for tick in range(1, 8):
            self.driver.update([b, a] if tick % 2 else [a, b], now=tick)
        self.assertEqual(self.pet.plays, [('work01', 'work-start')])
        self.pet.idle()
        self.assertEqual(self.pet.plays[-1], ('work02', 'working'))

    def test_malformed_metadata_is_ignored_without_replaying_or_retaining_content(self):
        self.driver.update([None, {}, {'key': 'x', 'state': []}, {'key': 3, 'state': 'running'}], now=0)
        self.assertEqual(self.pet.plays, [])
        self.assertEqual(self.driver.state, 'idle')
        self.assertEqual(self.driver._records, {})

    def test_observed_completion_celebrates_once_while_card_remains(self):
        self.driver.update([session('running')], now=0)
        self.driver.update([session('completed')], now=1)
        self.assertEqual(self.pet.plays[-1], ('18', 'task-celebration'))
        for value in range(2, 10):
            self.driver.update([session('completed')], now=value)
        self.pet.idle()
        self.driver.update([session('completed')], now=10)
        self.assertEqual(sum(item == '18' for item, _ in self.pet.plays), 1)

    def test_different_turns_complete_independently(self):
        for turn in ('one', 'two'):
            self.driver.update([session('running', turn=turn)], now=0)
            self.driver.update([session('completed', turn=turn)], now=1)
            self.pet.idle()
        self.assertEqual(sum(item == '18' for item, _ in self.pet.plays), 2)

    def test_unobserved_completed_turn_does_not_celebrate(self):
        self.driver.update([session('running', turn='one')], now=0)
        self.driver.update([session('completed', turn='two')], now=1)
        self.assertFalse(any(item == '18' for item, _ in self.pet.plays))

    def test_missing_turn_id_uses_observed_cycles(self):
        for _ in range(2):
            self.driver.update([session('running', turn=None)], now=0)
            self.driver.update([session('completed', turn=None)], now=1)
            self.pet.idle()
        self.assertEqual(sum(item == '18' for item, _ in self.pet.plays), 2)

    def test_waiting_then_completion_preserves_observed_running_turn(self):
        self.driver.update([session('running')], now=0)
        self.driver.update([session('needs_input')], now=1)
        self.assertEqual(self.pet.plays[-1][0], 'work05')
        self.driver.update([session('completed')], now=2)
        self.assertEqual(self.pet.plays[-1][0], '18')

    def test_priority_waiting_failed_completion_running(self):
        self.driver.update([session('running', 'a'), session('running', 'b')], now=0)
        self.driver.update([session('completed', 'a'), session('failed', 'f'),
                            session('needs_input', 'w'), session('running', 'b')], now=1)
        self.assertEqual(self.pet.plays[-1][0], 'work05')
        self.driver.update([session('completed', 'a'), session('failed', 'f'), session('running', 'b')], now=2)
        self.assertEqual(self.pet.plays[-1][0], '13')
        self.driver.update([session('completed', 'a'), session('running', 'b')], now=3)
        self.assertEqual(self.pet.plays[-1][0], '13')  # anger finishes naturally
        self.pet.idle()
        self.assertEqual(self.pet.plays[-1][0], '18')
        self.pet.idle()
        self.assertEqual(self.pet.plays[-1][0], 'work01')

    def test_failed_animation_is_not_repeated_by_polling_or_idle(self):
        self.driver.update([session('failed')], now=0)
        for value in range(1, 5):
            self.driver.update([session('failed')], now=value)
        self.pet.idle()
        self.driver.update([session('failed')], now=5)
        self.pet.idle()
        self.assertEqual(self.pet.plays, [('13', 'variant')])

    def test_original_chat_and_task_flag_have_priority(self):
        self.pet.chat.busy = True
        self.driver.update([session('running')], now=0)
        self.pet.idle()
        self.assertEqual(self.pet.plays, [])
        self.pet.chat.busy = False
        self.pet.task_active = True
        self.driver.update([session('running')], now=1)
        self.assertEqual(self.pet.plays, [])
        self.pet.task_active = False
        self.driver.update([session('running')], now=2)
        self.assertEqual(self.pet.plays[-1][0], 'work01')

    def test_drag_landing_click_quota_and_menu_are_not_interrupted(self):
        self.pet.dragging = True
        self.pet.mode = 'drag'
        self.driver.update([session('running')], now=0)
        self.assertEqual(self.pet.plays, [])
        self.pet.dragging = False
        self.pet.mode = 'landing'
        self.driver.update([session('running')], now=1)
        self.assertEqual(self.pet.plays, [])
        self.pet.idle()
        self.assertEqual(self.pet.plays[-1][0], 'work01')
        for attribute in ('quota_inflight',):
            setattr(self.pet, attribute, True)
            self.driver.update([session('needs_input')], now=2)
            self.assertEqual(self.pet.plays[-1][0], 'work01')
            setattr(self.pet, attribute, False)
        self.pet.click_timer.active = True
        self.driver.update([session('needs_input')], now=3)
        self.pet.click_timer.active = False
        self.popup = True
        self.driver.update([session('needs_input')], now=4)
        self.assertEqual(self.pet.plays[-1][0], 'work01')
        self.popup = False
        self.driver.update([session('needs_input')], now=5)
        self.assertEqual(self.pet.plays[-1][0], 'work05')

    def test_manual_action_defers_external_completion_until_idle(self):
        self.driver.update([session('running')], now=0)
        self.pet.play({'id': 'manual', 'file': 'manual.webp'}, 'variant')
        self.driver.update([session('completed')], now=1)
        self.assertEqual(self.pet.plays[-1][0], 'manual')
        self.pet.idle()
        self.assertEqual(self.pet.plays[-1][0], '18')

    def test_unavailable_releases_only_owned_animation_and_resets_baseline(self):
        self.driver.update([session('running')], now=0)
        self.driver.update([], available=False, now=1)
        self.assertEqual(self.pet.mode, 'idle')
        self.assertEqual(self.pet.idles, 1)
        self.driver.update([], available=False, now=2)
        self.assertEqual(self.pet.idles, 1)
        self.driver.update([session('completed')], now=3)
        self.assertFalse(any(item == '18' for item, _ in self.pet.plays))

    def test_inactive_does_not_touch_original_or_manual_animation(self):
        self.driver.update([session('running')], now=0)
        self.pet.play({'id': 'work01', 'file': 'work01.webp'}, 'work-start')
        self.pet.chat.busy = True
        self.driver.update([], active=False, now=1)
        self.assertEqual(self.pet.idles, 0)
        self.assertEqual(self.pet.mode, 'work-start')
        self.pet.chat.busy = False
        self.pet.play({'id': 'manual', 'file': 'manual.webp'}, 'variant')
        self.driver.update([], active=False, now=2)
        self.assertEqual(self.pet.idles, 0)
        self.assertEqual(self.pet.mode, 'variant')

    def test_empty_source_restores_idle_without_repeated_restore_calls(self):
        self.driver.update([session('running')], now=0)
        self.driver.update([], now=1)
        self.driver.update([], now=2)
        self.assertEqual(self.pet.idles, 1)
        self.assertEqual(self.pet.mode, 'idle')

    def test_pending_completion_expires_without_late_surprise(self):
        self.driver.update([session('running')], now=0)
        self.pet.play({'id': 'manual', 'file': 'manual.webp'}, 'variant')
        self.driver.update([session('completed')], now=1)
        self.driver.update([session('completed')], now=62)
        self.pet.idle()
        self.assertFalse(any(item == '18' for item, _ in self.pet.plays))


if __name__ == '__main__':
    unittest.main()
