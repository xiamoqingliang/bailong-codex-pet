"""Translate one owned Desktop chat stream into the original pet's UI events.

This module performs no I/O, sends no prompts and opens no windows. The IPC
transport must authenticate/filter the selected owner, host and thread before
passing changes here. Only the requested turn's public answer and summaries
are emitted; reasoning content and other chats/turns are never emitted.
"""
from __future__ import annotations

from copy import deepcopy
import re


MAX_PATCHES = 10000
MAX_PATH = 64
MAX_TEXT = 16 * 1024 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")
_RESERVED = frozenset(("__proto__", "prototype", "constructor"))


class DesktopStreamError(RuntimeError):
    pass


class NeedsSnapshot(DesktopStreamError):
    """The caller must resubscribe/request a snapshot, never invent completion."""


def _identifier(value):
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _revision(value):
    if type(value) is not int or value < 0:
        raise DesktopStreamError("Codex stream revision is invalid")
    return value


def apply_patches(state: dict, patches: list) -> dict:
    """Apply the Desktop's Immer add/replace/remove patches atomically."""
    if not isinstance(patches, list) or len(patches) > MAX_PATCHES:
        raise DesktopStreamError("Codex stream patch list is invalid")
    result = deepcopy(state)
    for patch in patches:
        if not isinstance(patch, dict):
            raise DesktopStreamError("Codex stream patch is invalid")
        operation, path = patch.get("op"), patch.get("path")
        if (operation not in ("add", "replace", "remove") or not isinstance(path, list)
                or len(path) > MAX_PATH or any(type(part) not in (str, int) for part in path)
                or any(isinstance(part, str) and part in _RESERVED for part in path)):
            raise DesktopStreamError("Codex stream patch path is invalid")
        if operation != "remove" and "value" not in patch:
            raise DesktopStreamError("Codex stream patch value is missing")
        if not path:
            if operation != "replace" or not isinstance(patch.get("value"), dict):
                raise DesktopStreamError("Codex stream root patch is invalid")
            result = deepcopy(patch["value"])
            continue
        parent = result
        for part in path[:-1]:
            if isinstance(parent, dict) and isinstance(part, str) and part in parent:
                parent = parent[part]
            elif isinstance(parent, list) and type(part) is int and 0 <= part < len(parent):
                parent = parent[part]
            else:
                raise NeedsSnapshot("Codex stream patch no longer matches its snapshot")
        key = path[-1]
        if isinstance(parent, dict) and isinstance(key, str):
            if operation in ("replace", "remove") and key not in parent:
                raise NeedsSnapshot("Codex stream patch key is missing")
            if operation == "remove":
                del parent[key]
            else:
                parent[key] = deepcopy(patch["value"])
        elif isinstance(parent, list):
            if operation == "add" and key == "-":
                key = len(parent)
            end = len(parent) if operation == "add" else len(parent) - 1
            if type(key) is not int or not 0 <= key <= end:
                raise NeedsSnapshot("Codex stream patch index is invalid")
            if operation == "add":
                parent.insert(key, deepcopy(patch["value"]))
            elif operation == "replace":
                parent[key] = deepcopy(patch["value"])
            else:
                parent.pop(key)
        else:
            raise NeedsSnapshot("Codex stream patch parent is missing")
    if not isinstance(result, dict):
        raise DesktopStreamError("Codex conversation snapshot is invalid")
    return result


def _text(value):
    if not isinstance(value, str):
        return ""
    if len(value) > MAX_TEXT:
        raise DesktopStreamError("Codex response exceeds the local display limit")
    return value


def _turn_id(turn):
    return turn.get("turnId") or turn.get("id") if isinstance(turn, dict) else None


