"""Keep the original pet's UI quiet during native hiding and graceful exit.

This mixin has no Qt imports. The bridge supplies the state, request and status
callbacks; the original Pet still owns animation, history and chat cleanup.
"""
from __future__ import annotations

import threading


class ExitWatchdog:
    """Give shutdown one fixed budget, independently of Qt's event loop."""
    def __init__(self, on_timeout, seconds=15.0):
        if seconds <= 0:
            raise ValueError("Shutdown timeout must be positive")
        self.seconds = seconds
        self._on_timeout = on_timeout
        self._lock = threading.Lock()
        self._timer = None
        self._cancelled = False

    def start(self):
        with self._lock:
            if self._timer is not None or self._cancelled:
                return False
            self._timer = threading.Timer(self.seconds, self._expired)
            self._timer.daemon = True
            self._timer.start()
            return True

    def _expired(self):
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
        self._on_timeout()

    def cancel(self):
        with self._lock:
            self._cancelled = True
            timer = self._timer
        if timer is not None:
            timer.cancel()


class PetLifecycle:
    native_sync_ready = False
    applying_native_visibility = False
    host_pid = None
    host_app_started_at = None
    exit_reason = None
    exit_host_pid = None
    exit_host_app_started_at = None
    _local_hide_pending = False
    _exit_watchdog = None

    def follow_host(self, state):
        """Learn this host's birth time without adopting a replacement host."""
        if self.exit_reason is not None:
            return False
        if state and state.get("running") is True and type(state.get("pid")) is int:
            started_at = state.get("appStartedAt")
            if self.host_pid is not None and (
                    state["pid"] != self.host_pid or
                    self.host_app_started_at is not None and started_at is not None
                    and started_at != self.host_app_started_at):
                self.request_exit("codex-closed")
                return False
            self.host_pid = state["pid"]
            if self.host_app_started_at is None:
                self.host_app_started_at = started_at
        return True

    def _hide_exit_ui(self):
        # Hide before chat.shutdown() can wait for a thread. Stop the original
        # fullscreen timer too: it must not show the composer while closing.
        for name in ("tick", "visibility_timer", "click_timer", "bubble_timer", "dock_click_timer", "_notice_timer", "_quota_hide_timer", "_quota_connect_timer", "_notification_timer"):
            timer = getattr(self, name, None)
            if timer is not None:
                timer.stop()
        self.hide()
        for name in ("tray", "bubble", "notification_bubble", "toolbar", "activity_cards", "tray_menu", "settings_panel", "quota_panel"):
            window = getattr(self, name, None)
            if window is not None:
                if hasattr(window, "set_active"):
                    window.set_active(False)
                window.hide()
        chat = getattr(self, "chat", None)
        if chat is not None:
            chat.closing = True
            chat.enabled = False
            for name in ("composer", "window"):
                window = getattr(chat, name, None)
                if window is not None:
                    window.hide()

    def _begin_exit(self, reason):
        if self.exit_reason is None:
            self.exit_reason = reason
            self.exit_host_pid = self.host_pid
            self.exit_host_app_started_at = self.host_app_started_at
            if self._exit_watchdog is not None:
                # Arm before any Qt call or chat cleanup can block. Retried
                # closeEvents must not renew this process's shutdown budget.
                self._exit_watchdog.start()
            self._hide_exit_ui()
            try:
                self._publish_closing()
            except OSError:
                pass  # A status-file collision must not prevent actual exit.
            if reason == "user":
                state = self._read_bridge_state()
                if (state and state.get("running") is True and state.get("pid") == self.exit_host_pid
                        and state.get("appStartedAt") == self.exit_host_app_started_at):
                    try:
                        self._request_native_visibility(False, state)
                    except OSError:
                        pass  # The supervisor also handles the final status.
        else:
            self._hide_exit_ui()

    def request_exit(self, reason):
        # The original close handler may retry after an asynchronous shutdown.
        # Only it schedules retries; observer ticks must not multiply them.
        if self.exit_reason is not None:
            return
        self._begin_exit(reason)
        self.close()

    def closeEvent(self, event):
        self._begin_exit("user")
        super().closeEvent(event)
        if event.isAccepted():
            # QApplication.quit() may be vetoed by an original chat window's
            # close handler. Cleanup has finished; exit the event loop itself.
            self._finish_exit()

    def desktop_hidden(self):
        return self.exit_reason is not None or super().desktop_hidden()

    def sync_dialogue_visibility(self):
        if self.exit_reason is not None:
            self._hide_exit_ui()
            return
        super().sync_dialogue_visibility()

    def apply_visibility(self):
        if self.exit_reason is not None:
            self._hide_exit_ui()
            return
        super().apply_visibility()

    def set_manual_hidden(self, hidden):
        if self.exit_reason is not None:
            return
        external = self.native_sync_ready and not self.applying_native_visibility
        if not external:
            if self.applying_native_visibility:
                # Codex remains the recovery entry even without a tray icon.
                # The standalone safety message must not veto a host Hide.
                self.manual_hidden = bool(hidden)
                return self.apply_visibility()
            return super().set_manual_hidden(hidden)
        state = self._read_bridge_state()
        # A local Hide wins over an old native-open sample until Hide has been
        # observed. An explicit tray Show cancels that intent immediately.
        self._local_hide_pending = bool(hidden)
        try:
            self._request_native_visibility(not hidden, state)
        except OSError:
            pass
        if hidden or self._native_can_show(state):
            self.manual_hidden = bool(hidden)
            self.apply_visibility()

    def apply_native_hidden(self, hidden):
        self.applying_native_visibility = True
        try:
            self.set_manual_hidden(hidden)
        finally:
            self.applying_native_visibility = False

    def allow_native_visible(self, state, active):
        if self.exit_reason is not None:
            return False
        if self._local_hide_pending:
            if state and state.get("overlayOpen") is False:
                self._local_hide_pending = False
            else:
                return False
        return active
