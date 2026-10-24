"""Full Bailong companion with its own attached controls.

Codex's dedicated list entry selects this mode. The native Mini is managed
separately by the supervisor; its geometry never controls or moves this pet.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
import traceback

from bridge_policy import full_mode_visible
from pet_lifecycle import PetLifecycle
from native_visibility import (BoundedStateFile, mask_ready, observer_generation,
                               request_visibility, valid_observer_state)

DIRECTORY = Path(__file__).resolve().parent
STATE_FILE = DIRECTORY / "codex-state.json"
STOP_FILE = DIRECTORY / "pet.stop"
STATUS_FILE = DIRECTORY / "pet-status.json"
LAYOUT_FILE = DIRECTORY / "bridge-layout.json"
_STATE_READER = BoundedStateFile(max_age=15.0, validator=valid_observer_state)
_LAST_SELECTION = None
_LAST_SELECTION_AT = None
_LAST_SELECTION_CONTEXT = None


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for attempt in range(3):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(0.01)


def save_status(value):
    write_json(STATUS_FILE, value)


def read_state(*, now=None, monotonic_now=None):
    global _LAST_SELECTION, _LAST_SELECTION_AT, _LAST_SELECTION_CONTEXT
    now = time.time() if now is None else now
    monotonic_now = time.monotonic() if monotonic_now is None else monotonic_now
    generation = observer_generation(DIRECTORY, now=now, monotonic_now=monotonic_now)
    if generation is None:
        _STATE_READER.clear()
        _LAST_SELECTION = _LAST_SELECTION_AT = _LAST_SELECTION_CONTEXT = None
        return None
    value = _STATE_READER.read(STATE_FILE, now=now, monotonic_now=monotonic_now,
                               generation=generation["generation"], minimum_mtime=generation["minimumMtime"])
    if value is None:
        return None
    value["_observerGeneration"] = generation["generation"]
    context = (str(STATE_FILE), generation["generation"], value.get("pid"), value.get("appStartedAt"))
    if context != _LAST_SELECTION_CONTEXT:
        _LAST_SELECTION = _LAST_SELECTION_AT = None
        _LAST_SELECTION_CONTEXT = context
    if value.get("selectionKnown") is True:
        _LAST_SELECTION = dict(value)
        _LAST_SELECTION_AT = _STATE_READER.verified_at
        return value
    # A failed metadata projection is not a user choice. Keep only the last
    # verified selection fields briefly; explicit Hide/None/new IDs win now.
    explicit_change = (value.get("running") is False or value.get("overlayOpen") is False
                       or value.get("nativePetVisible") is False
                       or _LAST_SELECTION is not None and value.get("selectedAvatarId") not in
                       (None, _LAST_SELECTION.get("selectedAvatarId")))
    if explicit_change:
        _LAST_SELECTION = _LAST_SELECTION_AT = None
        return value
    if (_LAST_SELECTION is not None and _LAST_SELECTION_AT is not None
            and 0 <= monotonic_now - _LAST_SELECTION_AT <= 1.0):
        for key in ("selectionKnown", "selectedAvatarId", "nativePetVisible", "fullBailongMigratedIds"):
            if key in _LAST_SELECTION:
                value[key] = _LAST_SELECTION[key]
    return value


def log_error():
    path = DIRECTORY / "pet-errors.log"
    if path.exists() and path.stat().st_size > 65536:
        path.write_text(path.read_text(encoding="utf-8")[-16000:], encoding="utf-8")
    with path.open("a", encoding="utf-8") as log:
        log.write(traceback.format_exc() + "\n")


def main():
    from bridge_loader import load_original_module
    original = load_original_module()
    from desktop_chat_link import install_desktop_chat_link
    install_desktop_chat_link(original)
    from asset_source import configure_asset_source
    asset_status = configure_asset_source(original)
    from bailong_toolbar import create_toolbar
    from activity_cards import create_activity_cards
    from activity_animation import ExternalActivityDriver
    from activity_runtime import ActivityRuntime
    app = original.QApplication([str(Path(__file__).resolve())])
    app.setQuitOnLastWindowClosed(False)

    connection = original.QLocalSocket()
    connection.connectToServer("bailong-desktop-pet-v1")
    if connection.waitForConnected(200):
        connection.disconnectFromServer()
        save_status({"pid": os.getpid(), "phase": "existing-pet", "updatedAt": time.time()})
        return 0
    server = original.QLocalServer()
    original.QLocalServer.removeServer("bailong-desktop-pet-v1")
    if not server.listen("bailong-desktop-pet-v1"):
        raise RuntimeError("Could not acquire the original pet singleton endpoint")

    save_status({"pid": os.getpid(), "phase": "loading-animations", "updatedAt": time.time(), **asset_status})

    starting_state = read_state()
    starting_host = starting_state.get("pid") if starting_state else None

    class LoadingCancelled(Exception):
        def __init__(self, reason):
            self.reason = reason

    def check_loading_host():
        nonlocal starting_host
        if STOP_FILE.exists():
            raise LoadingCancelled("supervisor-stop")
        state = read_state()
        if state and state.get("running") is False:
            raise LoadingCancelled("codex-closed")
        if state and state.get("running") is True:
            if starting_host is not None and state.get("pid") != starting_host:
                raise LoadingCancelled("codex-closed")
            starting_host = state.get("pid")

    class LinkedPet(PetLifecycle, original.Pet):
        toolbar = None
        activity_driver = None
        loading = True
        _read_bridge_state = staticmethod(read_state)
        _finish_exit = staticmethod(lambda: original.QApplication.exit(0))

        def _request_native_visibility(self, visible, state):
            request_visibility(DIRECTORY, visible, state)

        def _native_can_show(self, state):
            return full_mode_visible(state) and mask_ready(DIRECTORY, state)

        def _publish_closing(self):
            save_status({"pid": os.getpid(), "phase": "closing", "updatedAt": time.time(),
                         "exitReason": self.exit_reason, "exitHostPid": self.exit_host_pid,
                         "visible": False, "toolbarVisible": False, "trayVisible": False})

        def load(self, item):
            if self.loading:
                check_loading_host()
            frames = super().load(item)
            if self.loading:
                check_loading_host()
            return frames

        def idle(self):
            if self.activity_driver is not None and self.activity_driver.on_idle():
                return
            super().idle()

        def constrained_position(self, position, pointer=None):
            point = super().constrained_position(position, pointer)
            if self.toolbar is not None:
                point = self.toolbar.constrained_owner_position(point, pointer)
            return point

    try:
        check_loading_host()
        widget = LinkedPet()
        check_loading_host()
        widget.loading = False
    except LoadingCancelled as cancelled:
        # Construction has not entered app.exec(). Retire its loading dialog
        # and any partially prepared tray, instead of decoding all 2529 frames
        # after the host has already closed.
        for window in app.topLevelWidgets():
            window.hide()
            tray = getattr(window, "tray", None)
            if tray is not None:
                tray.hide()
        server.close()
        save_status({"pid": os.getpid(), "phase": "stopped", "updatedAt": time.time(),
                     "exitReason": cancelled.reason, "exitHostPid": starting_host})
        return 0
    initial_state = read_state()
    if initial_state and type(initial_state.get("pid")) is int:
        widget.host_pid = initial_state["pid"]
    widget.native_sync_ready = True
    toolbar = create_toolbar(original, widget)
    activity_cards = create_activity_cards(original, widget, on_activate=lambda key: activity_runtime.activate(key))
    widget.activity_cards = activity_cards
    activity_driver = ExternalActivityDriver(original, widget,
                                              is_popup_open=lambda: bool(original.QApplication.activePopupWidget()))
    widget.activity_driver = activity_driver
    activity_runtime = ActivityRuntime(original, widget, activity_cards, activity_driver)
    loaded = {Path(key).name: len(frames) for key, frames in widget.pinned_frames.items()}
    try:
        position = json.loads(LAYOUT_FILE.read_text(encoding="utf-8")).get("position")
        if (isinstance(position, list) and len(position) == 2
                and all(type(number) is int for number in position)):
            widget.move(widget.constrained_position(original.QPoint(*position)))
    except (OSError, ValueError):
        pass

    class FollowSelection:
        def __init__(self):
            self.last_active = None
            self.last_native_open = None
            self.last_report = 0.0
            self.last_signature = None
            self.last_position = None
            self.timer = original.QTimer()
            self.timer.setInterval(300)
            self.timer.timeout.connect(self.update)
            self.timer.start()

        def update(self):
            try:
                if widget.exit_reason is not None:
                    return
                if STOP_FILE.exists():
                    toolbar.set_active(False)
                    activity_cards.set_active(False)
                    widget.request_exit("supervisor-stop")
                    return
                state = read_state()
                if state and state.get("running") is True and type(state.get("pid")) is int:
                    widget.host_pid = state["pid"]
                active = widget.allow_native_visible(state, full_mode_visible(state) and mask_ready(DIRECTORY, state))
                if state and state.get("running") is False:
                    widget.request_exit("codex-closed")
                    return

                native_open = bool(state and state.get("overlayOpen"))
                show_request = native_open and self.last_native_open is not True
                if not active:
                    toolbar.set_active(False)
                    if not widget.manual_hidden:
                        widget.apply_native_hidden(True)
                elif self.last_active is not True or show_request:
                    widget.apply_native_hidden(False)
                toolbar.sync_visibility(active)
                activity_runtime.poll(active, state)
                self.last_active = active
                if state is not None:
                    self.last_native_open = native_open

                position = [widget.x(), widget.y()]
                if active and not widget.dragging and position != self.last_position:
                    write_json(LAYOUT_FILE, {"position": position})
                    self.last_position = position

                signature = (active, bool(widget.manual_hidden), bool(widget.fullscreen_hidden),
                             widget.isVisible(), toolbar.isVisible(), *position,
                             len(activity_cards.sessions), activity_driver.state, activity_runtime.available,
                             widget.bubble.isVisible(), widget.bubble_timer.isActive())
                if signature != self.last_signature or time.monotonic() - self.last_report >= 5:
                    save_status({
                        "pid": os.getpid(), "phase": "running", "updatedAt": time.time(),
                        "mode": "dedicated-toolbar", "fullSelected": active,
                        "observerFresh": state is not None,
                        "manualHidden": bool(widget.manual_hidden),
                        "fullscreenHidden": bool(widget.fullscreen_hidden),
                        "visible": widget.isVisible(), "position": position,
                        "trayVisible": widget.tray.isVisible(),
                        "windowId": int(widget.winId()),
                        "toolbarVisible": toolbar.isVisible(), "toolbarWindowId": int(toolbar.winId()),
                        "toolbarPosition": [toolbar.x(), toolbar.y()],
                        "toolbarSize": [toolbar.width(), toolbar.height()],
                        "loadedAnimations": len(loaded), "loadedFrames": sum(loaded.values()),
                        "framesByAnimation": loaded, "interactionVariants": len(widget.variants),
                        "originalEngine": True, "nativeMiniGeometryModified": False,
                        "chatBusy": bool(getattr(getattr(widget, 'chat', None), 'busy', False)),
                        "chatTransport": "codex-desktop-owner-stream",
                        **asset_status,
                        **activity_runtime.diagnostics(),
                        "externalActivityState": activity_driver.state,
                        "externalActivityAnimation": activity_driver.last_action,
                        "externalActivityError": activity_driver.last_error,
                        "activityCardsVisible": activity_cards.isVisible(),
                        "activityCardsWindowId": int(activity_cards.winId()),
                        "quotaResultVisible": bool(getattr(widget.bubble, 'is_quota_result', False) and widget.bubble.isVisible()),
                        "originalBubbleVisible": widget.bubble.isVisible(),
                        "originalBubbleRemainingMs": widget.bubble_timer.remainingTime(),
                    })
                    self.last_report = time.monotonic()
                    self.last_signature = signature
            except Exception:
                log_error()

    follower = FollowSelection()

    def reveal_existing():
        while server.hasPendingConnections():
            socket = server.nextPendingConnection()
            if socket:
                socket.disconnectFromServer()
                socket.deleteLater()
        if full_mode_visible(read_state()):
            follower.last_active = None
            follower.update()

    server.newConnection.connect(reveal_existing)
    follower.update()
    # A host can close while the original constructor preloads animations.
    # exit() before exec() has no effect; do not enter a new loop after cleanup.
    result = 0 if widget.exit_reason is not None else app.exec()
    activity_runtime.close()
    server.close()
    save_status({"pid": os.getpid(), "phase": "stopped", "updatedAt": time.time(),
                 "exitReason": widget.exit_reason or "user", "exitHostPid": widget.exit_host_pid or widget.host_pid})
    return result


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        log_error()
        save_status({"pid": os.getpid(), "phase": "error", "updatedAt": time.time(), "exitReason": "error"})
        raise
