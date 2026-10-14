"""Small layout helpers shared by the views: cards, form rows, buttons, tooltips."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from nicegui import ui

_ROLES = ("sc-muted", "sc-ok", "sc-warn", "sc-error", "sc-mono")

_KINDS = {
    "primary": "unelevated no-caps color=primary",
    "default": "outline no-caps color=grey-5",
    "danger": "outline no-caps color=negative",
    "quiet": "flat no-caps color=grey-5",
    "quiet-danger": "flat no-caps color=negative",
}


@contextmanager
def card(title: str, classes: str = "") -> Iterator[ui.column]:
    with ui.column().classes(f"sc-card w-full gap-2 {classes}") as col:
        ui.label(title).classes("sc-section")
        yield col


@contextmanager
def field(label: str, width: int = 210) -> Iterator[ui.row]:
    """A labelled form row: fixed-width label on the left, the control fills the rest."""
    with ui.row().classes("w-full items-center no-wrap gap-4") as row:
        ui.label(label).style(f"width:{width}px;min-width:{width}px")
        yield row


def header(title: str, subtitle: str | None = None) -> ui.label | None:
    ui.label(title).classes("sc-title")
    if subtitle is None:
        return None
    return ui.label(subtitle).classes("sc-subtitle")


def button(
    text: str,
    on_click: Callable[..., Any] | None = None,
    kind: str = "default",
    small: bool = False,
    icon: str | None = None,
) -> ui.button:
    b = ui.button(text, on_click=on_click, icon=icon).props(_KINDS[kind])
    if small:
        b.props("dense").classes("px-3")
    return b


def tip(element: ui.element, text: str) -> None:
    """Native hover tooltip; cheap to update, unlike a ui.tooltip child."""
    if element._props.get("title") != text:
        element._props["title"] = text
        element.update()


def role(label: ui.label, name: str | None) -> None:
    """Swap a label's text role (muted / ok / warn / error / mono)."""
    label.classes(remove=" ".join(_ROLES), add=f"sc-{name}" if name else None)


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


async def confirm(title: str, text: str, ok: str = "Yes", danger: bool = False) -> bool:
    with ui.dialog() as dialog, ui.card().classes("sc-card gap-4").style("min-width: 420px"):
        ui.label(title).classes("sc-panel-title")
        ui.label(text).style("white-space: pre-line")
        with ui.row().classes("w-full justify-end gap-2"):
            button("No", lambda: dialog.submit(False), kind="quiet")
            button(ok, lambda: dialog.submit(True), kind="danger" if danger else "primary")
    result = await dialog
    dialog.delete()
    return bool(result)


async def alert(title: str, text: str, error: bool = False) -> None:
    with ui.dialog() as dialog, ui.card().classes("sc-card gap-4").style("min-width: 420px"):
        with ui.row().classes("items-center gap-2 no-wrap"):
            ui.icon("error_outline" if error else "info_outline").classes(
                "text-negative" if error else "text-primary"
            ).style("font-size: 22px")
            ui.label(title).classes("sc-panel-title")
        ui.label(text).style("white-space: pre-line")
        with ui.row().classes("w-full justify-end"):
            button("OK", lambda: dialog.submit(None), kind="primary")
    await dialog
    dialog.delete()
