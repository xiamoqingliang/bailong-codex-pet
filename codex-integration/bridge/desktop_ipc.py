"""Scoped client for the current Desktop owner/follower IPC protocol.

This is a local, version-checked integration, not an OpenAI public API. It does
not launch app-server, create threads, read databases, or grant approvals.
Only an explicitly subscribed conversation's state is retained. A turn is
never automatically replayed after an uncertain transport outcome.
"""
from __future__ import annotations

from collections import deque
import ctypes
from ctypes import wintypes
import json
import os
import re
import struct
import time
import uuid

PIPE_NAME = r"\\.\pipe\codex-ipc"
MAX_FRAME_BYTES = 32 * 1024 * 1024
MAX_QUEUED_CHANGES = 512
STREAM_VERSION = 11
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")
_VERSIONS = {
    "initialize": 0,
    "thread-owner-discovery": 1,
    "thread-follower-start-turn": 2,
    "thread-follower-interrupt-turn": 4,
    "thread-follower-load-complete-history": 1,
    "thread-stream-following-changed": 1,
    "thread-stream-state-changed": STREAM_VERSION,
}


class DesktopIpcError(RuntimeError):
    pass


class DesktopUnavailable(DesktopIpcError):
    pass


class DesktopProtocolError(DesktopIpcError):
    pass


class DesktopRequestError(DesktopIpcError):
    pass


class DesktopOutcomeUnknown(DesktopIpcError):
    """The caller must inspect the same thread instead of resending its turn."""


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Invalid conversation or IPC client identifier")
    return value


def encode_frame(message):
    raw = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not 0 < len(raw) <= MAX_FRAME_BYTES:
        raise DesktopProtocolError("IPC frame is too large")
    return struct.pack("<I", len(raw)) + raw


class FrameDecoder:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("IPC data must be bytes")
        self.buffer.extend(data)
        messages = []
        while len(self.buffer) >= 4:
            size = struct.unpack_from("<I", self.buffer)[0]
            if not 0 < size <= MAX_FRAME_BYTES:
                self.buffer.clear()
                raise DesktopProtocolError("Invalid IPC frame length")
            if len(self.buffer) < size + 4:
                break
            raw = bytes(self.buffer[4:size + 4])
            del self.buffer[:size + 4]
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeError, ValueError) as error:
                raise DesktopProtocolError("Invalid IPC JSON") from error
            if not isinstance(message, dict):
                raise DesktopProtocolError("IPC message must be an object")
            messages.append(message)
        return messages


class WindowsPipeTransport:
    """Byte-mode pipe with bounded polling, no service startup or network use."""
    def __init__(self, *, expected_server_pid=None):
        if os.name != "nt":
            raise DesktopUnavailable("Desktop IPC requires Windows")
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        self.api.CreateFileW.restype = wintypes.HANDLE
        self.api.WaitNamedPipeW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
        self.api.PeekNamedPipe.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
        self.api.ReadFile.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
        self.api.WriteFile.argtypes = self.api.ReadFile.argtypes
        self.api.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.handle = None
        self.server_pid = None
        self.expected_server_pid = expected_server_pid
        self.decoder = FrameDecoder()
        self.pending = deque()

    def connect(self, timeout=3.0):
        deadline = time.monotonic() + timeout
        while True:
            handle = self.api.CreateFileW(PIPE_NAME, 0xC0000000, 0, None, 3, 0, None)
            if handle != ctypes.c_void_p(-1).value:
                self.handle = handle
                break
            code = ctypes.get_last_error()
            if code != 231 or time.monotonic() >= deadline:
                raise DesktopUnavailable("Codex Desktop IPC is not available")
            self.api.WaitNamedPipeW(PIPE_NAME, 100)
        get_pid = self.api.GetNamedPipeServerProcessId
        get_pid.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG))
        pid = wintypes.ULONG()
        if get_pid(self.handle, ctypes.byref(pid)):
            self.server_pid = pid.value
        if self.expected_server_pid is not None and self.server_pid != self.expected_server_pid:
            self.close()
            raise DesktopUnavailable("Desktop IPC belongs to a different process")

    def send(self, message):
        if self.handle is None:
            raise DesktopUnavailable("Desktop IPC is disconnected")
        raw = encode_frame(message)
        offset = 0
        while offset < len(raw):
            block = raw[offset:offset + 65536]
            buffer = ctypes.create_string_buffer(block)
            count = wintypes.DWORD()
            if not self.api.WriteFile(self.handle, buffer, len(block), ctypes.byref(count), None) or not count.value:
                raise DesktopUnavailable("Desktop IPC write failed")
            offset += count.value

    def receive(self, timeout):
        deadline = time.monotonic() + max(0, timeout)
        while True:
            if self.pending:
                return self.pending.popleft()
            if self.handle is None:
                raise DesktopUnavailable("Desktop IPC is disconnected")
            available = wintypes.DWORD()
            if not self.api.PeekNamedPipe(self.handle, None, 0, None, ctypes.byref(available), None):
                raise DesktopUnavailable("Desktop IPC closed")
            if available.value:
                size = min(available.value, 65536)
                buffer = ctypes.create_string_buffer(size)
                count = wintypes.DWORD()
                if not self.api.ReadFile(self.handle, buffer, size, ctypes.byref(count), None) or not count.value:
                    raise DesktopUnavailable("Desktop IPC read failed")
                self.pending.extend(self.decoder.feed(buffer.raw[:count.value]))
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(0.02, remaining))

    def close(self):
        if self.handle is not None:
            self.api.CloseHandle(self.handle)
            self.handle = None
        self.pending.clear()
        self.decoder.buffer.clear()


