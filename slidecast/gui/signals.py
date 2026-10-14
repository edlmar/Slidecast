"""A tiny thread-safe signal: the GUI's stand-in for Qt signals.

Handlers always run on NiceGUI's event loop, whichever thread emits, so a worker thread can report
progress without touching UI elements itself. A handler connected while a page is being built is
tied to that page's client and runs inside it (so ``ui.notify`` and dialogs land on the right
page); once that client is gone - a reload builds a fresh page - the handler is dropped.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Awaitable, Callable
from typing import Any

from nicegui import Client, background_tasks, ui

from slidecast.core.logging_setup import get_logger

log = get_logger("gui")

_loop: asyncio.AbstractEventLoop | None = None


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Remember NiceGUI's event loop (called once from ``app.on_startup``)."""
    global _loop
    _loop = loop


def _on_loop_thread() -> bool:
    try:
        return asyncio.get_running_loop() is _loop
    except RuntimeError:
        return False


def call_soon(fn: Callable[..., Any], *args: Any) -> None:
    """Run ``fn(*args)`` on the event loop: now if already there, else as soon as it is free."""
    loop = _loop
    if loop is None or loop.is_closed() or _on_loop_thread():
        _safe(fn, args)
    else:
        loop.call_soon_threadsafe(_safe, fn, args)


def _safe(fn: Callable[..., Any], args: tuple[Any, ...]) -> None:
    try:
        fn(*args)
    except Exception:
        log.exception("GUI callback %r failed", fn)


def current_client() -> Client | None:
    try:
        return ui.context.client
    except Exception:  # no slot on the stack: not inside a page
        return None


class Signal:
    def __init__(self) -> None:
        self._slots: list[tuple[Callable[..., Any], Client | None]] = []
        self._lock = threading.Lock()

    def connect(self, fn: Callable[..., Any]) -> None:
        with self._lock:
            self._slots.append((fn, current_client()))

    def emit(self, *args: Any) -> None:
        call_soon(self._dispatch, *args)

    def _dispatch(self, *args: Any) -> None:
        with self._lock:
            self._slots = [
                (fn, client)
                for fn, client in self._slots
                if client is None or client.id in Client.instances
            ]
            slots = list(self._slots)
        for fn, client in slots:
            try:
                if client is None:
                    result = fn(*args)
                else:
                    with client:
                        result = fn(*args)
                if inspect.isawaitable(result):  # async handler: may open a dialog and wait
                    background_tasks.create(_finish(result, client), name=f"signal {fn!r}")
            except Exception:
                log.exception("GUI handler %r failed", fn)


async def _finish(result: Awaitable[Any], client: Client | None) -> None:
    try:
        if client is None:
            await result
        else:
            with client:
                await result
    except Exception:
        log.exception("GUI handler failed")
