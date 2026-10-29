"""Protocol-only fixtures; no real pipe, account, thread, or model request."""
from collections import deque
import json
import struct
import unittest

from desktop_ipc import (DesktopIpcClient, DesktopOutcomeUnknown, DesktopProtocolError,
                         DesktopRequestError, DesktopUnavailable, FrameDecoder, MAX_FRAME_BYTES, encode_frame)


class FakePipe:
    def __init__(self):
        self.sent = []
        self.incoming = deque()
        self.closed = False
        self.respond = self.default_response

    def connect(self, timeout):
        pass

    def send(self, message):
        self.sent.append(message)
        if message["type"] == "request":
            response = self.respond(message)
            if response is not None:
                self.incoming.append(response)

    def default_response(self, message):
        method = message["method"]
        result = {"clientId": "pet-client"} if method == "initialize" else {}
        if method == "thread-follower-start-turn":
            result = {"result": {"turn": {"id": "turn-a", "status": "inProgress"}}}
        return {"type": "response", "requestId": message["requestId"], "method": method,
                "resultType": "success", "handledByClientId": "pet-client" if method == "initialize" else "desktop-owner",
                "result": result}

    def receive(self, timeout):
        return self.incoming.popleft() if self.incoming else None

    def close(self):
        self.closed = True


def broadcast(thread="thread-a", owner="desktop-owner", version=11, change=None, **extra):
    return {"type": "broadcast", "method": "thread-stream-state-changed", "version": version,
            "sourceClientId": owner, "params": {"hostId": "local", "conversationId": thread,
                "change": change or {"type": "snapshot", "revision": 1, "conversationState": {"id": thread}}}, **extra}


