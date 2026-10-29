"""Mock Desktop/stdio only: these tests never open chats or submit real work."""
from collections import deque
from types import SimpleNamespace
import threading
import unittest

from desktop_chat_link import make_session_class
from desktop_ipc import DesktopUnavailable, DesktopOutcomeUnknown


class Cancelled(RuntimeError):
    pass


class OriginalSession:
    def __init__(self, cwd, thread_id=None, cancel=None, emit=None, ask=None, mode="work"):
        self.cwd, self.thread_id, self.mode = cwd, thread_id, mode
        self.cancel, self.emit = cancel or threading.Event(), emit or (lambda *args: None)
        self.process = None
        self.original_calls = []
        self.turn_id = None

    def start(self):
        self.original_calls.append(("start", None))
        self.process = object()

    def send(self, payload):
        self.original_calls.append(("send", payload))

    def rpc(self, method, params, timeout=45):
        self.original_calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "seed-thread"}}
        return {}

    def close(self):
        self.original_calls.append(("close", None))
        self.process = None

    def run(self, prompt):
        raise AssertionError("Legacy submission must never be used")


def snapshot(thread_id="thread-1", revision=1, turn_id="old", status="completed", text="old output"):
    return {"type": "snapshot", "revision": revision, "conversationState": {
        "id": thread_id, "hostId": "local", "requests": [], "turns": [
            {"turnId": turn_id, "status": status, "items": [
                {"type": "agentMessage", "id": "a", "text": text, "phase": "final_answer"}]}]}}


class FakeClock:
    value = 0.0
    def __call__(self):
        return self.value
    def sleep(self, seconds):
        self.value += seconds


