"""Run pet user turns through the existing Codex Desktop chat owner.

Only an explicit ChatController.submit calls run(). New chats use the original
app-server solely to create durable metadata, then release that writer. The
Desktop owns execution and the pet follows its exact thread/turn stream. A
request with an uncertain outcome is never retried through the legacy server.
"""
from __future__ import annotations

import importlib
import os
import re
import time
from urllib.parse import quote
import uuid

from desktop_chat_adapter import DesktopStreamReducer, DesktopStreamError, NeedsSnapshot, install_session_class
from desktop_ipc import DesktopIpcClient, DesktopIpcError, DesktopOutcomeUnknown, DesktopUnavailable


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")


def open_desktop_thread(thread_id):
    if not isinstance(thread_id, str) or not _ID.fullmatch(thread_id):
        raise ValueError("Invalid Codex thread identifier")
    os.startfile("codex://threads/" + quote(thread_id, safe=""))


def make_session_class(backend, *, client_factory=DesktopIpcClient,
                       open_thread=open_desktop_thread, clock=time.monotonic,
                       sleep=time.sleep, owner_timeout=15.0, snapshot_timeout=10.0,
                       inactivity_timeout=900.0):
    original_class = backend.CodexSession

    class DesktopLinkedSession(original_class):
        _bailong_desktop_link = True

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._desktop_client = None
            self._desktop_stream = None
            self._desktop_submitted = False

        def _check_cancel(self):
            if not self.cancel.is_set():
                return
            if self._desktop_submitted and self.turn_id and self._desktop_client is not None:
                try:
                    self._desktop_client.interrupt(self.thread_id, self.turn_id)
                except (DesktopIpcError, OSError):
                    raise backend.TaskCancelled("已请求停止；连接未确认，请在 Codex 中检查最终状态。")
            raise backend.TaskCancelled("任务已停止，已有内容仍保留在同一 Codex 聊天中。")

        def _create_thread_only(self, prompt):
            """No turn/start here: seed one persistent chat and release stdio."""
            self._check_cancel()
            persisted = False
            try:
                super().start()
                self.rpc("initialize", {"clientInfo": {
                    "name": "bailong_pet", "title": "GPT白龙娘动态桌宠", "version": backend.VERSION}})
                self.send({"method": "initialized"})
                instruction = backend.PERSONA
                if self.mode == "chat":
                    instruction += "当前为聊天模式：只根据对话直接交流，不查看或更改文件，不运行工具。"
                params = {"cwd": self.cwd,
                          "sandbox": "read-only" if self.mode == "chat" else "workspace-write",
                          "approvalPolicy": "on-request", "approvalsReviewer": "user",
                          "developerInstructions": instruction, "ephemeral": False}
                result = self.rpc("thread/start", params)
                identifier = result.get("thread", {}).get("id")
                if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
                    raise RuntimeError("Codex 没有返回有效聊天标识，消息尚未发送。")
                self.thread_id = identifier
                try:
                    self.rpc("thread/name/set", {"threadId": identifier,
                             "name": " ".join(prompt.split())[:60]}, timeout=5)
                except backend.TaskCancelled:
                    raise
                except Exception as error:
                    # Current app-server does not durably persist an empty
                    # thread/start alone. Naming must finish before releasing
                    # its writer and publishing the ID to the original store.
                    raise RuntimeError("新聊天尚未完成保存，消息没有发送。请稍后重试。") from error
                persisted = True
                self.emit("thread", identifier)
            finally:
                if not persisted:
                    self.thread_id = None
                super().close()

        def _attach(self, prompt):
            self._desktop_client = client_factory()
            self._desktop_client.connect(timeout=5)
            if not self.thread_id:
                self._create_thread_only(prompt)
            elif not isinstance(self.thread_id, str) or not _ID.fullmatch(self.thread_id):
                raise RuntimeError("保存的聊天标识无效，消息尚未发送。")
            self._check_cancel()
            open_thread(self.thread_id)
            deadline = clock() + owner_timeout
            owner = None
            while clock() < deadline:
                self._check_cancel()
                owner = self._desktop_client.discover_owner(self.thread_id,
                            timeout=min(3.0, max(0.1, deadline - clock())))
                if owner:
                    break
                sleep(0.2)
            if not owner:
                raise DesktopUnavailable("Codex has not acquired the requested chat")
            self._desktop_client.subscribe(self.thread_id, owner)
            self._desktop_stream = DesktopStreamReducer(self.thread_id, self.emit)
            deadline = clock() + snapshot_timeout
            while clock() < deadline:
                self._check_cancel()
                change = self._desktop_client.next_change(self.thread_id, timeout=0.25)
                if change is None:
                    continue
                try:
                    self._desktop_stream.apply_change(change)
                except NeedsSnapshot:
                    self._desktop_client.request_snapshot(self.thread_id)
                    continue
                if self._desktop_stream.revision is not None:
                    return
            raise DesktopUnavailable("Codex has not provided the chat snapshot")

        def run(self, prompt):
            self._desktop_submitted = False
            self.turn_id = None
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError("请输入要发送的内容。")
            try:
                self._attach(prompt)
                self._check_cancel()
                if self._desktop_stream.busy:
                    raise RuntimeError("此聊天仍在 Codex 中执行，消息尚未发送。请等待完成，或先在 Codex 中停止当前任务。")
                self.emit("progress", "已连接到 Codex 中的同一聊天，白龙会同步显示执行进展。")
                # Set before sending. Any exception from this point is not a
                # license to submit the prompt again by a different transport.
                self._desktop_submitted = True
                result = self._desktop_client.start_turn(self.thread_id, prompt,
                             client_message_id=str(uuid.uuid4()), timeout=30)
                turn_id = result.get("turn", {}).get("id")
                if not isinstance(turn_id, str) or not _ID.fullmatch(turn_id):
                    raise DesktopOutcomeUnknown("Desktop did not return a recognized turn")
                self.turn_id = turn_id
                self._desktop_stream.bind_turn(turn_id)
                deadline = clock() + inactivity_timeout
                while not self._desktop_stream.terminal:
                    self._check_cancel()
                    if clock() >= deadline:
                        raise DesktopUnavailable("Codex chat stream was inactive too long")
                    change = self._desktop_client.next_change(self.thread_id, timeout=0.25)
                    if change is None:
                        continue
                    try:
                        changed = self._desktop_stream.apply_change(change)
                    except NeedsSnapshot:
                        self._desktop_client.request_snapshot(self.thread_id)
                        continue
                    if changed:
                        deadline = clock() + inactivity_timeout
                if self._desktop_stream.status == "interrupted":
                    raise backend.TaskCancelled("任务已在 Codex 中停止，已有内容保留在同一聊天中。")
                if self._desktop_stream.status == "failed":
                    raise RuntimeError(self._desktop_stream.error or "Codex 未能完成任务，请查看同一聊天中的错误。")
                return self._desktop_stream.result()
            except backend.TaskCancelled:
                raise
            except (DesktopIpcError, DesktopStreamError, OSError) as error:
                if self._desktop_submitted:
                    raise RuntimeError("与 Codex 的同步确认中断，消息可能已在执行；请先查看同一聊天，避免重复发送。") from error
                raise RuntimeError("尚未连接到 Codex 中的对应聊天，消息没有发送。请确认 Codex 已打开并已加载此聊天后重试。") from error
            finally:
                self.close()

        def close(self):
            client, self._desktop_client = self._desktop_client, None
            if client is not None:
                try:
                    client.close()
                except (DesktopIpcError, OSError):
                    pass
            stream, self._desktop_stream = self._desktop_stream, None
            if stream is not None:
                stream.clear()
            super().close()

    DesktopLinkedSession.__name__ = "DesktopLinkedSession"
    return DesktopLinkedSession


def install_desktop_chat_link(original=None, **dependencies):
    """Call after load_original_module and before constructing the first Pet.

    Pet imports ChatController lazily. The existing Qt worker and signals are
    retained, so stream events and local-history updates stay on the GUI thread.
    """
    backend = importlib.import_module("chat_backend")
    ui = importlib.import_module("chat_ui")
    if getattr(backend.CodexSession, "_bailong_desktop_link", False):
        return backend.CodexSession
    session_class = make_session_class(backend, **dependencies)
    install_session_class(backend, ui, session_class)
    return session_class