class DesktopIpcClient:
    """Single-threaded client; call from the pet's chat worker, not Qt's thread."""
    def __init__(self, transport=None):
        self.transport = transport if transport is not None else WindowsPipeTransport()
        self.client_id = "initializing-client"
        self.owners = {}
        self.changes = deque()
        self._queued_bytes = 0
        self._fatal = None
        self.connected = False

    def connect(self, timeout=5.0):
        self.transport.connect(timeout)
        try:
            result = self._rpc("initialize", {"clientType": "bailong"}, timeout=timeout)
            self.client_id = _identifier(result.get("clientId"))
            self.connected = True
            return self
        except Exception:
            self.transport.close()
            raise

    def _dispatch(self, message):
        kind = message.get("type")
        if kind == "client-discovery-request":
            self.transport.send({"type": "client-discovery-response", "requestId": message.get("requestId"),
                                 "response": {"canHandle": False}})
            return
        if kind == "request":
            self.transport.send({"type": "response", "requestId": message.get("requestId"),
                                 "resultType": "error", "error": "no-handler-for-request"})
            return
        if kind != "broadcast":
            return
        recipients = message.get("targetClientIds")
        if recipients is not None and self.client_id not in recipients:
            return
        params = message.get("params")
        if not isinstance(params, dict):
            return
        if message.get("method") == "client-status-changed":
            if params.get("status") == "disconnected" and params.get("clientId") in self.owners.values():
                self._fatal = DesktopUnavailable("The conversation owner disconnected")
            return
        if message.get("method") != "thread-stream-state-changed":
            return
        thread_id = params.get("conversationId")
        owner = self.owners.get(thread_id)
        if owner is None or message.get("sourceClientId") != owner or params.get("hostId") != "local":
            return
        if message.get("version") != STREAM_VERSION:
            self._fatal = DesktopProtocolError("Unsupported Desktop stream version")
            return
        change = params.get("change")
        if not isinstance(change, dict) or change.get("type") not in ("snapshot", "patches"):
            self._fatal = DesktopProtocolError("Unsupported conversation change")
            return
        size = len(json.dumps(change, ensure_ascii=False).encode("utf-8"))
        if len(self.changes) >= MAX_QUEUED_CHANGES or self._queued_bytes + size > MAX_FRAME_BYTES:
            self._fatal = DesktopProtocolError("Desktop conversation stream exceeded its buffer")
            return
        self.changes.append((thread_id, change, size))
        self._queued_bytes += size

    def _rpc(self, method, params, *, target=None, timeout=10.0, mutating=False):
        if method not in _VERSIONS:
            raise ValueError("Unsupported IPC request")
        if self._fatal:
            raise self._fatal
        request_id = str(uuid.uuid4())
        message = {"type": "request", "requestId": request_id, "sourceClientId": self.client_id,
                   "version": _VERSIONS[method], "method": method, "params": params,
                   "timeoutMs": max(1, int(timeout * 1000))}
        if target is not None:
            message["targetClientId"] = _identifier(target)
        try:
            self.transport.send(message)
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Desktop IPC request timed out")
                response = self.transport.receive(remaining)
                if response is None:
                    raise TimeoutError("Desktop IPC request timed out")
                if response.get("type") == "response" and response.get("requestId") == request_id:
                    if response.get("resultType") != "success":
                        code = response.get("error")
                        if mutating and isinstance(code, str) and (code in {
                                "request-timeout", "client-disconnected", "server-closed", "timeout", "connection-closed"
                        } or code.endswith("-timeout")):
                            raise DesktopOutcomeUnknown("Message delivery is unconfirmed; inspect the same conversation")
                        raise DesktopRequestError(str(code)[:512] if isinstance(code, str) else "Desktop request rejected")
                    if response.get("method") != method:
                        raise DesktopProtocolError("Mismatched Desktop response method")
                    if target is not None and response.get("handledByClientId") != target:
                        raise DesktopProtocolError("Desktop response came from an unexpected owner")
                    result = response.get("result")
                    if not isinstance(result, dict):
                        raise DesktopProtocolError("Invalid Desktop response result")
                    return dict(result, _handledByClientId=response.get("handledByClientId"))
                self._dispatch(response)
                if self._fatal:
                    raise self._fatal
        except DesktopRequestError:
            raise
        except (TimeoutError, OSError, DesktopIpcError) as error:
            if mutating:
                raise DesktopOutcomeUnknown("Message delivery is unconfirmed; do not send it again automatically") from error
            if isinstance(error, DesktopIpcError):
                raise
            raise DesktopUnavailable("Desktop IPC did not respond") from error

    def discover_owner(self, thread_id, timeout=5.0):
        thread_id = _identifier(thread_id)
        try:
            result = self._rpc("thread-owner-discovery", {"hostId": "local", "conversationId": thread_id}, timeout=timeout)
        except DesktopRequestError as error:
            if str(error) == "no-client-found":
                return None
            raise
        return _identifier(result.get("_handledByClientId"))

    def subscribe(self, thread_id, owner):
        thread_id, owner = _identifier(thread_id), _identifier(owner)
        if not self.connected:
            raise DesktopUnavailable("Desktop IPC is not initialized")
        self.owners[thread_id] = owner
        self.transport.send({"type": "broadcast", "method": "thread-stream-following-changed",
                             "sourceClientId": self.client_id, "targetClientIds": [owner], "version": 1,
                             "params": {"conversationId": thread_id, "hostId": "local", "following": True}})

    def request_snapshot(self, thread_id):
        """Reannounce following; current Desktop sends the owner snapshot again."""
        self.subscribe(thread_id, self._owner(thread_id))

    def _owner(self, thread_id):
        thread_id = _identifier(thread_id)
        owner = self.owners.get(thread_id)
        if owner is None:
            raise DesktopUnavailable("Subscribe to the conversation owner first")
        return owner

    def next_change(self, thread_id, timeout=0.25):
        thread_id = _identifier(thread_id)
        self._owner(thread_id)
        deadline = time.monotonic() + max(0, timeout)
        while True:
            if self._fatal:
                raise self._fatal
            for index, (key, change, size) in enumerate(self.changes):
                if key == thread_id:
                    del self.changes[index]
                    self._queued_bytes -= size
                    return change
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            message = self.transport.receive(remaining)
            if message is None:
                return None
            self._dispatch(message)

    def start_turn(self, thread_id, text, *, client_message_id=None, timeout=30.0):
        owner = self._owner(thread_id)
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 1024 * 1024:
            raise ValueError("Message must be nonempty text of at most 1 MiB")
        message_id = _identifier(client_message_id or str(uuid.uuid4()))
        result = self._rpc("thread-follower-start-turn", {
            "conversationId": thread_id,
            "turnStart": {"request": {"threadId": thread_id,
                "input": [{"type": "text", "text": text}], "clientUserMessageId": message_id},
                "context": {"inheritThreadSettings": True}},
        }, target=owner, timeout=timeout, mutating=True)
        response = result.get("result")
        if not isinstance(response, dict) or not isinstance(response.get("turn"), dict):
            raise DesktopOutcomeUnknown("Desktop accepted the request but its turn response is not recognized")
        return response

    def interrupt(self, thread_id, turn_id, timeout=10.0):
        return self._rpc("thread-follower-interrupt-turn", {
            "conversationId": _identifier(thread_id), "expectedTurnId": _identifier(turn_id), "mode": "user-stop",
        }, target=self._owner(thread_id), timeout=timeout, mutating=True)

    def close(self):
        if self.connected:
            for thread_id, owner in list(self.owners.items()):
                try:
                    self.transport.send({"type": "broadcast", "method": "thread-stream-following-changed",
                        "sourceClientId": self.client_id, "targetClientIds": [owner], "version": 1,
                        "params": {"conversationId": thread_id, "hostId": "local", "following": False}})
                except (OSError, DesktopIpcError):
                    pass
        self.connected = False
        self.owners.clear()
        self.changes.clear()
        self._queued_bytes = 0
        self.transport.close()