class FramingTests(unittest.TestCase):
    def test_fragmented_and_multiple_unicode_frames(self):
        frames = [{"name": "白龙"}, {"type": "snapshot", "n": 3}]
        wire = b"".join(encode_frame(value) for value in frames)
        decoder = FrameDecoder()
        result = []
        for byte in wire:
            result.extend(decoder.feed(bytes([byte])))
        self.assertEqual(result, frames)
        self.assertFalse(decoder.buffer)

    def test_invalid_length_json_and_nonobject_are_rejected(self):
        for wire in (struct.pack("<I", 0), struct.pack("<I", MAX_FRAME_BYTES + 1),
                     struct.pack("<I", 1) + b"{", struct.pack("<I", 2) + b"[]"):
            with self.subTest(wire=wire[:4]):
                with self.assertRaises(DesktopProtocolError):
                    FrameDecoder().feed(wire)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.pipe = FakePipe()
        self.client = DesktopIpcClient(self.pipe).connect()

    def test_initialize_and_owner_discovery_are_metadata_only(self):
        self.assertEqual(self.pipe.sent[0]["params"], {"clientType": "bailong"})
        self.assertEqual(self.client.discover_owner("thread-a"), "desktop-owner")
        request = self.pipe.sent[-1]
        self.assertEqual(request["version"], 1)
        self.assertEqual(request["params"], {"hostId": "local", "conversationId": "thread-a"})
        self.assertNotIn("hostId", request)
        self.assertFalse(self.client.owners)

    def test_no_owner_is_not_a_new_thread_or_send(self):
        self.pipe.respond = lambda m: {"type": "response", "requestId": m["requestId"],
                                      "resultType": "error", "error": "no-client-found"}
        self.assertIsNone(self.client.discover_owner("thread-a"))
        self.assertEqual(len(self.pipe.sent), 2)

    def test_subscription_is_targeted_and_only_its_owner_changes_survive(self):
        self.client.subscribe("thread-a", "desktop-owner")
        following = self.pipe.sent[-1]
        self.assertEqual(following["targetClientIds"], ["desktop-owner"])
        self.assertTrue(following["params"]["following"])
        self.pipe.incoming.extend([broadcast(thread="other"), broadcast(owner="other-owner"),
                                   broadcast(targetClientIds=["other-client"]), broadcast()])
        self.assertEqual(self.client.next_change("thread-a")["revision"], 1)
        self.assertFalse(self.client.changes)

    def test_start_turn_preserves_thread_and_inherits_app_settings(self):
        self.client.subscribe("thread-a", "desktop-owner")
        result = self.client.start_turn("thread-a", "hello", client_message_id="message-a")
        self.assertEqual(result["turn"]["id"], "turn-a")
        sent = self.pipe.sent[-1]
        self.assertEqual((sent["method"], sent["version"], sent["targetClientId"]),
                         ("thread-follower-start-turn", 2, "desktop-owner"))
        start = sent["params"]["turnStart"]
        self.assertEqual(start["request"], {"threadId": "thread-a", "input": [{"type": "text", "text": "hello"}],
                                            "clientUserMessageId": "message-a"})
        self.assertEqual(start["context"], {"inheritThreadSettings": True})
        self.assertNotIn("approvalPolicy", json.dumps(start))

    def test_deltas_received_before_turn_ack_are_queued(self):
        self.client.subscribe("thread-a", "desktop-owner")
        self.pipe.incoming.append(broadcast(change={"type": "patches", "baseRevision": 1, "revision": 2, "patches": []}))
        self.client.start_turn("thread-a", "hello")
        self.assertEqual(self.client.next_change("thread-a")["revision"], 2)

    def test_timeout_after_submit_is_unknown_and_is_never_replayed(self):
        self.client.subscribe("thread-a", "desktop-owner")
        self.pipe.respond = lambda m: None
        with self.assertRaises(DesktopOutcomeUnknown):
            self.client.start_turn("thread-a", "only once")
        self.assertEqual(sum(m.get("method") == "thread-follower-start-turn" for m in self.pipe.sent), 1)

    def test_router_timeout_is_also_unknown(self):
        self.client.subscribe("thread-a", "desktop-owner")
        self.pipe.respond = lambda m: {"type": "response", "requestId": m["requestId"],
                                      "resultType": "error", "error": "request-timeout"}
        with self.assertRaises(DesktopOutcomeUnknown):
            self.client.start_turn("thread-a", "hello")

    def test_explicit_owner_rejection_is_distinct_from_uncertainty(self):
        self.client.subscribe("thread-a", "desktop-owner")
        self.pipe.respond = lambda m: {"type": "response", "requestId": m["requestId"],
                                      "resultType": "error", "error": "request-version-mismatch"}
        with self.assertRaises(DesktopRequestError):
            self.client.start_turn("thread-a", "hello")

    def test_wrong_owner_response_cannot_confirm_mutation(self):
        self.client.subscribe("thread-a", "desktop-owner")
        def respond(message):
            response = self.pipe.default_response(message)
            response["handledByClientId"] = "different-owner"
            return response
        self.pipe.respond = respond
        with self.assertRaises(DesktopOutcomeUnknown):
            self.client.start_turn("thread-a", "hello")

    def test_stream_version_changes_fail_closed(self):
        self.client.subscribe("thread-a", "desktop-owner")
        self.pipe.incoming.append(broadcast(version=12))
        with self.assertRaises(DesktopProtocolError):
            self.client.next_change("thread-a")

    def test_owner_disconnect_is_reported(self):
        self.client.subscribe("thread-a", "desktop-owner")
        self.pipe.incoming.append({"type": "broadcast", "method": "client-status-changed",
                                   "params": {"status": "disconnected", "clientId": "desktop-owner"}})
        with self.assertRaises(DesktopUnavailable):
            self.client.next_change("thread-a")

    def test_unrelated_discovery_cannot_make_pet_owner(self):
        self.client.subscribe("thread-a", "desktop-owner")
        self.pipe.incoming.append({"type": "client-discovery-request", "requestId": "test-request"})
        self.assertIsNone(self.client.next_change("thread-a"))
        self.assertEqual(self.pipe.sent[-1]["response"], {"canHandle": False})

    def test_interrupt_is_expected_turn_scoped(self):
        self.client.subscribe("thread-a", "desktop-owner")
        self.client.interrupt("thread-a", "turn-a")
        sent = self.pipe.sent[-1]
        self.assertEqual(sent["version"], 4)
        self.assertEqual(sent["params"], {"conversationId": "thread-a", "expectedTurnId": "turn-a", "mode": "user-stop"})

    def test_close_unsubscribes_and_clears_all_content(self):
        self.client.subscribe("thread-a", "desktop-owner")
        self.client._dispatch(broadcast())
        self.client.close()
        self.assertFalse(self.pipe.sent[-1]["params"]["following"])
        self.assertFalse(self.client.changes)
        self.assertFalse(self.client.owners)
        self.assertTrue(self.pipe.closed)

    def test_must_subscribe_before_sending(self):
        with self.assertRaises(DesktopUnavailable):
            self.client.start_turn("thread-a", "hello")
        self.assertEqual(len(self.pipe.sent), 1)


if __name__ == "__main__":
    unittest.main()
