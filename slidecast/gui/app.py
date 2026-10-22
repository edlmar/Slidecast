"""GUI entry point: ``python -m slidecast.gui.app [--project DIR | --deck FILE] [--fake-tts]``.

The interface is a NiceGUI page (Vue + Quasar) served locally. By default it opens in its own
native window (pywebview); ``--browser`` serves it to the default browser instead.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import multiprocessing
import sys
from pathlib import Path

from slidecast.core.logging_setup import configure_logging, get_logger

log = get_logger("gui")

MIN_SIZE = (1180, 640)
# Starting size; the main window then grows it to fit the tallest step.
DEFAULT_SIZE = (1480, 900)


def _windows_app_id() -> None:
    """Explicit AppUserModelID so the taskbar shows our icon, not the generic python.exe one."""
    if sys.platform != "win32":
        return
    with contextlib.suppress(Exception):
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("slidecast.app")


# Module level on purpose: the native window lives in a child process that re-imports this
# module but never runs main(), and it is that process whose taskbar button needs the id.
_windows_app_id()


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()  # the native window is a multiprocessing child
    parser = argparse.ArgumentParser(prog="slidecast-gui")
    parser.add_argument("--project", type=Path, help="Project folder to open")
    parser.add_argument("--deck", type=Path, help=".pptx to open (creates/opens its project)")
    parser.add_argument("--fake-tts", action="store_true", help="Use the fake TTS backend")
    parser.add_argument(
        "--browser", action="store_true", help="Open in the web browser instead of a window"
    )
    parser.add_argument("--port", type=int, help="Port to serve on (default: any free port)")
    args = parser.parse_args(argv)

    configure_logging(None, console=True)

    from nicegui import app, ui

    from slidecast.gui import media, signals
    from slidecast.gui.context import AppContext
    from slidecast.gui.main_window import MainWindow

    ctx = AppContext(fake_tts=args.fake_tts)
    ctx.logs.attach()
    media.register_routes()

    # The .ico carries a frame per size for the window and taskbar; the page uses the SVG.
    window_icon = Path(__file__).with_name("slidecast.ico")
    icon = Path(__file__).with_name("slidecast.svg")
    app.add_static_file(local_file=icon, url_path="/slidecast/icon.svg")
    # Versioned by mtime: the webview caches the old mark long after the file changes.
    icon_url = f"/slidecast/icon.svg?v={icon.stat().st_mtime_ns}"

    def startup() -> None:
        signals.bind_loop(asyncio.get_running_loop())
        try:
            if args.project:
                ctx.open_project(args.project)
            elif args.deck:
                ctx.open_deck(args.deck)
        except Exception as exc:
            log.error("Could not open %s: %s", args.project or args.deck, exc)
        ctx.probe_encoders()

    def shutdown() -> None:
        ctx.wait_for_task()
        ctx.logs.detach()

    app.on_startup(startup)
    app.on_shutdown(shutdown)

    @ui.page("/")
    def index() -> None:
        MainWindow(ctx, icon_url)

    native = not args.browser
    if native:
        from slidecast.gui.window_icon import apply_window_icon

        app.native.window_args["min_size"] = MIN_SIZE
        # Set in the window process, sized to its DPI (see window_icon.py).
        app.native.start_args["func"] = apply_window_icon
        app.native.start_args["args"] = (str(window_icon),)
    ui.run(
        title="Slidecast",
        native=native,
        window_size=DEFAULT_SIZE if native else None,
        port=args.port,
        reload=False,
        dark=True,
        # Not in a native window: NiceGUI would then set the window icon itself, from one
        # stretched frame, over the per-DPI one apply_window_icon sets. The SVG is for a tab.
        favicon=None if native else icon,
        show_welcome_message=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