def _find_turn(state, turn_id):
    # Desktop prefers canonical history entities, falling back to its transient
    # legacy turns for a newly submitted turn not yet installed in an island.
    history = state.get("turnHistory")
    if isinstance(history, dict) and history.get("kind") == "canonical":
        canonical = history.get("history")
        if isinstance(canonical, dict):
            entities = canonical.get("entitiesByKey")
            if isinstance(entities, dict):
                turn = entities.get("turn:" + turn_id)
                if _turn_id(turn) == turn_id:
                    return turn
                for island in canonical.get("islands", []):
                    if not isinstance(island, dict):
                        continue
                    for entry in island.get("entries", []):
                        if not isinstance(entry, dict):
                            continue
                        turn = entities.get(entry.get("value"))
                        if _turn_id(turn) == turn_id:
                            return turn
    turns = state.get("turns", [])
    if isinstance(turns, list):
        return next((turn for turn in reversed(turns) if _turn_id(turn) == turn_id), None)
    return None


def _public_summary(item):
    summary = item.get("summary")
    if isinstance(summary, str):
        return _text(summary)
    if not isinstance(summary, list):
        return ""
    return "\n\n".join(text for value in summary
                       if (text := _text(value if isinstance(value, str) else
                                         value.get("text") if isinstance(value, dict) else None)))


