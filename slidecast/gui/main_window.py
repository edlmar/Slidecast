"""Main window: menu bar + sidebar + step views + collapsible log + status bar."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from nicegui import app, ui
from nicegui.events import KeyEventArguments

from slidecast import __version__
from slidecast.core import pipeline as P
from slidecast.core.logging_setup import get_logger
from slidecast.core.project import Project, SlideStatus
from slidecast.gui import file_dialogs as D
from slidecast.gui.app import DEFAULT_SIZE
from slidecast.gui.context import AppContext
from slidecast.gui.theme import apply_theme
from slidecast.gui.ui_kit import alert, confirm, plural
from slidecast.gui.views.export_view import ExportView
from slidecast.gui.views.rebuild_view import RebuildView
from slidecast.gui.views.source_view import SourceView
from slidecast.gui.views.synthesis_view import SynthesisView
from slidecast.gui.views.voice_view import VoiceView
from slidecast.gui.widgets.log_console import LogConsole
from slidecast.gui.widgets.player import Player
from slidecast.gui.widgets.sidebar import Sidebar
from slidecast.gui.widgets.status_bar import StatusBar

MenuItem = tuple[str, str, Callable[[], object]] | None  # (label, shortcut, action) | separator

# Height the status bar gains while a task runs (the progress row), so a run never introduces
# scrolling the idle window did not have.
PROGRESS_RESERVE = 40
# Title bar + frame when the webview cannot report it (outerHeight is 0 in some backends).
DEFAULT_CHROME = 40

# Natural height of the tallest step, measured with every step shown in turn and collapsed
# sections (Voice > Advanced) forced open. Content is fluid in width, so only height counts.
_MEASURE_JS = """
(() => {
  const views = Array.from(document.querySelectorAll('.sc-view'));
  const shown = views.map((v) => !v.classList.contains('hidden'));
  document.body.classList.add('sc-measure');
  let tallest = 0;
  for (const v of views) {
    views.forEach((o) => o.classList.toggle('hidden', o !== v));
    tallest = Math.max(tallest, v.offsetHeight);
  }
  views.forEach((o, i) => o.classList.toggle('hidden', !shown[i]));
  document.body.classList.remove('sc-measure');
  const content = getComputedStyle(document.querySelector('.nicegui-content'));
  const px = (s) => parseFloat(s) || 0;
  return {
    need: tallest + px(content.paddingTop) + px(content.paddingBottom)
      + (document.querySelector('.q-header')?.offsetHeight || 0)
      + (document.querySelector('.q-footer')?.offsetHeight || 0),
    chrome: Math.max(window.outerHeight - window.innerHeight, 0),
    availW: screen.availWidth, availH: screen.availHeight,
    left: screen.availLeft || 0, top: screen.availTop || 0,
  };
})()
"""


log = get_logger("gui")


class MainWindow:
    def __init__(self, ctx: AppContext, icon_url: str | None) -> None:
        self.ctx = ctx
        apply_theme()
        ui.page_title(self._title(ctx.project))
        self.player = Player()
        ctx.player_stop.connect(self.player.stop)

        with ui.header().classes("sc-menubar items-center gap-0"):
            self._menu_bar()
        with (
            ui.left_drawer(value=True, fixed=True, bordered=True)
            .props("width=232 behavior=desktop")
            .classes("sc-sidebar p-0")
        ):
            self.sidebar = Sidebar(self.show_step, icon_url)
        with ui.footer().classes("sc-footer p-0"), ui.column().classes("w-full gap-0"):
            self.log = LogConsole(ctx.logs)
            self.status = StatusBar(
                on_cancel=self.cancel,
                on_pause=self.ctx.set_paused,
                on_log=self.log.set_visible,
            )

        self.source_view = SourceView(ctx, self.player, on_make_video=self.run_all)
        self.deck_view = self.source_view.deck_view
        self.batch_view = self.source_view.batch_view
        self.voice_view = VoiceView(ctx, self.player)
        self.synth_view = SynthesisView(ctx, on_open_deck=self.source_view.browse_deck)
        self.export_view = ExportView(ctx)
        self.rebuild_view = RebuildView(
            ctx, on_rebuild_all=self.run_all, on_batch_run=self.batch_view.run
        )
        self.views: dict[str, ui.element] = {
            "deck": self.source_view.root,
            "voice": self.voice_view.root,
            "synth": self.synth_view.root,
            "export": self.export_view.root,
            "rebuild": self.rebuild_view.root,
        }
        self.step = "deck"
        for view in self.views.values():
            view.classes("sc-view")
        self._client = ui.context.client
        if app.native.main_window is not None and not ctx.window_fitted:
            ui.timer(0.5, self._fit_window, once=True)

        ui.keyboard(on_key=self._on_key, ignore=["input", "select", "textarea"])

        ctx.project_changed.connect(self.on_project_changed)
        ctx.slides_changed.connect(self.update_hints)
        ctx.slide_changed.connect(lambda _i: self.update_hints())
        ctx.renders_changed.connect(self.update_hints)
        ctx.batch_changed.connect(self.update_hints)
        ctx.task_started.connect(
            lambda title: self.status.start(title, background=title == "Checking encoders")
        )
        ctx.task_progress.connect(self.on_progress)
        ctx.task_finished.connect(self.on_finished)
        ctx.message.connect(self.on_message)
        ctx.paused_changed.connect(self.status.set_paused_ui)

        self.show_step("deck")
        self.update_hints()
        # A reload mid-run lands on a page that has to catch up with the task in flight.
        if ctx.busy:
            self.status.start(ctx.task_title, background=ctx.task_title == "Checking encoders")
            if ctx.last_progress is not None:
                self.on_progress(ctx.last_progress)
            self.status.set_paused_ui(ctx.paused)

    # -- menu ---------------------------------------------------------------------------------

    def _menu_bar(self) -> None:
        menus: dict[str, list[MenuItem]] = {
            "File": [
                ("Open deck...", "Ctrl+O", self.source_view_browse_deck),
                ("Open folder of decks...", "Ctrl+Shift+O", self.source_view_browse_folder),
                None,
                ("Set LibreOffice path...", "", self.pick_soffice),
                ("Set ffmpeg path...", "", self.pick_ffmpeg),
                None,
                ("Quit", "Ctrl+Q", self.quit),
            ],
            "Run": [
                ("Render slides", "", self.ctx.start_render),
                ("Synthesize pending", "Ctrl+R", lambda: self.ctx.start_synthesis()),
                ("Export video", "Ctrl+E", self.export),
                ("Run everything", "F5", self.run_all),
                ("Cancel task", "Esc", self.cancel),
                ("Jump to next failure", "Ctrl+J", self.jump_to_next_failure),
                None,
                ("Pause / resume", "Ctrl+P", self.toggle_pause),
            ],
            "Help": [
                ("Toggle log", "Ctrl+L", self.toggle_log),
                ("About", "", self.about),
            ],
        }
        for title, items in menus.items():
            with (
                ui.button(title).props("flat no-caps dense color=grey-4").classes("px-3"),
                ui.menu().props("auto-close"),
                ui.list().props("dense").style("min-width: 240px"),
            ):
                for item in items:
                    if item is None:
                        ui.separator()
                        continue
                    label, shortcut, action = item
                    with ui.item(on_click=action):
                        with ui.item_section():
                            ui.item_label(label)
                        if shortcut:
                            with ui.item_section().props("side"):
                                ui.item_label(shortcut).props("caption")

    # The menu is built before the views and the status bar exist, so its entries are methods
    # that look them up when clicked.
    def source_view_browse_deck(self) -> object:
        return self.source_view.browse_deck()

    def source_view_browse_folder(self) -> object:
        return self.source_view.browse_folder()

    def jump_to_next_failure(self) -> None:
        self.show_step("deck")
        self.deck_view.jump_to_next_failure()

    def export(self) -> None:
        self.ctx.start_export(self.export_view.on_exported)

    def toggle_log(self) -> None:
        self.status.toggle_log()

    # -- window size --------------------------------------------------------------------------

    async def _fit_window(self) -> None:
        """Grow the native window until no step needs scrolling (clamped to the screen).

        Runs once per app start: a reload keeps whatever size the user has chosen since.
        """
        window = app.native.main_window
        if window is None or self.ctx.window_fitted:
            return
        self.ctx.window_fitted = True
        try:
            await self._client.connected()
            m = await ui.run_javascript(_MEASURE_JS, timeout=5)
        except Exception as exc:  # measuring is a nicety; the default size still works
            log.debug("Window fit skipped: %s", exc)
            return
        chrome = int(m["chrome"]) or DEFAULT_CHROME
        height = int(m["need"]) + PROGRESS_RESERVE + chrome
        avail_w, avail_h = int(m["availW"]), int(m["availH"])
        if height > avail_h:
            window.maximize()  # the screen is too small for everything: use all of it
            return
        width = min(DEFAULT_SIZE[0], avail_w)
        window.resize(width, height)
        x = int(m["left"]) + (avail_w - width) // 2
        window.move(x, int(m["top"]) + (avail_h - height) // 2)

    # -- keyboard -----------------------------------------------------------------------------

    def _on_key(self, e: KeyEventArguments) -> object:
        if not e.action.keydown or e.action.repeat:
            return None
        name = str(e.key.name)
        key = name.lower()
        ctrl, shift = e.modifiers.ctrl, e.modifiers.shift
        if ctrl and shift and key == "o":
            return self.source_view.browse_folder()
        if ctrl and not shift:
            actions: dict[str, Callable[[], object]] = {
                "o": self.source_view.browse_deck,
                "r": lambda: self.ctx.start_synthesis(),
                "e": self.export,
                "j": self.jump_to_next_failure,
                "p": self.toggle_pause,
                "l": self.toggle_log,
                "q": self.quit,
            }
            action = actions.get(key)
            return action() if action is not None else None
        if ctrl or shift or e.modifiers.alt:
            return None
        if name == "F5":
            return self.run_all()
        if name == "Escape" and self.ctx.busy:
            self.cancel()
        elif self.step == "deck" and self.ctx.project is not None:
            # Keyboard-first row actions on the slide table.
            if name == "Enter":
                self.deck_view.play_selected()
            elif key == "r":
                self.deck_view.regenerate_selected()
        return None

    # -- navigation ---------------------------------------------------------------------------

    def show_step(self, key: str) -> None:
        self.step = key
        for k, view in self.views.items():
            view.set_visibility(k == key)
        self.sidebar.select(key)

    @staticmethod
    def _title(project: Project | None) -> str:
        return "slidecast" if project is None else f"{project.deck_path.name} - slidecast"

    def on_project_changed(self, project: Project | None) -> None:
        # Every step stays reachable: with no deck open, Voice/Synthesis/Export edit the
        # settings a batch run will use, and Rebuild works across a scanned folder.
        self.show_step("deck")
        ui.page_title(self._title(project))
        self.update_hints()

    def update_hints(self) -> None:
        project = self.ctx.project
        if project is None:
            ref = self.ctx.settings().tts.reference_clip
            self.sidebar.set_hint("voice", Path(ref).name if ref else "no clip")
            for k in ("synth", "export"):
                self.sidebar.set_hint(k, "batch settings")
            self.sidebar.set_hint("deck", self.source_view.hint())
            self.sidebar.set_hint("rebuild", self.rebuild_view.hint())
            return
        inc = project.state.included_slides()
        rendered = P.cached_render(project) is not None
        self.sidebar.set_hint(
            "deck", plural(len(inc), "slide") + (", rendered" if rendered else "")
        )
        if self.source_view.mode != 0:  # folder panel showing: describe the folder instead
            self.sidebar.set_hint("deck", self.source_view.hint())
        ref = project.settings.tts.reference_clip
        self.sidebar.set_hint("voice", Path(ref).name if ref else "no clip")
        done = sum(
            1 for s in inc if s.has_narration and s.status in (SlideStatus.DONE, SlideStatus.CACHED)
        )
        need = sum(1 for s in inc if s.has_narration)
        self.sidebar.set_hint("synth", f"{done}/{need} narrated")
        self.sidebar.set_hint(
            "export",
            Path(project.state.last_output).name if project.state.last_output else "not exported",
        )
        self.sidebar.set_hint("rebuild", self.rebuild_view.hint())

    # -- tasks --------------------------------------------------------------------------------

    def on_progress(self, p: P.Progress) -> None:
        self.status.update_progress(p.fraction, p.message)

    async def on_finished(self, message: str, ok: bool) -> None:
        self.status.finish(message, ok)
        self.update_hints()
        if not ok and "cancelled" not in message:
            await alert("slidecast", message, error=True)

    def on_message(self, level: str, text: str) -> None:
        self.status.set_message(text)
        if level == "warn":
            ui.notify(text, type="warning")

    def cancel(self) -> None:
        if self.ctx.busy:
            self.status.cancelling()
            self.ctx.cancel_task()

    def toggle_pause(self) -> None:
        if self.ctx.busy:
            self.status.toggle_pause()

    async def run_all(self) -> None:
        """One click: slides -> narration -> encoded video."""
        project = self.ctx.project
        if project is None:
            return
        if not project.settings.tts.reference_clip:
            self.show_step("voice")
            await alert(
                "Pick a voice first",
                "Choose the reference clip of your voice in the Voice step, then press "
                "Make video again.",
            )
            return
        self.ctx.start_run_all(on_success=self.export_view.on_exported)

    # -- settings ----------------------------------------------------------------------------

    async def pick_soffice(self) -> None:
        path = await D.pick_file("LibreOffice soffice", file_types=D.PROGRAMS)
        if path:
            self.ctx.settings().render.soffice_path = path
            self.ctx.settings_edited()

    async def pick_ffmpeg(self) -> None:
        path = await D.pick_file("ffmpeg binary", file_types=D.PROGRAMS)
        if path:
            self.ctx.settings().export.ffmpeg_path = path
            self.ctx.settings_edited()
            self.ctx.probe_encoders()

    async def about(self) -> None:
        await alert(
            "About slidecast",
            f"slidecast {__version__}\n\nPPTX to narrated video with Chatterbox-Turbo, "
            "LibreOffice, PyMuPDF and ffmpeg. Fully local.\n\n"
            "Generated audio carries Resemble AI's Perth neural watermark.",
        )

    async def quit(self) -> None:
        if self.ctx.busy and not await confirm(
            "Task running", "A task is still running. Cancel it and quit?", ok="Quit", danger=True
        ):
            return
        self.player.stop()
        app.shutdown()
