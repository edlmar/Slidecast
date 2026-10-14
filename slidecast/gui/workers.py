"""Background workers: run a core function with progress + cancel without blocking the UI."""

from __future__ import annotations

import logging
import threading
import traceback
from collections import deque
from collections.abc import Callable
from typing import Any

from slidecast.core.errors import Cancelled, SlidecastError
from slidecast.core.logging_setup import CallbackHandler, get_logger
from slidecast.core.pipeline import CancelToken, Progress
from slidecast.gui.signals import Signal

log = get_logger("gui")

WorkFn = Callable[[Callable[[Progress], None], CancelToken], Any]

MAX_LOG_LINES = 5000


class Worker(threading.Thread):
    """Runs ``fn(progress_cb, cancel_token)`` on a background thread.

    The callbacks are called on the worker thread; the context marshals them onto the UI loop.
    """

    def __init__(
        self,
        name: str,
        fn: WorkFn,
        on_progress: Callable[[Progress], None],
        on_success: Callable[[Any], None],
        on_failed: Callable[[str], None],
        on_cancelled: Callable[[], None],
    ) -> None:
        # Daemon: a thread stuck in native code must not keep the process alive after the
        # window closes. Subprocesses are covered separately (proc.py's kill-on-close job).
        super().__init__(name=f"slidecast: {name}", daemon=True)
        self.title = name
        self._fn = fn
        self._on_progress = on_progress
        self._on_success = on_success
        self._on_failed = on_failed
        self._on_cancelled = on_cancelled
        self.token = CancelToken()
        self.result: Any = None

    def cancel(self) -> None:
        self.token.cancel()

    def set_paused(self, paused: bool) -> None:
        self.token.set_paused(paused)

    @property
    def is_paused(self) -> bool:
        return self.token.is_paused

    def run(self) -> None:
        try:
            self.result = self._fn(self._on_progress, self.token)
        except Cancelled:
            log.info("%s cancelled", self.title)
            self._on_cancelled()
        except SlidecastError as exc:
            log.error("%s failed: %s", self.title, exc)
            self._on_failed(str(exc))
        except Exception as exc:
            log.error("%s crashed: %s\n%s", self.title, exc, traceback.format_exc())
            self._on_failed(f"Unexpected error: {exc}")
        else:
            self._on_success(self.result)


class LogBridge:
    """Keeps the recent log in memory (so a reloaded page shows it) and announces new lines."""

    def __init__(self) -> None:
        self.lines: deque[tuple[str, int]] = deque(maxlen=MAX_LOG_LINES)
        self.message = Signal()  # (text, level), delivered on the UI loop
        self.handler = CallbackHandler(self._emit)
        self.handler.setLevel(logging.DEBUG)
        self._local = threading.local()

    def _emit(self, text: str, level: int) -> None:
        self.lines.append((text, level))
        # A console handler that fails logs the failure, which lands back here: record it, but
        # do not announce it, or a broken handler would recurse forever.
        if getattr(self._local, "emitting", False):
            return
        self._local.emitting = True
        try:
            self.message.emit(text, level)
        finally:
            self._local.emitting = False

    def attach(self) -> None:
        get_logger().addHandler(self.handler)

    def detach(self) -> None:
        get_logger().removeHandler(self.handler)
