"""Bottom status bar: message, global progress, elapsed/ETA, pause, cancel, log toggle."""

from __future__ import annotations

import time
from collections.abc import Callable

from nicegui import ui

from slidecast.core.timeline import format_duration
from slidecast.gui.ui_kit import button, tip


class StatusBar:
    def __init__(
        self,
        on_cancel: Callable[[], None],
        on_pause: Callable[[bool], None],
        on_log: Callable[[bool], None],
    ) -> None:
        self._on_pause = on_pause
        self._on_log = on_log
        self._started: float | None = None
        self._fraction = 0.0
        self._paused_at: float | None = None
        self._paused_total = 0.0
        self._last_message = ""
        self._background = False
        self._cancelling = False
        self._has_progress = False
        self._log_open = False

        with ui.column().classes("w-full gap-2 px-4 py-2"):
            with ui.row().classes("w-full items-center no-wrap gap-3"):
                self.state_label = ui.label("Ready").classes("sc-badge sc-badge-idle")
                self.message = ui.label("No task running").classes("sc-ellipsis flex-1")
                self.pause_btn = button("Pause", self.toggle_pause, small=True)
                tip(
                    self.pause_btn,
                    "Pause after the current slide/deck finishes. Cached work is kept; "
                    "Resume continues.",
                )
                self.cancel_btn = button("Cancel", on_cancel, kind="danger", small=True)
                self.log_btn = button("Log", self.toggle_log, small=True)
            with ui.row().classes("w-full items-center no-wrap gap-3") as self.progress_panel:
                self.bar = ui.linear_progress(value=0, show_value=False).props(
                    "instant-feedback rounded size=6px"
                )
                self.bar.style("max-width: 320px; min-width: 120px")
                self.percent = ui.label("").classes("sc-muted").style("min-width: 56px")
                self.timing = ui.label("").classes("sc-muted")
        self.pause_btn.set_visibility(False)
        self.cancel_btn.set_visibility(False)
        self.progress_panel.set_visibility(False)
        ui.timer(0.5, self._tick)

    # -- state --------------------------------------------------------------------------------

    def _set_state(self, text: str, state: str) -> None:
        self.state_label.set_text(text)
        self.state_label.classes(replace=f"sc-badge sc-badge-{state}")

    def _set_message(self, text: str) -> None:
        self.message.set_text(text)
        tip(self.message, text)

    def set_message(self, text: str) -> None:
        """Transient notice from the app (not a task's own progress)."""
        self._set_message(text)

    def _indeterminate(self, on: bool) -> None:
        if on:
            self.bar.props("indeterminate")
        else:
            self.bar.props(remove="indeterminate")

    def start(self, message: str, *, background: bool = False) -> None:
        self._started = time.monotonic()
        self._fraction = 0.0
        self._paused_at = None
        self._paused_total = 0.0
        self._last_message = message
        self._background = background
        self._cancelling = False
        self._has_progress = False
        self.bar.set_value(0)
        self._indeterminate(True)
        self.percent.set_text("Starting")
        self.timing.set_text("")
        self._set_message(message)
        self._set_state("Checking" if background else "Running", "running")
        self.progress_panel.set_visibility(not background)
        self.pause_btn.set_visibility(not background)
        self.pause_btn.set_enabled(True)
        self.pause_btn.set_text("Pause")
        self.cancel_btn.set_visibility(not background)
        self.cancel_btn.set_enabled(True)
        self._tick()

    def update_progress(self, fraction: float, message: str) -> None:
        if self._started is None:
            return
        self._fraction = max(0.0, min(fraction, 1.0))
        self._has_progress = True
        self._indeterminate(False)
        self.bar.set_value(self._fraction)
        self.percent.set_text(f"{self._fraction:.0%}")
        self._last_message = message
        if self._paused_at is None and not self._cancelling:
            self._set_message(message)

    # -- pause --------------------------------------------------------------------------------

    def toggle_pause(self) -> None:
        if self._started is None or self._background or self._cancelling:
            return
        paused = self._paused_at is None
        self.set_paused_ui(paused)
        self._on_pause(paused)

    def set_paused_ui(self, paused: bool) -> None:
        """Reflect paused state without re-emitting (also used when something else pauses us)."""
        if (
            self._started is None
            or self._background
            or self._cancelling
            or paused == (self._paused_at is not None)
        ):
            return
        if paused:
            self._paused_at = time.monotonic()
            self.pause_btn.set_text("Resume")
            self._set_message(f"Paused - {self._last_message}")
            self._set_state("Paused", "paused")
            if not self._has_progress:
                self._indeterminate(False)
        else:
            if self._paused_at is not None:
                self._paused_total += time.monotonic() - self._paused_at
            self._paused_at = None
            self.pause_btn.set_text("Pause")
            self._set_message(self._last_message)
            self._set_state("Running", "running")
            if not self._has_progress:
                self._indeterminate(True)
        self._tick()

    def cancelling(self) -> None:
        if self._started is None:
            return
        self._cancelling = True
        self.cancel_btn.set_enabled(False)
        self.pause_btn.set_enabled(False)
        self._set_state("Stopping", "paused")
        self._set_message("Cancelling...")

    def finish(self, message: str, ok: bool = True) -> None:
        elapsed = self._working_seconds()
        background = self._background
        self._paused_at = None
        self._paused_total = 0.0
        self._started = None
        self._cancelling = False
        self._background = False
        self.pause_btn.set_text("Pause")
        self.pause_btn.set_visibility(False)
        self.cancel_btn.set_visibility(False)
        self.progress_panel.set_visibility(False)
        self._indeterminate(False)
        self.bar.set_value(0)
        self.percent.set_text("")
        self.timing.set_text("")
        if background and ok:
            self._set_state("Ready", "idle")
            self._set_message("No task running")
            return
        cancelled = not ok and "cancelled" in message.casefold()
        self._set_state(
            "Complete" if ok else "Stopped" if cancelled else "Failed",
            "success" if ok else "idle" if cancelled else "error",
        )
        self.message.set_text(message)
        tip(
            self.message,
            message if background else f"{message}\nElapsed {format_duration(elapsed)}",
        )

    # -- log toggle ---------------------------------------------------------------------------

    def toggle_log(self) -> None:
        self._log_open = not self._log_open
        self.log_btn.props(f"color={'primary' if self._log_open else 'grey-5'}")
        self._on_log(self._log_open)

    # -- timing -------------------------------------------------------------------------------

    def _working_seconds(self) -> float:
        """Elapsed time with paused stretches removed, so the ETA stays meaningful."""
        if self._started is None:
            return 0.0
        paused = self._paused_total
        if self._paused_at is not None:
            paused += time.monotonic() - self._paused_at
        return max(time.monotonic() - self._started - paused, 0.0)

    def _tick(self) -> None:
        if self._started is None or self._background:
            return
        elapsed = self._working_seconds()
        if self._paused_at is not None:
            text = f"Elapsed {format_duration(elapsed)}   paused"
        elif 0.02 < self._fraction < 1.0:
            eta = elapsed / self._fraction - elapsed
            text = f"Elapsed {format_duration(elapsed)}   ETA {format_duration(eta)}"
        else:
            text = f"Elapsed {format_duration(elapsed)}"
        if self.timing.text != text:
            self.timing.set_text(text)