class DesktopStreamReducer:
    """State and public display events for one Desktop-owned user turn."""
    def __init__(self, thread_id, emit=None):
        if not _identifier(thread_id):
            raise DesktopStreamError("Codex thread identifier is invalid")
        self.thread_id = thread_id
        self.turn_id = None
        self.emit = emit or (lambda kind, value: None)
        self.revision = None
        self._state = None
        self._output = ""
        self._summary = ""
        self._final = ""
        self._progress = set()
        self.status = None
        self.error = None
        self._has_emitted_thread = False
        self._pending_input = False

    def bind_turn(self, turn_id):
        if not _identifier(turn_id):
            raise DesktopStreamError("Codex turn identifier is invalid")
        if turn_id == self.turn_id:
            return
        self.turn_id = turn_id
        self._output = self._summary = self._final = ""
        self._progress.clear()
        self.status = self.error = None
        self._pending_input = False
        if not self._has_emitted_thread:
            self.emit("thread", self.thread_id)
            self._has_emitted_thread = True
        self._publish()

    def apply_change(self, change):
        if not isinstance(change, dict):
            raise DesktopStreamError("Codex stream change is invalid")
        revision = _revision(change.get("revision"))
        if self.revision is not None and revision <= self.revision:
            return False
        kind = change.get("type")
        if kind == "snapshot":
            state = change.get("conversationState")
            if not isinstance(state, dict):
                raise DesktopStreamError("Codex conversation snapshot is invalid")
            candidate = deepcopy(state)
        elif kind == "patches":
            base = _revision(change.get("baseRevision"))
            if self._state is None or base != self.revision:
                raise NeedsSnapshot("Codex stream skipped a revision; a fresh snapshot is required")
            candidate = apply_patches(self._state, change.get("patches"))
        else:
            raise DesktopStreamError("Codex stream change type is unsupported")
        if candidate.get("id") != self.thread_id or candidate.get("hostId", "local") != "local":
            raise DesktopStreamError("Codex snapshot belongs to another chat")
        self._state, self.revision = candidate, revision
        # acceptedTextChanges is an additional Desktop text-cache optimization;
        # the same text mutations are already present in the Immer patches.
        self._publish()
        return True

    @property
    def terminal(self):
        return self.status in ("completed", "failed", "interrupted")

    @property
    def completed(self):
        return self.status == "completed"

    @property
    def busy(self):
        """Preflight only: do not submit over a turn already running in Desktop."""
        state = self._state
        if state is None:
            return False
        runtime = state.get("threadRuntimeStatus")
        if isinstance(runtime, dict) and runtime.get("type") == "active":
            return True
        pending = state.get("unconfirmedTurnSubmissions", [])
        if isinstance(pending, list) and any(isinstance(item, dict) and item.get("terminal") is not True
                                             for item in pending):
            return True
        candidates = []
        history = state.get("turnHistory")
        if isinstance(history, dict) and history.get("kind") == "canonical":
            canonical = history.get("history", {})
            entities = canonical.get("entitiesByKey", {}) if isinstance(canonical, dict) else {}
            islands = canonical.get("islands", []) if isinstance(canonical, dict) else []
            if isinstance(entities, dict) and isinstance(islands, list):
                for island in islands:
                    if not isinstance(island, dict):
                        continue
                    for entry in island.get("entries", []):
                        item = entities.get(entry.get("value")) if isinstance(entry, dict) else None
                        if isinstance(item, dict):
                            candidates.append(item)
        turns = state.get("turns", [])
        if isinstance(turns, list):
            known = {_turn_id(item) for item in candidates}
            candidates.extend(item for item in turns if isinstance(item, dict) and _turn_id(item) not in known)
        return bool(candidates and candidates[-1].get("status") == "inProgress")

    def _publish(self):
        if self._state is None or self.turn_id is None:
            return
        turn = _find_turn(self._state, self.turn_id)
        if turn is None:
            return
        items = turn.get("items", [])
        if not isinstance(items, list):
            raise DesktopStreamError("Codex turn items are invalid")
        output, final, summaries = [], [], []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "agentMessage":
                value = _text(item.get("text"))
                if value:
                    output.append(value)
                    if item.get("phase") == "final_answer":
                        final.append(value)
            elif kind == "reasoning":
                # Deliberately never inspect item.content or encrypted content.
                value = _public_summary(item)
                if value:
                    summaries.append(value)
            elif kind in ("commandExecution", "fileChange"):
                token = (str(item.get("id", index)), str(item.get("status")))
                if token in self._progress:
                    continue
                self._progress.add(token)
                if kind == "commandExecution":
                    command = _text(item.get("command"))
                    prefix = "正在执行：" if item.get("status") == "inProgress" else "执行结束："
                    value = prefix + command
                    aggregate = _text(item.get("aggregatedOutput"))
                    if aggregate and item.get("status") != "inProgress":
                        value += "\n" + aggregate[-12000:]
                else:
                    changes = item.get("changes", [])
                    paths = [_text(entry.get("path")) for entry in changes if isinstance(entry, dict)] if isinstance(changes, list) else []
                    value = "文件更改：" + "、".join(path for path in paths if path)
                self.emit("progress", value)
        transcript = _text("\n\n".join(output))
        summary = _text("\n\n".join(summaries))
        self._final = "\n\n".join(final) or (output[-1] if output else "")
        if transcript != self._output:
            self._output = transcript
            self.emit("output", transcript)
        if summary != self._summary:
            self._summary = summary
            self.emit("summary_update", summary)
        status = turn.get("status")
        if status not in ("inProgress", "completed", "failed", "interrupted"):
            raise DesktopStreamError("Codex turn status is unsupported")
        self.status = status
        error = turn.get("error")
        self.error = _text(error.get("message")) if isinstance(error, dict) else None
        requests = self._state.get("requests")
        pending = isinstance(requests, list) and bool(requests) and status == "inProgress"
        if pending and not self._pending_input:
            self.emit("progress", "请在 Codex 中处理确认或补充信息，白龙会继续同步进展。")
        self._pending_input = pending

    def result(self):
        if not self.completed:
            raise DesktopStreamError("Codex has not confirmed completion of this turn")
        return {"threadId": self.thread_id,
                "text": self._final or "任务已完成，未返回文字结果。",
                "transcript": self._output}

    def clear(self):
        """Discard in-memory snapshots on close; no transcript is logged here."""
        self._state = None
        self.revision = None
        self.turn_id = None
        self._output = self._summary = self._final = ""
        self._progress.clear()
        self.status = self.error = None


def install_session_class(chat_backend, chat_ui, session_class):
    """Patch both bindings before a first ChatController/TaskWorker is created.

    chat_ui uses ``from chat_backend import CodexSession``, so changing only
    the backend binding would leave the actual worker on the legacy transport.
    Existing controllers/workers must be stopped normally before replacing it.
    """
    previous = (chat_backend.CodexSession, chat_ui.CodexSession)
    chat_backend.CodexSession = session_class
    chat_ui.CodexSession = session_class
    return previous