class Ipc:
    def __init__(self, clock, thread_id="thread-1"):
        self.clock, self.thread_id = clock, thread_id
        self.changes = deque([snapshot(thread_id)])
        self.calls, self.closed = [], False
        self.owner = "desktop-owner"
        self.start_error = None
        self.after_send = [snapshot(thread_id, 2, "new-turn", "inProgress", "live output"),
                           snapshot(thread_id, 3, "new-turn", "completed", "final output")]
        self.receive_error = None

    def connect(self, timeout):
        self.calls.append(("connect", timeout))
        return self
    def discover_owner(self, thread_id, timeout):
        self.calls.append(("discover", thread_id))
        return self.owner
    def subscribe(self, thread_id, owner):
        self.calls.append(("subscribe", thread_id, owner))
    def next_change(self, thread_id, timeout):
        self.clock.sleep(timeout)
        if self.receive_error and not self.changes:
            raise self.receive_error
        return self.changes.popleft() if self.changes else None
    def start_turn(self, thread_id, prompt, **options):
        self.calls.append(("start_turn", thread_id, prompt, options))
        if self.start_error:
            raise self.start_error
        self.changes.extend(self.after_send)
        return {"turn": {"id": "new-turn"}}
    def request_snapshot(self, thread_id):
        self.calls.append(("snapshot", thread_id))
    def interrupt(self, thread_id, turn_id):
        self.calls.append(("interrupt", thread_id, turn_id))
    def close(self):
        self.closed = True


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.events, self.opened = FakeClock(), [], []
        self.ipc = Ipc(self.clock)
        self.backend = SimpleNamespace(CodexSession=OriginalSession, VERSION="test", PERSONA="original persona", TaskCancelled=Cancelled)

    def create(self, thread_id="thread-1", **options):
        klass = make_session_class(self.backend, client_factory=lambda: self.ipc,
                  open_thread=self.opened.append, clock=self.clock, sleep=self.clock.sleep,
                  owner_timeout=1, snapshot_timeout=1, inactivity_timeout=2)
        return klass("fixture-cwd", thread_id, emit=lambda *event: self.events.append(event), **options)

    def test_existing_thread_stays_same_and_gui_executes_once(self):
        session = self.create()
        result = session.run("user message")
        self.assertEqual(result, {"threadId": "thread-1", "text": "final output", "transcript": "final output"})
        self.assertEqual(self.opened, ["thread-1"])
        sends = [call for call in self.ipc.calls if call[0] == "start_turn"]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][1:3], ("thread-1", "user message"))
        self.assertFalse(any(method in ("thread/start", "turn/start", "start") for method, _ in session.original_calls))
        self.assertIn(("output", "live output"), self.events)
        self.assertNotIn(("output", "old output"), self.events)
        self.assertTrue(self.ipc.closed)

    def test_new_thread_bootstrap_never_submits_stdio_turn(self):
        self.ipc = Ipc(self.clock, "seed-thread")
        session = self.create(None)
        result = session.run("new message")
        methods = [name for name, _ in session.original_calls]
        self.assertEqual(methods.count("thread/start"), 1)
        self.assertNotIn("turn/start", methods)
        self.assertIn("thread/name/set", methods)
        self.assertEqual(result["threadId"], "seed-thread")
        self.assertIn(("thread", "seed-thread"), self.events)
        params = next(value for name, value in session.original_calls if name == "thread/start")
        self.assertEqual(params["developerInstructions"], "original persona")
        self.assertEqual(params["approvalPolicy"], "on-request")
        self.assertEqual(params["sandbox"], "workspace-write")
        self.assertIs(params["ephemeral"], False)
        self.assertIsNone(session.process)

    def test_missing_gui_fails_before_seed_or_message(self):
        def fail(timeout):
            raise DesktopUnavailable("offline")
        self.ipc.connect = fail
        session = self.create(None)
        with self.assertRaisesRegex(RuntimeError, "消息没有发送"):
            session.run("do not send")
        self.assertFalse(any(call[0] == "thread/start" for call in session.original_calls))
        self.assertFalse(any(call[0] == "start_turn" for call in self.ipc.calls))

    def test_name_failure_does_not_publish_bad_id_and_retry_can_create(self):
        self.ipc = Ipc(self.clock, "seed-thread")
        session = self.create(None)
        original_rpc = session.rpc
        fail_name = [True]
        def rpc(method, params, timeout=45):
            if method == "thread/name/set" and fail_name[0]:
                raise RuntimeError("synthetic persistence failure")
            return original_rpc(method, params, timeout=timeout)
        session.rpc = rpc
        with self.assertRaisesRegex(RuntimeError, "消息没有发送"):
            session.run("first attempt")
        self.assertIsNone(session.thread_id)
        self.assertIsNone(session.process)
        self.assertFalse(any(event[0] == "thread" for event in self.events))
        self.assertFalse(any(call[0] == "start_turn" for call in self.ipc.calls))
        self.assertEqual(self.opened, [])
        fail_name[0] = False
        result = session.run("explicit user retry")
        self.assertEqual(result["threadId"], "seed-thread")
        self.assertEqual(len([call for call in self.ipc.calls if call[0] == "start_turn"]), 1)

    def test_no_owner_preserves_seed_and_does_not_send(self):
        self.ipc = Ipc(self.clock, "seed-thread")
        self.ipc.owner = None
        session = self.create(None)
        with self.assertRaisesRegex(RuntimeError, "消息没有发送"):
            session.run("later")
        self.assertEqual(session.thread_id, "seed-thread")
        self.assertFalse(any(call[0] == "start_turn" for call in self.ipc.calls))

    def test_no_initial_snapshot_never_sends(self):
        self.ipc.changes.clear()
        session = self.create()
        with self.assertRaisesRegex(RuntimeError, "消息没有发送"):
            session.run("later")
        self.assertFalse(any(call[0] == "start_turn" for call in self.ipc.calls))

    def test_existing_desktop_turn_is_not_interrupted_or_overlapped(self):
        for kind in ("turn", "runtime", "pending"):
            self.setUp()
            value = snapshot(status="inProgress" if kind == "turn" else "completed")
            if kind == "runtime":
                value["conversationState"]["threadRuntimeStatus"] = {"type": "active"}
            if kind == "pending":
                value["conversationState"]["unconfirmedTurnSubmissions"] = [{"terminal": False}]
            self.ipc.changes = deque([value])
            session = self.create()
            with self.assertRaisesRegex(RuntimeError, "消息尚未发送"):
                session.run("do not overlap")
            self.assertFalse(any(call[0] in ("start_turn", "interrupt") for call in self.ipc.calls))

    def test_uncertain_send_is_not_replayed(self):
        self.ipc.start_error = DesktopOutcomeUnknown("lost response")
        session = self.create()
        with self.assertRaisesRegex(RuntimeError, "避免重复发送"):
            session.run("exactly once")
        self.assertEqual(len([c for c in self.ipc.calls if c[0] == "start_turn"]), 1)
        self.assertFalse(any(name == "turn/start" for name, _ in session.original_calls))

    def test_disconnect_after_send_preserves_uncertain_status(self):
        self.ipc.after_send = []
        self.ipc.receive_error = DesktopUnavailable("disconnected")
        session = self.create()
        with self.assertRaisesRegex(RuntimeError, "避免重复发送"):
            session.run("once")
        self.assertEqual(len([c for c in self.ipc.calls if c[0] == "start_turn"]), 1)

    def test_revision_gap_requests_snapshot_without_resending(self):
        self.ipc.after_send = [
            {"type": "patches", "revision": 9, "baseRevision": 8, "patches": []},
            snapshot("thread-1", 10, "new-turn", "completed", "resynced result")]
        session = self.create()
        self.assertEqual(session.run("once")["text"], "resynced result")
        self.assertIn(("snapshot", "thread-1"), self.ipc.calls)
        self.assertEqual(len([c for c in self.ipc.calls if c[0] == "start_turn"]), 1)

    def test_failure_and_interrupt_do_not_claim_success(self):
        for status in ("failed", "interrupted"):
            self.setUp()
            self.ipc.after_send = [snapshot("thread-1", 2, "new-turn", status)]
            session = self.create()
            with self.assertRaises(Cancelled if status == "interrupted" else RuntimeError):
                session.run("request")

    def test_cancel_before_send_does_not_interrupt_or_send(self):
        cancellation = threading.Event()
        cancellation.set()
        session = self.create(cancel=cancellation)
        with self.assertRaises(Cancelled):
            session.run("request")
        self.assertFalse(any(c[0] in ("start_turn", "interrupt") for c in self.ipc.calls))

    def test_cancel_after_send_targets_exact_turn(self):
        cancellation = threading.Event()
        session = self.create(cancel=cancellation)
        original_receive = self.ipc.next_change
        def receive(*args, **kwargs):
            change = original_receive(*args, **kwargs)
            if change and change["revision"] == 2:
                cancellation.set()
            return change
        self.ipc.next_change = receive
        with self.assertRaises(Cancelled):
            session.run("request")
        self.assertIn(("interrupt", "thread-1", "new-turn"), self.ipc.calls)

    def test_inactivity_times_out_without_resending(self):
        self.ipc.after_send = []
        session = self.create()
        with self.assertRaisesRegex(RuntimeError, "避免重复发送"):
            session.run("request")
        self.assertEqual(len([c for c in self.ipc.calls if c[0] == "start_turn"]), 1)


if __name__ == "__main__":
    unittest.main()
