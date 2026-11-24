"""Native composer readback and delivery policy with a simulated Codex index."""
from threading import Event
import unittest

from desktop_chat_window import DesktopChatWindow, DesktopWindowRejected
from desktop_ipc import DesktopOutcomeUnknown


def turn(turn_id, prompt, *, status="completed"):
    return {"codexTurnId": turn_id, "status": status, "error": "",
            "messages": [{"role": "user", "text": prompt}]}


class History:
    def __init__(self):
        self.turns = []
        self.cache = {}

    def read(self, thread_ids):
        return {"available": True, "threads": {thread_id: {
            "threadId": thread_id, "visible": True, "turns": self.turns,
        } for thread_id in thread_ids}}


class NativeTurnTests(unittest.TestCase):
    def setUp(self):
        self.reader = History()
        self.session = DesktopChatWindow(Event(), lambda thread_id: None, reader=self.reader)
        self.session.follow("test-thread")

    def test_native_markdown_confirms_only_the_new_turn(self):
        self.reader.turns.append(turn("old-turn", "中文\n[下一行]"))
        before = self.session.next_change("test-thread")
        self.assertEqual(before["conversationState"]["turns"][0]["id"], "old-turn")

        def invoke(operation, before_commit, *, prompt):
            self.assertEqual(operation, "send")
            self.assertEqual(prompt, "中文\r\n[下一行]")
            before_commit()
            self.reader.turns.append(turn("new-turn", "中文\\\n\\[下一行\\]\n"))

        self.session._invoke = invoke
        result = self.session.start_turn("test-thread", "中文\r\n[下一行]")
        self.assertEqual(result, {"turn": {"id": "new-turn"}})
        self.assertEqual(self.session.turn_id, "new-turn")

    def test_unconfirmed_invocation_is_never_repeated(self):
        count = []

        def invoke(operation, before_commit, *, prompt):
            before_commit()
            count.append(operation)

        self.session._invoke = invoke
        with self.assertRaises(DesktopOutcomeUnknown):
            self.session.start_turn("test-thread", "not recorded", timeout=0)
        self.assertEqual(count, ["send"])
        self.assertIsNone(self.session.turn_id)

    def test_active_chat_rejects_commit(self):
        self.reader.turns.append(turn("active-turn", "another message", status="running"))

        def invoke(operation, before_commit, *, prompt):
            before_commit()
            self.fail("The native composer would be invoked during a running turn")

        self.session._invoke = invoke
        with self.assertRaisesRegex(DesktopWindowRejected, "仍在"):
            self.session.start_turn("test-thread", "new message")
