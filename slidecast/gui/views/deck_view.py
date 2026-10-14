"""Deck step: drop a .pptx, then a table of slides with notes editing and row actions."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from nicegui import ui
from nicegui.events import GenericEventArguments

from slidecast.core import pipeline as P
from slidecast.core.errors import SlidecastError
from slidecast.core.project import Project, SlideStatus
from slidecast.core.timeline import format_duration
from slidecast.gui import file_dialogs as D
from slidecast.gui.context import AppContext
from slidecast.gui.ui_kit import alert, button, tip
from slidecast.gui.widgets.player import Player
from slidecast.gui.widgets.slide_table import SlideTable, ThumbnailLoader, thumb_url

# Files dropped anywhere on the window. A webview hands the real path over (pywebview sets
# `pywebviewFullPath`); a plain browser never reveals it, so Python gets only the name.
_DROP_SCRIPT = """
<script>
(() => {
  if (window.scDropInstalled) return;
  window.scDropInstalled = true;
  const zone = () => document.querySelector('.sc-dropzone');
  const hasFiles = (e) =>
    e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
  window.addEventListener('dragover', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    zone()?.classList.add('active');
  });
  window.addEventListener('dragleave', (e) => {
    if (e.relatedTarget === null) zone()?.classList.remove('active');
  });
  window.addEventListener('drop', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    zone()?.classList.remove('active');
    const file = Array.from(e.dataTransfer.files || [])
      .find((f) => f.name.toLowerCase().endsWith('.pptx'));
    if (!file) return;
    const path = file.pywebviewFullPath || file.path || '';
    emitEvent('sc_drop', {name: file.name, path});
  });
})();
</script>
"""


class DeckView:
    def __init__(
        self,
        ctx: AppContext,
        player: Player,
        on_batch_folder: Callable[[Path], None],
        on_make_video: Callable[[], object],
    ) -> None:
        self.ctx = ctx
        self.player = player
        self._on_batch_folder = on_batch_folder
        self._loader: ThumbnailLoader | None = None
        self.summary_text = ""

        with ui.column().classes("w-full gap-4") as self.root:
            # ---- empty state -------------------------------------------------------------
            with ui.column().classes("w-full gap-4") as self.empty:
                with ui.column().classes("sc-dropzone w-full items-center justify-center gap-2"):
                    ui.label("Drop a PowerPoint deck here").classes("sc-title")
                    ui.label(".pptx only. A project folder is created next to the deck.").classes(
                        "sc-muted"
                    )
                    button("Browse for deck", self.browse_deck, kind="primary").classes("mt-2")
                with ui.row().classes("w-full items-center"):
                    ui.label("Recent projects").classes("sc-section")
                    ui.space()
                    b = button("Open folder...", self.browse_project)
                    tip(
                        b,
                        "Pick a folder: slidecast finds every presentation in it and its "
                        "subfolders.",
                    )
                self.recent_box = ui.column().classes("w-full gap-1")

            # ---- project state -----------------------------------------------------------
            with ui.column().classes("w-full gap-3") as self.page:
                with ui.row().classes("w-full items-start no-wrap gap-2"):
                    with ui.column().classes("gap-0 flex-1 min-w-0"):
                        self.title = ui.label("").classes("sc-title sc-ellipsis w-full")
                        self.subtitle = ui.label("").classes("sc-subtitle sc-ellipsis w-full")
                    self.make_btn = button("Make video", on_make_video, kind="primary")
                    tip(
                        self.make_btn,
                        "Render slides, narrate every pending slide, and encode the final video.",
                    )
                    self.render_btn = button("Render slides", self.render_slides)
                    self.reload_btn = button("Reload deck", self.reload_deck)
                    self.close_btn = button("Close", self.ctx.close_project, kind="quiet")

                with ui.row().classes("w-full items-center gap-2"):
                    self.play_btn = button("Play audio", self.play_selected, small=True)
                    self.regen_btn = button(
                        "Regenerate slide", self.regenerate_selected, small=True
                    )
                    self.edit_btn = button("Edit notes", self.edit_selected, small=True)
                    self.include_btn = button("Exclude", self.toggle_include_selected, small=True)
                    self.reset_btn = button(
                        "Reset notes", self.reset_selected_notes, kind="quiet", small=True
                    )
                    ui.space()
                    self.summary = ui.label("").classes("sc-muted")

                # Designed error state: a missing tool becomes a panel with a fix, not a traceback.
                self.env_banner = ui.row().classes("sc-card w-full items-center no-wrap gap-3")
                with self.env_banner:
                    self.env_label = ui.label("").classes("sc-error flex-1")
                    self.env_soffice_btn = button(
                        "Choose soffice.exe...", lambda: self._pick_tool("soffice"), small=True
                    )
                    self.env_ffmpeg_btn = button(
                        "Choose ffmpeg...", lambda: self._pick_tool("ffmpeg"), small=True
                    )
                self.env_banner.set_visibility(False)

                self.table = SlideTable(
                    on_select=lambda _r: self.update_buttons(), on_action=self._row_action
                )

        ui.add_body_html(_DROP_SCRIPT)
        ui.on("sc_drop", self._on_drop)

        ctx.project_changed.connect(self.on_project_changed)
        ctx.slide_changed.connect(self.table.refresh_row)
        ctx.slides_changed.connect(self.on_slides_changed)
        ctx.renders_changed.connect(self.load_thumbnails)
        ctx.task_started.connect(lambda _t: self.update_buttons())
        ctx.task_finished.connect(lambda *_: self.update_buttons())
        player.changed.connect(self._sync_playing)
        self.on_project_changed(ctx.project)

    # -- project lifecycle ---------------------------------------------------------------

    def on_project_changed(self, project: Project | None) -> None:
        self.stop_loader()
        self.table.set_project(project)
        self.empty.set_visibility(project is None)
        self.page.set_visibility(project is not None)
        if project is None:
            self.summary_text = ""
            self.refresh_recent()
            return
        self.title.set_text(project.deck_path.name)
        self.on_slides_changed()  # refreshes the table and restarts the thumbnail loader

    def _check_env(self) -> None:
        """Surface missing external tools as a fixable panel before a run hits them."""
        from slidecast.core import ffmpeg as F
        from slidecast.core import render as R

        project = self.ctx.project
        problems: list[str] = []
        need_soffice = need_ffmpeg = False
        if project is not None:
            if P.resolve_slide_source(project) == "stills":
                try:
                    R.find_soffice(project.settings.render.soffice_path)
                except SlidecastError:
                    problems.append(
                        "LibreOffice was not found - slides cannot be rendered. Install it "
                        "(winget install TheDocumentFoundation.LibreOffice) or locate soffice."
                    )
                    need_soffice = True
            try:
                F.find_ffmpeg(project.settings.export.ffmpeg_path)
            except SlidecastError:
                problems.append(
                    "ffmpeg was not found - video export will fail. Install it "
                    "(winget install Gyan.FFmpeg) or locate the binary."
                )
                need_ffmpeg = True
        self.env_soffice_btn.set_visibility(need_soffice)
        self.env_ffmpeg_btn.set_visibility(need_ffmpeg)
        self.env_label.set_text("\n".join(problems))
        self.env_banner.set_visibility(bool(problems))

    async def _pick_tool(self, which: str) -> None:
        project = self.ctx.project
        if project is None:
            return
        if which == "soffice":
            path = await D.pick_file("LibreOffice soffice", file_types=D.PROGRAMS)
            if path:
                project.settings.render.soffice_path = path
        else:
            path = await D.pick_file("ffmpeg binary", file_types=D.PROGRAMS)
            if path:
                project.settings.export.ffmpeg_path = path
        if path:
            self.ctx.settings_edited()
            self._check_env()

    def on_slides_changed(self) -> None:
        project = self.ctx.project
        if project is None:
            return
        self.table.refresh()
        inc = project.state.included_slides()
        total = sum((s.duration or s.estimated_seconds) for s in inc)
        done = sum(1 for s in inc if s.status in (SlideStatus.DONE, SlideStatus.CACHED))
        errors = sum(1 for s in inc if s.status == SlideStatus.ERROR)
        n = len(project.state.slides)
        self.subtitle.set_text(
            f"{n} slide{'s' if n != 1 else ''}, {len(inc)} included, "
            f"~{format_duration(total)} narration   ·   {project.directory}"
        )
        text = f"{done}/{len(inc)} narrated"
        if errors:
            text += f", {errors} failed"
        self.summary_text = text
        self.summary.set_text(text)
        self.update_buttons()
        self._check_env()
        # Thumbnails on disk make a reload here cheap.
        self.load_thumbnails()

    def refresh_recent(self) -> None:
        self.recent_box.clear()
        recents = [p for p in self.ctx.store.recent_projects() if Project.exists(p)]
        with self.recent_box:
            if not recents:
                ui.label("No recent projects.").classes("sc-muted")
            for p in recents[:6]:
                with (
                    ui.row()
                    .classes("sc-step w-full items-center no-wrap gap-3")
                    .style("height: auto; padding: 8px 12px")
                    .on("click", lambda _e, path=p: self.open_project(path))
                ):
                    ui.icon("folder_open").classes("sc-muted")
                    ui.label(p.name).classes("sc-step-title")
                    ui.label(str(p)).classes("sc-muted sc-ellipsis flex-1")

    async def open_deck(self, deck: Path) -> None:
        try:
            self.ctx.open_deck(deck)
        except SlidecastError as exc:
            await alert("Could not open deck", str(exc), error=True)

    async def open_project(self, path: Path) -> None:
        try:
            self.ctx.open_project(path)
        except SlidecastError as exc:
            await alert("Could not open project", str(exc), error=True)

    async def browse_deck(self) -> None:
        start = self.ctx.store.last_dir("deck")
        path = await D.pick_file("Open deck", start, D.PPTX, suffixes=(".pptx",))
        if path:
            self.ctx.store.set_last_dir("deck", path)
            await self.open_deck(path)

    async def browse_project(self) -> None:
        """Pick a folder: open it directly if it is a project, else hand it to the batch panel."""
        folder = await D.pick_folder("Open folder", self.ctx.store.last_dir("project"))
        if folder is None:
            return
        self.ctx.store.set_last_dir("project", folder)
        if Project.exists(folder):
            await self.open_project(folder)
        else:
            self._on_batch_folder(folder)

    async def _on_drop(self, e: GenericEventArguments) -> None:
        args = e.args if isinstance(e.args, dict) else {}
        path = str(args.get("path") or "")
        if path and Path(path).is_file():
            await self.open_deck(Path(path))
        else:
            ui.notify(
                f"Cannot see where {args.get('name', 'the file')} lives from here - "
                "use Browse for deck instead.",
                type="warning",
            )

    async def reload_deck(self) -> None:
        project = self.ctx.project
        if project is None or self.ctx.busy:
            return
        try:
            changed = project.refresh_deck()
        except SlidecastError as exc:
            await alert("Reload failed", str(exc), error=True)
            return
        self.on_project_changed(project)
        self.ctx.message.emit(
            "info", "Deck reloaded: changes found" if changed else "Deck unchanged"
        )

    # -- rendering / thumbnails ------------------------------------------------------------

    def render_slides(self) -> None:
        self.ctx.start_render()

    def stop_loader(self) -> None:
        # Clearing self._loader first matters: a queued thumbnail from the old loader can still
        # arrive after this, and the guard in load_thumbnails must see it as stale.
        loader, self._loader = self._loader, None
        if loader is not None:
            loader.stop()

    def load_thumbnails(self) -> None:
        self.stop_loader()
        project = self.ctx.project
        if project is None:
            return
        render = P.cached_render(project)
        self.render_btn.set_text("Re-render slides" if render else "Render slides")
        sources = P.thumbnail_sources(project)
        preview_fn: Callable[[Callable[[], bool]], list[Path]] | None = None
        if not sources:
            # Nothing rendered yet: preview the raw deck at low res so the table is not blank.
            from slidecast.core import render as R

            try:
                soffice = R.find_soffice(project.settings.render.soffice_path)
            except SlidecastError:
                return  # no LibreOffice: skip previews silently, the env banner already nags

            def preview_fn(is_cancelled: Callable[[], bool]) -> list[Path]:
                return R.preview_thumbnails(
                    project.deck_path,
                    P.preview_sources(project),
                    soffice,
                    len(project.state.slides),
                    is_cancelled,
                )

        ffmpeg = None
        if any(s.clip is not None for s in sources):
            from slidecast.core import ffmpeg as F

            try:
                ffmpeg = F.find_ffmpeg(project.settings.export.ffmpeg_path)
            except SlidecastError:
                return  # no ffmpeg: skip thumbnails rather than nagging

        def loaded(row: int, path: Path) -> None:
            # Ignore thumbnails from a loader that has been superseded: its row numbers refer
            # to the slide list as it was when it started, which a reload can have reordered.
            if self._loader is loader:
                self.table.set_thumbnail(row, thumb_url(path))

        loader = ThumbnailLoader(sources, loaded, ffmpeg, preview_fn)
        self._loader = loader
        loader.start()

    # -- keyboard (bound by the main window while this step is showing) --------------------

    def jump_to_next_failure(self) -> None:
        """Select the next row after the current one whose synthesis failed (Ctrl+J)."""
        project = self.ctx.project
        if project is None or not project.state.slides:
            return
        n = len(project.state.slides)
        current = self.table.selected
        start = (current if current is not None else -1) + 1
        for step in range(n):
            row = (start + step) % n
            if project.state.slides[row].status == SlideStatus.ERROR:
                self.table.select(row)
                self.table.scroll_to(row)
                self.update_buttons()
                return
        self.ctx.message.emit("info", "No failed slides.")

    # -- row actions --------------------------------------------------------------------

    def _row_action(self, row: int, action: str) -> object:
        """Row menu / double-click / play pill. May return a coroutine (the notes editor)."""
        handlers: dict[str, Callable[[], object]] = {
            "edit": self.edit_selected,
            "play": self.play_selected,
            "regenerate": self.regenerate_selected,
            "include": self.toggle_include_selected,
            "reset": self.reset_selected_notes,
        }
        handler = handlers.get(action)
        return handler() if handler is not None else None

    def selected_row(self) -> int | None:
        return self.table.selected

    def update_buttons(self) -> None:
        project = self.ctx.project
        row = self.selected_row()
        busy = self.ctx.busy
        s = self.table.slide(row) if row is not None else None
        has = s is not None
        self.render_btn.set_enabled(project is not None and not busy)
        self.make_btn.set_enabled(project is not None and not busy)
        self.reload_btn.set_enabled(project is not None and not busy)
        self.close_btn.set_enabled(not busy)
        self.play_btn.set_enabled(bool(s and s.audio_path and Path(s.audio_path).is_file()))
        self.regen_btn.set_enabled(bool(s and s.include and s.has_narration and not busy))
        self.edit_btn.set_enabled(has and not busy)
        self.include_btn.set_enabled(has and not busy)
        self.include_btn.set_text("Include" if (s and not s.include) else "Exclude")
        self.reset_btn.set_enabled(bool(s and s.notes_override is not None and not busy))

    def play_row(self, row: int) -> None:
        """Toggle playback of one slide's generated audio (table pill, Enter, toolbar)."""
        s = self.table.slide(row)
        if s is None or not s.audio_path:
            return
        path = Path(s.audio_path)
        if path.is_file():
            self.player.toggle(path)
            self._sync_playing()

    def _sync_playing(self) -> None:
        playing: int | None = None
        project = self.ctx.project
        current = self.player.current
        if project is not None and current is not None and self.player.is_playing():
            for i, s in enumerate(project.state.slides):
                if s.audio_path and Path(s.audio_path) == current:
                    playing = i
                    break
        self.table.set_playing_row(playing)

    def play_selected(self) -> None:
        row = self.selected_row()
        if row is not None:
            self.play_row(row)

    def regenerate_selected(self) -> None:
        row = self.selected_row()
        if row is not None and not self.ctx.busy:
            self.ctx.start_synthesis(only={row}, force=True)

    async def edit_selected(self) -> None:
        row = self.selected_row()
        s = self.table.slide(row) if row is not None else None
        if s is None or row is None:
            return
        if self.ctx.busy:
            ui.notify("A task is running - notes can be edited when it finishes.")
            return
        with (
            ui.dialog() as dialog,
            ui.card().classes("sc-card gap-3").style("width: 760px; max-width: 90vw"),
        ):
            ui.label(f"Slide {s.number}: {s.title}").classes("sc-panel-title sc-ellipsis w-full")
            ui.label(
                "Edited notes override the deck's speaker notes for narration only; the deck "
                "itself is not changed."
            ).classes("sc-muted")
            editor = ui.textarea(value=s.effective_notes).classes("w-full")
            editor.props("outlined autogrow autofocus input-style='min-height: 220px'")
            with ui.row().classes("w-full justify-end gap-2"):
                button("Cancel", lambda: dialog.submit(None), kind="quiet")
                button("Save", lambda: dialog.submit(editor.value or ""), kind="primary")
        text = await dialog
        dialog.delete()
        if isinstance(text, str) and text != s.effective_notes:
            self.on_notes_edited(row, text)

    def toggle_include_selected(self) -> None:
        row = self.selected_row()
        project = self.ctx.project
        if row is None or project is None or self.ctx.busy:
            return
        project.set_include(row, not project.state.slides[row].include)
        self.on_slides_changed()

    def reset_selected_notes(self) -> None:
        row = self.selected_row()
        project = self.ctx.project
        if row is None or project is None or self.ctx.busy:
            return
        project.set_notes_override(row, None)
        self.on_slides_changed()

    def on_notes_edited(self, row: int, text: str) -> None:
        project = self.ctx.project
        if project is None or self.ctx.busy:
            return
        project.set_notes_override(row, text)
        self.on_slides_changed()
