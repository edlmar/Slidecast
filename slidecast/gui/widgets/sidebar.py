"""Left sidebar: the four pipeline steps (Decks covers one deck or a folder) plus Rebuild."""

from __future__ import annotations

from collections.abc import Callable

from nicegui import ui

from slidecast.gui.ui_kit import tip

STEPS = [
    ("deck", "Decks"),
    ("voice", "Voice"),
    ("synth", "Synthesis"),
    ("export", "Export"),
    ("rebuild", "Rebuild"),
]

_HINTS = {
    "no deck": "No presentation selected",
    "no clip": "Add a reference voice",
    "batch settings": "Shared settings",
    "not exported": "No video exported",
}


class Sidebar:
    def __init__(self, on_select: Callable[[str], None], icon_url: str | None) -> None:
        self._rows: dict[str, ui.row] = {}
        self._hints: dict[str, ui.label] = {}
        with ui.column().classes("w-full gap-2 px-3 pt-5 pb-4"):
            with ui.column().classes("gap-1 px-2 pb-2"):
                with ui.row().classes("items-center gap-2 no-wrap"):
                    if icon_url:
                        ui.image(icon_url).style("width: 30px; height: 30px")
                    ui.label("slidecast").classes("sc-brand")
                ui.label("Presentation to video").classes("sc-muted")
            ui.label("Workflow").classes("sc-section px-2 pt-2 pb-1")
            for i, (key, title) in enumerate(STEPS):
                with (
                    ui.row()
                    .classes("sc-step w-full items-center gap-3")
                    .on("click", lambda _e, k=key: on_select(k)) as row
                ):
                    ui.label(str(i + 1)).classes("sc-step-num")
                    with ui.column().classes("gap-0 min-w-0 flex-1"):
                        ui.label(title).classes("sc-step-title")
                        self._hints[key] = ui.label("").classes("sc-step-hint sc-ellipsis w-full")
                self._rows[key] = row
        self.select("deck")

    def select(self, key: str) -> None:
        for k, row in self._rows.items():
            row.classes(add="active" if k == key else None, remove=None if k == key else "active")

    def set_hint(self, key: str, text: str) -> None:
        display = _HINTS.get(text, text)
        label = self._hints[key]
        if label.text != display:
            label.set_text(display)
        tip(self._rows[key], display)
