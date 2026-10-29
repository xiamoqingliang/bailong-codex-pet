"""Synthetic Desktop stream fixtures only; no real chats or app connections."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from desktop_chat_adapter import DesktopStreamReducer, DesktopStreamError, NeedsSnapshot, apply_patches, install_session_class


def turn(turn_id="turn-new", status="inProgress", text="正在处理", phase="commentary"):
    return {"turnId": turn_id, "status": status, "items": [
        {"type": "userMessage", "id": "user", "content": [{"type": "text", "text": "用户输入不能成为回答"}]},
        {"type": "agentMessage", "id": "answer", "text": text, "phase": phase},
    ]}


def snapshot(value=None, revision=1):
    return {"type": "snapshot", "revision": revision, "conversationState": {
        "id": "thread-1", "hostId": "local", "turns": [value or turn()], "requests": []}}


def patches(values, base=1, revision=2):
    return {"type": "patches", "baseRevision": base, "revision": revision, "patches": values}


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.reader = DesktopStreamReducer("thread-1", lambda kind, value: self.events.append((kind, value)))

    def test_binding_required_and_old_completed_turn_does_not_finish_new_turn(self):
        self.reader.apply_change(snapshot(turn("old", "completed", "旧回复", "final_answer")))
        self.assertEqual(self.events, [])
        self.reader.bind_turn("turn-new")
        self.assertFalse(self.reader.terminal)
        self.assertNotIn("旧回复", repr(self.events))
        with self.assertRaises(DesktopStreamError):
            self.reader.result()

    def test_snapshot_then_delta_completion_returns_compatible_result(self):
        self.reader.bind_turn("turn-new")
        self.reader.apply_change(snapshot())
        self.reader.apply_change(patches([
            {"op": "replace", "path": ["turns", 0, "items", 1, "text"], "value": "处理完成"},
            {"op": "replace", "path": ["turns", 0, "items", 1, "phase"], "value": "final_answer"},
            {"op": "replace", "path": ["turns", 0, "status"], "value": "completed"},
        ]))
        self.assertTrue(self.reader.completed)
        self.assertEqual(self.reader.result(), {"threadId": "thread-1", "text": "处理完成", "transcript": "处理完成"})
        self.assertEqual([event[1] for event in self.events if event[0] == "output"], ["正在处理", "处理完成"])

    def test_canonical_history_and_streaming_patches(self):
        value = snapshot(turn("old", "completed", "不输出"))
        value["conversationState"]["turnHistory"] = {"kind": "canonical", "history": {
            "entitiesByKey": {"turn:turn-new": turn()}, "islands": [{"entries": [{"value": "turn:turn-new"}]}]}}
        self.reader.bind_turn("turn-new")
        self.reader.apply_change(value)
        self.reader.apply_change(patches([{"op": "replace", "path": ["turnHistory", "history", "entitiesByKey", "turn:turn-new", "items", 1, "text"], "value": "实时答案"}]))
        self.assertEqual(self.events[-1], ("output", "实时答案"))

    def test_canonical_nonstandard_key_and_transient_fallback(self):
        value = snapshot()
        value["conversationState"]["turnHistory"] = {"kind": "canonical", "history": {
            "entitiesByKey": {"entity-x": turn("old", "completed", "旧")}, "islands": [{"entries": [{"value": "entity-x"}]}]}}
        self.reader.bind_turn("turn-new")
        self.reader.apply_change(value)
        self.assertIn(("output", "正在处理"), self.events)
        self.reader.bind_turn("old")
        self.assertTrue(self.reader.completed)

    def test_reasoning_content_and_other_turns_never_emitted(self):
        value = snapshot()
        value["conversationState"]["turns"].insert(0, turn("other", "completed", "OTHER_SECRET"))
        value["conversationState"]["turns"][1]["items"].append({"type": "reasoning", "summary": ["公开摘要", {"text": "第二段"}], "content": ["RAW_REASONING_SECRET"], "encryptedContent": "ENCRYPTED_SECRET"})
        self.reader.bind_turn("turn-new")
        self.reader.apply_change(value)
        self.assertIn(("summary_update", "公开摘要\n\n第二段"), self.events)
        self.assertNotIn("SECRET", repr(self.events))
        self.assertNotIn("用户输入", repr(self.events))

    def test_final_answer_separated_from_commentary(self):
        value = snapshot(turn(status="completed"))
        value["conversationState"]["turns"][0]["items"].extend([
            {"id": "final", "type": "agentMessage", "text": "最终回复", "phase": "final_answer"},
        ])
        self.reader.bind_turn("turn-new")
        self.reader.apply_change(value)
        self.assertEqual(self.reader.result()["text"], "最终回复")
        self.assertEqual(self.reader.result()["transcript"], "正在处理\n\n最终回复")

    def test_gap_and_patch_without_snapshot_require_resubscribe(self):
        with self.assertRaises(NeedsSnapshot):
            self.reader.apply_change(patches([]))
        self.reader.apply_change(snapshot())
        with self.assertRaises(NeedsSnapshot):
            self.reader.apply_change(patches([], base=7, revision=8))
        self.assertEqual(self.reader.revision, 1)
        self.reader.apply_change(snapshot(revision=9))
        self.assertEqual(self.reader.revision, 9)

    def test_duplicates_do_not_reemit_or_regress(self):
        self.reader.bind_turn("turn-new")
        self.reader.apply_change(snapshot())
        before = deepcopy(self.events)
        self.assertFalse(self.reader.apply_change(snapshot(revision=1)))
        self.assertFalse(self.reader.apply_change(snapshot(revision=0)))
        self.assertEqual(self.events, before)

    def test_other_thread_and_host_rejected_without_mutation(self):
        self.reader.apply_change(snapshot())
        for key, value in (("id", "someone-else"), ("hostId", "cloud")):
            changed = snapshot(revision=2)
            changed["conversationState"][key] = value
            with self.assertRaises(DesktopStreamError):
                self.reader.apply_change(changed)
            self.assertEqual(self.reader.revision, 1)

    def test_failed_and_interrupted_are_not_success(self):
        for status in ("failed", "interrupted"):
            self.setUp()
            value = snapshot(turn(status=status))
            value["conversationState"]["turns"][0]["error"] = {"message": "需要处理的错误"}
            self.reader.bind_turn("turn-new")
            self.reader.apply_change(value)
            self.assertTrue(self.reader.terminal)
            self.assertFalse(self.reader.completed)
            self.assertEqual(self.reader.error, "需要处理的错误")
            with self.assertRaises(DesktopStreamError):
                self.reader.result()

    def test_progress_and_pending_approval_are_deduplicated(self):
        value = snapshot()
        value["conversationState"]["turns"][0]["items"].extend([
            {"type": "commandExecution", "id": "cmd", "command": "test-command", "status": "inProgress", "aggregatedOutput": "still-running"},
            {"type": "fileChange", "id": "file", "status": "completed", "changes": [{"path": "example.txt", "diff": "not-output"}]},
        ])
        value["conversationState"]["requests"] = [{"method": "needs-confirmation", "params": {"private": "not-output"}}]
        self.reader.bind_turn("turn-new")
        self.reader.apply_change(value)
        count = len([e for e in self.events if e[0] == "progress"])
        self.assertEqual(count, 3)
        self.reader.apply_change(patches([]))
        self.assertEqual(len([e for e in self.events if e[0] == "progress"]), count)
        self.assertNotIn("not-output", repr(self.events))

    def test_accepted_text_hint_does_not_duplicate_patched_text(self):
        self.reader.bind_turn("turn-new")
        self.reader.apply_change(snapshot())
        change = patches([{"op": "replace", "path": ["turns", 0, "items", 1, "text"], "value": "AB"}])
        change["acceptedTextChanges"] = [{"type": "text", "edits": [{"insert": "B"}]}]
        self.reader.apply_change(change)
        self.assertEqual(self.events[-1], ("output", "AB"))

    def test_clear_discards_snapshot(self):
        self.reader.bind_turn("turn-new")
        self.reader.apply_change(snapshot())
        self.reader.clear()
        self.assertIsNone(self.reader._state)
        self.assertIsNone(self.reader.turn_id)
        self.assertFalse(self.reader.terminal)


class PatchTests(unittest.TestCase):
    def test_list_and_object_operations(self):
        source = {"items": [1, 2], "old": True}
        updated = apply_patches(source, [
            {"op": "add", "path": ["items", 1], "value": 3},
            {"op": "replace", "path": ["items", 0], "value": 4},
            {"op": "remove", "path": ["old"]},
            {"op": "add", "path": ["items", "-"], "value": 5},
            {"op": "remove", "path": ["items", 2]},
        ])
        self.assertEqual(updated, {"items": [4, 3, 5]})
        self.assertEqual(source, {"items": [1, 2], "old": True})

    def test_atomic_failure_and_reserved_paths(self):
        source = {"x": 1}
        with self.assertRaises(NeedsSnapshot):
            apply_patches(source, [{"op": "replace", "path": ["x"], "value": 2}, {"op": "remove", "path": ["missing"]}])
        self.assertEqual(source, {"x": 1})
        for path in (["__proto__"], ["constructor"], [True]):
            with self.assertRaises(DesktopStreamError):
                apply_patches(source, [{"op": "add", "path": path, "value": 4}])

    def test_root_replacement_and_invalid_revision(self):
        self.assertEqual(apply_patches({}, [{"op": "replace", "path": [], "value": {"x": 1}}]), {"x": 1})
        reader = DesktopStreamReducer("thread-1")
        for revision in (-1, True, "1"):
            with self.assertRaises(DesktopStreamError):
                reader.apply_change(snapshot(revision=revision))

    def test_patch_both_original_import_bindings(self):
        first, second, replacement = object(), object(), object()
        backend, ui = SimpleNamespace(CodexSession=first), SimpleNamespace(CodexSession=second)
        self.assertEqual(install_session_class(backend, ui, replacement), (first, second))
        self.assertIs(backend.CodexSession, replacement)
        self.assertIs(ui.CodexSession, replacement)


if __name__ == "__main__":
    unittest.main()
