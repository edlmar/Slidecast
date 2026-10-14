"""Collapsible log console with copy/clear."""

from __future__ import annotations

import logging

from nicegui import ui

from slidecast.gui.ui_kit import button
from slidecast.gui.workers import MAX_LOG_LINES, LogBridge


def _decorate(line: str, level: int) -> str:
    if level >= logging.ERROR:
        return f"!! {line}"
    if level >= logging.WARNING:
        return f" ! {line}"
    return line


class LogConsole:
    def __init__(self, bridge: LogBridge) -> None:
        self._bridge = bridge
        with ui.column().classes("w-full gap-0") as self.root:
            with ui.row().classes("w-full items-center px-4 py-1"):
                ui.label("Log").classes("sc-section")
                ui.space()
                button("Copy", self.copy_all, kind="quiet", small=True)
                button("Clear", self.clear, kind="quiet", small=True)
            self.log = ui.log(max_lines=MAX_LOG_LINES).classes("sc-log w-full")
        for line, level in list(bridge.lines):
            self.log.push(_decorate(line, level))
        bridge.message.connect(self.append)
        self.root.set_visibility(False)

    def append(self, line: str, level: int) -> None:
        self.log.push(_decorate(line, level))

    def clear(self) -> None:
        self._bridge.lines.clear()
        self.log.clear()

    def copy_all(self) -> None:
        ui.clipboard.write("\n".join(_decorate(t, lvl) for t, lvl in self._bridge.lines))
        ui.notify("Log copied")

    def set_visible(self, visible: bool) -> None:
        self.root.set_visibility(visible)
