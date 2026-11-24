"""Send through the native Codex composer when its older IPC pipe is absent."""
from __future__ import annotations

import json
from pathlib import Path
from queue import Empty, Queue
import re
from string import punctuation
import subprocess
from threading import Thread
import time

from bridge_supervisor import powershell_path, read_observation
from conversation_sync import ACTIVE, ConversationReader
from desktop_ipc import DesktopIpcError, DesktopOutcomeUnknown


HERE = Path(__file__).resolve().parent
POLL_INTERVAL = 0.4
MARKDOWN_ESCAPE = re.compile(r"\\([" + re.escape(punctuation) + r"\n])")
REJECTIONS = {
    "window": "无法连接到已打开的 Codex 桌面窗口。",
    "chat": "无法在 Codex 中核对这条聊天的标识。",
    "draft": "Codex 的输入框已有草稿或附件，请在该聊天中处理后重试。",
    "busy": "此聊天仍在 Codex 中执行，请等待当前任务结束。",
    "editor": "当前 Codex 输入框无法接收这条消息。",
    "clipboard": "无法安全地通过 Codex 的聊天链接核对会话。",
}


class DesktopWindowRejected(DesktopIpcError):
    """The helper confirms that it did not invoke the send or stop control."""


def _rejected(reason):
    return DesktopWindowRejected(REJECTIONS.get(reason, REJECTIONS["editor"]) + " 消息没有发送。")


def _normalize(text):
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _same_prompt(observed, submitted):
    # The native composer serializes literal punctuation and line breaks as
    # Markdown before writing the user message to Codex's turn history.
    expected = _normalize(submitted)
    if _normalize(observed) == expected:
        return True
    markdown = observed.replace("\r\n", "\n").replace("\r", "\n")
    return _normalize(MARKDOWN_ESCAPE.sub(r"\1", markdown)) == expected


def _view_of(turn):
    items = []
    pending = False
    for message in turn["messages"]:
        kind = message["role"]
        if kind == "assistant":
            phase = message["phase"]
            items.append({"type": "agentMessage", "text": message["text"],
                          "phase": "final_answer" if phase == "final" else phase})
        elif kind == "summary":
            items.append({"type": "reasoning", "summary": [message["text"]]})
        elif kind == "tool":
            pending |= message["status"] in {"waitingForInput", "waitingForApproval"}
            items.append({"type": message["toolType"], "id": message["id"], "status": message["status"],
                          "command": message["command"], "changes": message["changes"]})
    status = turn["status"]
    return {"id": turn["codexTurnId"], "status": "inProgress" if status in ACTIVE else status,
            "items": items, "error": {"message": turn["error"]}}, pending


