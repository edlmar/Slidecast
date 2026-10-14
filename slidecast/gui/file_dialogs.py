"""Open-file / open-folder pickers.

In the native window these are the operating system's own dialogs (via pywebview). In a browser
tab the page cannot see local paths at all, so a small path prompt stands in - the server runs on
this same machine, so a typed path is exactly as good.
"""

from __future__ import annotations

import sys
from pathlib import Path

from nicegui import app, ui

from slidecast.core.logging_setup import get_logger
from slidecast.gui.ui_kit import button

log = get_logger("gui")

PPTX = ("PowerPoint (*.pptx)",)
AUDIO = ("Audio (*.wav;*.flac;*.ogg;*.mp3)", "All files (*.*)")
PROGRAMS = ("Programs (*.exe)", "All files (*.*)") if sys.platform == "win32" else ()


def _native_window() -> object | None:
    native = getattr(app, "native", None)
    return getattr(native, "main_window", None)


def _dialog_kind(folder: bool) -> object:
    import webview

    kinds = getattr(webview, "FileDialog", None)
    if kinds is not None:  # pywebview >= 5
        return kinds.FOLDER if folder else kinds.OPEN
    return webview.FOLDER_DIALOG if folder else webview.OPEN_DIALOG


async def _native(folder: bool, start: str, file_types: tuple[str, ...]) -> Path | bool | None:
    """Returns the pick, None when cancelled, or False when no native dialog is available."""
    window = _native_window()
    if window is None:
        return False
    try:
        result = await window.create_file_dialog(  # type: ignore[attr-defined]
            _dialog_kind(folder),
            directory=start if start and Path(start).is_dir() else "",
            allow_multiple=False,
            file_types=file_types,
        )
    except Exception as exc:
        log.warning("Native file dialog failed, falling back to a path prompt: %s", exc)
        return False
    if not result:
        return None
    first = result[0] if isinstance(result, list | tuple) else result
    return Path(str(first))


async def _prompt(title: str, start: str, folder: bool, suffixes: tuple[str, ...]) -> Path | None:
    with ui.dialog() as dialog, ui.card().classes("sc-card gap-3").style("min-width: 560px"):
        ui.label(title).classes("sc-panel-title")
        path_in = ui.input("Folder" if folder else "File", value=start).classes("w-full")
        path_in.props("outlined dense autofocus clearable")
        error = ui.label("").classes("sc-error")

        def accept() -> None:
            text = (path_in.value or "").strip().strip('"')
            p = Path(text).expanduser() if text else None
            if p is None:
                error.set_text("Enter a path.")
            elif folder and not p.is_dir():
                error.set_text("That folder does not exist.")
            elif not folder and not p.is_file():
                error.set_text("That file does not exist.")
            elif suffixes and p.suffix.lower() not in suffixes:
                error.set_text(f"Expected {' / '.join(suffixes)}.")
            else:
                dialog.submit(p)

        path_in.on("keydown.enter", accept)
        with ui.row().classes("w-full justify-end gap-2"):
            button("Cancel", lambda: dialog.submit(None), kind="quiet")
            button("Open", accept, kind="primary")
    result = await dialog
    dialog.delete()
    return result if isinstance(result, Path) else None


async def pick_file(
    title: str, start: str = "", file_types: tuple[str, ...] = (), suffixes: tuple[str, ...] = ()
) -> Path | None:
    picked = await _native(False, start, file_types)
    if picked is not False:
        return picked  # type: ignore[return-value]
    return await _prompt(title, start, False, suffixes)


async def pick_folder(title: str, start: str = "") -> Path | None:
    picked = await _native(True, start, ())
    if picked is not False:
        return picked  # type: ignore[return-value]
    return await _prompt(title, start, True, ())
