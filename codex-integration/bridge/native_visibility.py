"""Keep the native Mini toggle authoritative while rendering the full pet.

Only the native Mini's drawing region is masked. Show/Hide actions are issued
on a user request or a one-time startup restoration, never to suppress artwork.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time
import uuid

STATE_NAME = "native-visibility-state.json"
REQUEST_NAME = "native-visibility-request.json"
GENERATION_NAME = "observer-generation.json"
IO_GRACE_SECONDS = 1.0


class BoundedStateFile:
    """Retry transient sharing failures without treating them as a Hide event.

    Only I/O failures reuse a verified snapshot, for at most one second. A
    successfully read invalid/stale value always invalidates it immediately.
    The generation/minimum-mtime key prevents reusing a previous observer run.
    """
    def __init__(self, max_bytes=65536, max_age=15.0, validator=None):
        self.max_bytes, self.max_age, self.validator = max_bytes, max_age, validator
        self.key = None
        self.value = None
        self.verified_at = None
        self.modified_at = None
        self.reused = False
        self.reason = None

    def clear(self):
        self.value = self.verified_at = self.modified_at = None
        self.reused = False

    def read(self, path, *, now=None, monotonic_now=None, generation=None, minimum_mtime=0.0):
        path = Path(path)
        now = time.time() if now is None else now
        monotonic_now = time.monotonic() if monotonic_now is None else monotonic_now
        key = (str(path.absolute()), generation, minimum_mtime)
        if key != self.key:
            self.clear()
            self.key = key
        self.reused = False
        for attempt in range(3):
            try:
                with path.open("r", encoding="utf-8-sig") as stream:
                    info = os.fstat(stream.fileno())
                    if info.st_size > self.max_bytes:
                        self.reason = "oversize"
                        self.clear()
                        return None
                    if info.st_mtime < minimum_mtime:
                        self.reason = "generation"
                        self.clear()
                        return None
                    if self.max_age is not None and info.st_mtime > now + 2:
                        self.reason = "future"
                        self.clear()
                        return None
                    if self.max_age is not None and now - info.st_mtime > self.max_age:
                        self.reason = "stale"
                        self.clear()
                        return None
                    value = json.load(stream)
                    if not isinstance(value, dict) or (self.validator and not self.validator(value)):
                        raise ValueError("invalid state schema")
                self.value = value
                self.verified_at, self.modified_at = monotonic_now, info.st_mtime
                self.reason = None
                return dict(value)
            except OSError:
                if attempt < 2:
                    time.sleep(.01)
                    continue
            except (ValueError, TypeError):
                self.reason = "invalid"
                self.clear()
                return None
            break
        self.reason = "io"
        if (self.value is not None and self.verified_at is not None
                and 0 <= monotonic_now - self.verified_at <= IO_GRACE_SECONDS
                and self.modified_at >= minimum_mtime
                and (self.max_age is None or self.modified_at <= now + 2)
                and (self.max_age is None or now - self.modified_at <= self.max_age)):
            self.reused = True
            return dict(self.value)
        self.clear()
        return None


def valid_observer_state(value):
    return (type(value.get("running")) is bool and type(value.get("overlayOpen")) is bool
            and (not value["running"] or type(value.get("pid")) is int and value["pid"] > 0))


_GENERATION_READER = BoundedStateFile(max_bytes=1024, max_age=None,
    validator=lambda value: isinstance(value.get("generation"), str)
    and bool(value["generation"]) and type(value.get("minimumMtime")) in (int, float))
_MASK_READER = BoundedStateFile(max_bytes=16384, max_age=6.0)


def publish_observer_generation(base, minimum_mtime):
    token = uuid.uuid4().hex
    _write(Path(base) / GENERATION_NAME, {"generation": token, "minimumMtime": minimum_mtime})
    return token


def observer_generation(base, *, now=None, monotonic_now=None):
    return _GENERATION_READER.read(Path(base) / GENERATION_NAME, now=now, monotonic_now=monotonic_now)


def _read(path):
    # Requests are not cached or replayed, but a transient replace collision
    # gets the same short bounded retry as observation/status reads.
    return BoundedStateFile(max_bytes=16384, max_age=None).read(path) or {}


def _write(path, value):
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=True), encoding="utf-8")
        for attempt in range(3):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(.01)
    finally:
        temporary.unlink(missing_ok=True)


def request_visibility(base, visible, state):
    if not state or type(state.get("pid")) is not int:
        return False
    _write(Path(base) / REQUEST_NAME, {
        "id": uuid.uuid4().hex, "at": time.time(), "pid": state["pid"],
        "appStartedAt": state.get("appStartedAt"),
        "selectedAvatarId": state.get("selectedAvatarId"),
        "visible": bool(visible),
    })
    return True


def mask_ready(base, state):
    if not state:
        return False
    generation = state.get("_observerGeneration")
    identity = (generation, state.get("pid"), state.get("appStartedAt"),
                state.get("overlayHwnd"), state.get("selectedAvatarId"))
    data = _MASK_READER.read(Path(base) / STATE_NAME, generation=identity) or {}
    stamp = data.get("at")
    mask = data.get("mask", {})
    return bool(data.get("observerGeneration") == generation
                and isinstance(stamp, (int, float)) and 0 <= time.time() - stamp <= 6
                and isinstance(mask, dict) and mask.get("masked") is True
                and mask.get("pid") == state.get("pid")
                and mask.get("hwnd") == state.get("overlayHwnd"))


class NativeMiniController:
    MAX_ATTEMPTS = 3
    HELPER_TIMEOUT_SECONDS = 6.0
    # Retry only a failure proved to precede input delivery. An unknown result
    # must never repeat a global shortcut or a UI Automation invocation.
    RETRYABLE_REASONS = frozenset({"shortcut-keys-held", "main-window-not-ready"})

    def __init__(self, base, logger, launcher=None, command_builder=None, mask=None, show_on_start=False,
                 previous_host=None):
        if launcher is None or command_builder is None:
            from bridge_supervisor import launch_native_control, native_control_command
            launcher = launcher or launch_native_control
            command_builder = command_builder or native_control_command
        if mask is None:
            from native_overlay_mask import NativeOverlayMask
            mask = NativeOverlayMask()
        self.base, self.logger = Path(base), logger
        self.launcher, self.command_builder, self.mask = launcher, command_builder, mask
        self.process = None
        self.pending_action = None
        self._process_started = None
        self._process_selection = None
        self.last_result = None
        self.phase = "waiting-for-selection"
        self._selection = None
        self._seen_open = False
        self._desired = None
        self._desired_source = None
        self._attempts = 0
        self._readiness_since = None
        self._next_action = 0.0
        self._started = time.time()
        self._show_on_start = bool(show_on_start)
        self._last_host = previous_host
        self._host_was_absent = False
        self._host_start_pending = False
        self._request_id = _read(self.base / REQUEST_NAME).get("id")
        self._last_report = 0.0
        self._last_signature = None
        self._generation = None
        self._last_trusted_observation = None
        self._last_trusted_at = None

    def set_generation(self, generation):
        if generation == self._generation:
            return
        self._cancel_helper("observer-generation-changed")
        self.mask.restore()
        self._generation = generation
        self._last_trusted_observation = self._last_trusted_at = None
        self._selection = None
        self._desired = self._desired_source = None
        self._seen_open = False
        self.phase = "waiting-for-observer-generation"
        self._report(time.monotonic(), None)

    def _cancel_helper(self, reason):
        """Stop only our own one-shot helper, before it can deliver stale input."""
        process = self.process
        if process is None:
            return
        action = self.pending_action
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=.5)
        except (OSError, subprocess.TimeoutExpired):
            self.logger.warning("native visibility helper cancellation failed")
        finally:
            if process.stdout is not None:
                process.stdout.close()
            self.process = self.pending_action = None
            self._process_started = self._process_selection = None
            self.last_result = {"action": action, "acted": False,
                                "reason": reason, "outcomeUnknown": True}

    def _poll(self, now):
        if (self.process is not None and self.process.poll() is None
                and self._process_started is not None
                and now - self._process_started >= self.HELPER_TIMEOUT_SECONDS):
            self._cancel_helper("helper-timeout-no-replay")
            self._desired = self._desired_source = None
            return
        if self.process is None or self.process.poll() is None:
            return
        action = self.pending_action
        try:
            raw = self.process.stdout.read(8193)
            if len(raw) > 8192:
                raise ValueError("oversized response")
            value = json.loads(raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw)
            self.last_result = {"action": action, "acted": value.get("acted") is True,
                                "method": str(value.get("method", ""))[:80],
                                "reason": str(value.get("reason", ""))[:160]}
            opened = value.get("openedOverlayHwnd")
            if (action == "Show" and value.get("acted") is True
                    and value.get("method") == "global-hotkey" and type(opened) is int and opened > 0
                    and self._process_selection is not None and self._process_selection == self._selection):
                self.mask.mask(opened, self._process_selection[0])
        except (OSError, ValueError, AttributeError):
            self.last_result = {"action": action, "acted": False, "reason": "invalid-response"}
        finally:
            if self.process.stdout is not None:
                self.process.stdout.close()
            self.process = None
            self.pending_action = None
            self._process_started = self._process_selection = None
        self._next_action = now + 1.0
        if self.last_result.get("reason") == "main-window-not-ready":
            if self._readiness_since is None:
                self._readiness_since = now
            if now - self._readiness_since < 30:
                self._attempts = max(0, self._attempts - 1)
            else:
                self._desired = self._desired_source = None
        if (self.last_result.get("reason") not in self.RETRYABLE_REASONS and self._desired is not None
                and action == ("Show" if self._desired else "Hide")):
            # An observed successful Show may already have been followed by a
            # user's Hide before the observer samples it. Unknown/failed
            # delivery is also never replayed without a new user request.
            self._desired = None
            self._desired_source = None

    def _user_request(self, observation):
        value = _read(self.base / REQUEST_NAME)
        identifier = value.get("id")
        if not identifier or identifier == self._request_id:
            return
        self._request_id = identifier
        stamp = value.get("at")
        if (not isinstance(stamp, (int, float)) or stamp < self._started
                or not 0 <= time.time() - stamp <= 15
                or value.get("pid") != observation.pid
                or (value.get("appStartedAt") is not None and
                    value.get("appStartedAt") != getattr(observation, "app_started_at", None))
                or value.get("selectedAvatarId") != observation.selected_avatar_id
                or type(value.get("visible")) is not bool):
            return
        self._desired = value["visible"]
        self._desired_source = "user"
        self._readiness_since = None
        self._attempts = 0
        self._next_action = 0.0
        if self.process is not None and self.pending_action != ("Show" if self._desired else "Hide"):
            self._cancel_helper("superseded-by-user-request")

    def _report(self, now, observation):
        value = {"phase": self.phase, "mask": self.mask.status(), "observerGeneration": self._generation,
                 "nativeOpen": bool(observation and observation.overlay_open),
                 "desired": self._desired, "lastResult": self.last_result}
        # High-frequency mask counters are diagnostic only. Identity/failure
        # changes report immediately; counters piggyback on the 2-second tick.
        signature_value = {**value, "mask": {key: item for key, item in value["mask"].items()
                                            if key not in ("maskCount", "reapplyCount", "restoreCount")}}
        signature = json.dumps(signature_value, sort_keys=True)
        if signature != self._last_signature or now - self._last_report >= 2:
            try:
                _write(self.base / STATE_NAME, {**value, "at": time.time()})
                self._last_signature, self._last_report = signature, now
            except OSError:
                self.logger.warning("native visibility status write failed")

    def tick(self, observation, now, source_failure=None):
        self._poll(now)
        if observation is not None:
            if not observation.running:
                self._host_was_absent = True
            elif observation.pid:
                started = getattr(observation, "app_started_at", None)
                changed = bool(self._last_host is not None and (
                    observation.pid != self._last_host[0] or
                    started is not None and self._last_host[1] is not None and started != self._last_host[1]))
                if started is None and self._last_host is not None and observation.pid == self._last_host[0]:
                    started = self._last_host[1]
                host = (observation.pid, started)
                if changed or self._host_was_absent:
                    self._host_start_pending = True
                self._last_host = host
                self._host_was_absent = False
        known = observation is not None and observation.selection_known and observation.running
        uncertain = observation is None or (observation.running and not observation.selection_known)
        hard_failure = source_failure in ("stale", "generation", "invalid", "oversize", "future", "observer-stopped")
        if uncertain and not hard_failure and self._last_trusted_observation is not None:
            previous = self._last_trusted_observation
            same_identity = (observation is None or
                observation.pid == previous.pid and
                observation.selected_avatar_id in (None, previous.selected_avatar_id) and
                observation.native_pet_visible is not False)
            if (same_identity and self._last_trusted_at is not None
                    and 0 <= now - self._last_trusted_at <= 2.0):
                self.phase = "waiting-for-fresh-selection"
                self._report(now, observation or previous)
                return  # Preserve the owned mask; never send actions on uncertainty.
        if not known or not observation.full_selected:
            if known:
                self._host_start_pending = False
            self.mask.restore()
            self._cancel_helper("target-no-longer-active")
            self._desired = None
            self._desired_source = None
            self._selection = None
            self._seen_open = False
            self._last_trusted_observation = self._last_trusted_at = None
            self.phase = "native-mode" if known else "waiting-for-selection"
            self._report(now, observation)
            return
        self._last_trusted_observation = observation
        self._last_trusted_at = getattr(observation, "verified_at", None)
        if self._last_trusted_at is None:
            self._last_trusted_at = now
        selection = (observation.pid, observation.selected_avatar_id)
        if self.process is not None and self._process_selection != selection:
            self._cancel_helper("target-selection-changed")
        if selection != self._selection:
            self._readiness_since = None
            self._selection = selection
            self._seen_open = observation.overlay_open
            # A legacy bridge may have closed a Mini whose persistent intent is
            # still open. Restore that once; never undo a later explicit Hide.
            self._desired = True if observation.requested_open is True and not observation.overlay_open else None
            self._desired_source = "startup" if self._desired is not None else None
            self._attempts = 0
            self._next_action = 0.0
        if self._show_on_start:
            self._show_on_start = False
            if not observation.overlay_open:
                self._desired = True
                self._desired_source = "installation"
                self._attempts = 0
                self._next_action = 0.0
        if self._host_start_pending:
            self._host_start_pending = False
            if not observation.overlay_open:
                self._desired = True
                self._desired_source = "codex-restarted"
                self._attempts = 0
                self._next_action = 0.0
        if observation.overlay_hwnd:
            self.mask.mask(observation.overlay_hwnd, observation.pid)
        self._user_request(observation)
        if self._desired_source == "startup" and observation.requested_open is False:
            self._desired = None
            self._desired_source = None
        if observation.overlay_open:
            self._seen_open = True
        if self._desired is not None and observation.overlay_open == self._desired:
            self._desired = None
            self._desired_source = None
        if self.process is not None:
            self.phase = "changing-native-visibility"
        elif self._desired is not None and self._attempts < self.MAX_ATTEMPTS and now >= self._next_action:
            action = "Show" if self._desired else "Hide"
            try:
                command = self.command_builder(self.base, action, observation)
                if self._desired_source == "startup":
                    command = [*command, "-OnlyWhenPersistedOpen"]
                self.process = self.launcher(command, self.base)
                self.pending_action = action
                self._process_started = now
                self._process_selection = selection
                self._attempts += 1
                self._next_action = now + 1.0
                self.phase = "changing-native-visibility"
            except (OSError, ValueError) as error:
                self.last_result = {"action": action, "acted": False, "reason": type(error).__name__}
                self._attempts += 1
                self._next_action = now + 2.0
        elif self._desired is not None:
            self.phase = "waiting-for-native-toggle" if self._attempts < self.MAX_ATTEMPTS else "native-toggle-unavailable"
        else:
            self.phase = "following-native-toggle"
        self._report(now, observation)

    def status(self):
        return {"phase": self.phase, "helperPid": self.process.pid if self.process else None,
                "pendingAction": self.pending_action, "lastResult": self.last_result,
                "mask": self.mask.status(), "desired": self._desired,
                "lastHost": list(self._last_host) if self._last_host is not None else None}

    def close(self):
        self._cancel_helper("controller-closed")
        self.mask.close()
        self._report(time.monotonic(), None)