class DesktopChatWindow:
    """Use the selected Desktop window for input and its local history for readback."""

    def __init__(self, cancel, open_thread, *, reader=None):
        self.cancel = cancel
        self.open_thread = open_thread
        self.reader = reader if reader is not None else ConversationReader()
        self.observation = None
        self.thread_id = None
        self.turn_id = None
        self._revision = 0
        self._previous = None
        self._next_poll = 0.0

    def connect(self):
        observed = read_observation(HERE / "codex-state.json", time.time())
        if not observed or not observed.running or not observed.pid or not observed.app_started_at:
            raise _rejected("window")
        self.observation = observed

    def follow(self, thread_id):
        self.thread_id = thread_id

    def _turns(self):
        result = self.reader.read([self.thread_id])
        if not result["available"]:
            return None
        record = result["threads"].get(self.thread_id)
        if not record or not record["visible"] or record["threadId"] != self.thread_id:
            raise _rejected("chat")
        return record["turns"]

    def next_change(self, thread_id, timeout=0.25):
        if thread_id != self.thread_id:
            raise _rejected("chat")
        now = time.monotonic()
        delay = self._next_poll - now
        if delay > 0:
            time.sleep(min(timeout, delay))
            if delay > timeout:
                return None
        self._next_poll = time.monotonic() + POLL_INTERVAL
        turns = self._turns()
        if turns is None:
            return None
        turn = (next((turn for turn in turns if turn["codexTurnId"] == self.turn_id), None)
                if self.turn_id else turns[-1] if turns else None)
        view, pending = _view_of(turn) if turn else (None, False)
        state = {"id": self.thread_id, "turns": [view] if view else [], "requests": [True] if pending else []}
        if state == self._previous:
            return None
        self._previous = state
        self._revision += 1
        return {"type": "snapshot", "revision": self._revision, "conversationState": state}

    def _invoke(self, operation, before_commit, *, prompt=None):
        observed = self.observation
        request = json.dumps({"operation": operation, "pid": observed.pid, "startedAt": observed.app_started_at,
                              "threadId": self.thread_id, "prompt": prompt}, ensure_ascii=True)
        command = [str(powershell_path()), "-NoProfile", "-NonInteractive", "-STA",
                   "-ExecutionPolicy", "Bypass", "-File", str(HERE / "desktop_chat_window.ps1")]
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, encoding="utf-8",
                                       creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError as error:
            raise _rejected("window") from error
        committed = False
        try:
            prepared = Queue(maxsize=1)
            Thread(target=lambda: prepared.put(process.stdout.readline().strip()), daemon=True).start()
            process.stdin.write(request + "\n")
            process.stdin.flush()
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if operation == "send" and self.cancel.is_set():
                    raise DesktopWindowRejected("已取消发送。")
                try:
                    response = prepared.get(timeout=0.1)
                    break
                except Empty:
                    continue
            else:
                raise _rejected("chat")
            if response != "ready":
                raise _rejected(response.removeprefix("reject:"))
            before_commit()
            if operation == "send" and self.cancel.is_set():
                raise DesktopWindowRejected("已取消发送。")
            committed = True
            output, _ = process.communicate("commit\n", timeout=8)
            result = output.strip()
            if result.startswith("reject:"):
                raise _rejected(result.removeprefix("reject:"))
            if process.returncode != 0 or result != "invoked":
                raise DesktopOutcomeUnknown("Codex composer did not confirm the action")
        except (OSError, subprocess.TimeoutExpired) as error:
            if committed:
                raise DesktopOutcomeUnknown("Codex composer action outcome is unknown") from error
            raise _rejected("window") from error
        finally:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()  # EOF never commits a prepared action.
            if process.poll() is None:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdout.close()

    def start_turn(self, thread_id, prompt, *, client_message_id=None, timeout=30):
        if thread_id != self.thread_id:
            raise _rejected("chat")
        if len(prompt.encode("utf-8")) > 1024 * 1024:
            raise DesktopWindowRejected("内容超过 1 MiB，消息没有发送。")
        existing = set()

        def remember_turns():
            turns = self._turns()
            if turns is None:
                raise _rejected("chat")
            if turns and turns[-1]["status"] in ACTIVE:
                raise _rejected("busy")
            existing.update(turn["codexTurnId"] for turn in turns)

        self._invoke("send", remember_turns, prompt=prompt)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                turns = self._turns() or []
            except DesktopWindowRejected as error:
                raise DesktopOutcomeUnknown("The submitted chat can no longer be read") from error
            new_turns = [turn for turn in turns if turn["codexTurnId"] not in existing]
            if len(new_turns) == 1:
                first_prompt = next((item["text"] for item in new_turns[0]["messages"] if item["role"] == "user"), "")
                if _same_prompt(first_prompt, prompt):
                    self.turn_id = new_turns[0]["codexTurnId"]
                    return {"turn": {"id": self.turn_id}}
            if len(new_turns) > 1:
                break
            time.sleep(POLL_INTERVAL)
        raise DesktopOutcomeUnknown("A new Codex turn with the submitted prompt was not confirmed")

    def interrupt(self, thread_id, turn_id):
        if thread_id != self.thread_id or turn_id != self.turn_id:
            raise _rejected("chat")

        def check_active_turn():
            turns = self._turns()
            if not turns or turns[-1]["codexTurnId"] != turn_id or turns[-1]["status"] not in ACTIVE:
                raise _rejected("busy")

        check_active_turn()
        self.open_thread(thread_id)
        self._invoke("stop", check_active_turn)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            turns = self._turns() or []
            if any(turn["codexTurnId"] == turn_id and turn["status"] == "interrupted" for turn in turns):
                return
            time.sleep(POLL_INTERVAL)
        raise DesktopOutcomeUnknown("Codex did not confirm that the expected turn stopped")

    def close(self):
        self._previous = None
        self.reader.cache.clear()
