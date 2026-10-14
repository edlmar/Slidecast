"""Rebuild step: remake one part of a video without redoing the rest.

Each row is a piece of the pipeline - slide images, slide videos, narration, the export - showing
what it currently costs on disk and offering the two useful verbs: rebuild it, or throw it away so
the next run makes it fresh.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from nicegui import ui

from slidecast.core import components as C
from slidecast.core.project import Project
from slidecast.gui.context import AppContext
from slidecast.gui.ui_kit import alert, button, card, confirm, header, tip


class ComponentRow:
    """One rebuildable piece: what it is, what it costs, and how to remake it."""

    def __init__(
        self, key: str, on_rebuild: Callable[[str], object], on_clear: Callable[[str], object]
    ) -> None:
        self.key = key
        row = ui.row().classes("sc-card w-full items-center no-wrap gap-4")
        with row.style("padding: 12px 16px"):
            with ui.column().classes("gap-0 flex-1 min-w-0"):
                self.title = ui.label("").classes("sc-panel-title")
                self.detail = ui.label("").classes("sc-muted")
            self.size_label = ui.label("").classes("sc-mono text-right").style("min-width: 90px")
            self.rebuild_btn = button("Rebuild", lambda: on_rebuild(key))
            self.clear_btn = button("Clear", lambda: on_clear(key), kind="quiet-danger")

    def update_from(self, component: C.Component, busy: bool) -> None:
        self.title.set_text(component.label)
        self.detail.set_text(component.description)
        self.size_label.set_text(f"{component.size_mb:,.1f} MB" if component.present else "-")
        self.rebuild_btn.set_enabled(component.rebuildable and not busy)
        self.clear_btn.set_enabled(component.present and not busy)


class RebuildView:
    def __init__(
        self,
        ctx: AppContext,
        on_rebuild_all: Callable[[], Awaitable[None]],
        on_batch_run: Callable[[], None],
    ) -> None:
        self.ctx = ctx
        self._on_rebuild_all = on_rebuild_all
        self._on_batch_run = on_batch_run

        with ui.column().classes("w-full gap-4") as self.root:
            header(
                "Rebuild",
                "Remake one part of the video without redoing the rest. Everything is cached, so "
                "rebuilding the narration leaves the slides alone, and vice versa.",
            )
            self.scope = ui.label("").classes("sc-muted")

            self.rows: dict[str, ComponentRow] = {}
            for key in C.COMPONENT_KEYS:
                self.rows[key] = ComponentRow(key, self.rebuild, self.clear)

            with card("Specific slides"):
                with ui.row().classes("w-full items-center no-wrap gap-2"):
                    ui.label("Slide numbers")
                    self.slides = ui.input(placeholder="e.g. 3, 7, 12-15   (blank = all)")
                    self.slides.props("outlined dense clearable").classes("flex-1")
                    self.regen_btn = button(
                        "Re-record narration", self.regenerate_slides, kind="primary"
                    )
                ui.label(
                    "Use this after editing notes for a few slides, or when a slide's delivery "
                    "came out wrong. Everything else stays cached."
                ).classes("sc-muted")

            with ui.row().classes("w-full items-center"):
                self.all_btn = button("Rebuild everything", self.rebuild_everything, "danger")
                tip(
                    self.all_btn,
                    "Clear every cache and make the whole video again from the deck.",
                )
                ui.space()
                self.total = ui.label("").classes("sc-muted")

        ctx.project_changed.connect(lambda _p: self.refresh())
        ctx.batch_changed.connect(self.refresh)
        ctx.slides_changed.connect(self.refresh)
        ctx.renders_changed.connect(self.refresh)
        ctx.task_started.connect(lambda _t: self.refresh())
        ctx.task_finished.connect(lambda *_: self.refresh())
        self.refresh()

    # -- scope ------------------------------------------------------------------------------

    @property
    def batch_dirs(self) -> list[Path]:
        """Projects of the decks in the current folder scan (empty outside folder mode)."""
        return [d for d in self.ctx.batch_project_dirs if Project.exists(d)]

    def hint(self) -> str:
        if self.ctx.project is not None:
            return f"{C.total_bytes(self.ctx.project) / 1e6:,.0f} MB cached"
        dirs = self.batch_dirs
        if not dirs:
            return "no deck"
        total = sum(c.bytes for c in C.survey_many(dirs))
        return f"{total / 1e6:,.0f} MB, {len(dirs)} decks"

    # -- state ------------------------------------------------------------------------------

    def refresh(self) -> None:
        project = self.ctx.project
        dirs = self.batch_dirs
        busy = self.ctx.busy
        if project is None and not dirs:
            for row in self.rows.values():
                row.update_from(C.Component(row.key, row.key.title(), "", 0, 0, False, False), True)
            self.scope.set_text("Open a deck, or scan a folder in the Decks step.")
            self.total.set_text("")
            self.slides.set_enabled(False)
            self.regen_btn.set_enabled(False)
            self.all_btn.set_enabled(False)
            return

        if project is not None:
            components = {c.key: c for c in C.survey(project)}
            self.scope.set_text(f"Working on {project.deck_path.name}")
            where = project.directory.name
        else:
            components = {c.key: c for c in C.survey_many(dirs)}
            self.scope.set_text(
                f"Working on all {len(dirs)} deck(s) in the scanned folder - every action below "
                "applies to all of them."
            )
            where = f"{len(dirs)} projects"
        for key, row in self.rows.items():
            row.update_from(components[key], busy)
        total = sum(c.bytes for c in components.values())
        self.total.set_text(f"{total / 1e6:,.0f} MB in {where}")
        # Naming particular slides only makes sense for one deck.
        self.slides.set_enabled(project is not None)
        self.regen_btn.set_enabled(not busy and project is not None)
        tip(
            self.regen_btn,
            "Synthesize these slides again, even if they are cached."
            if project is not None
            else "Open a single deck to re-record particular slides.",
        )
        self.all_btn.set_enabled(not busy)

    def parse_slides(self) -> set[int] | None:
        """Parse "3, 7, 12-15" into 0-based indices. None means every slide."""
        text = (self.slides.value or "").strip()
        if not text:
            return None
        picked: set[int] = set()
        for part in text.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                lo, _, hi = part.partition("-")
                picked.update(range(int(lo) - 1, int(hi)))
            else:
                picked.add(int(part) - 1)
        return picked or None

    # -- actions ----------------------------------------------------------------------------

    def rebuild(self, key: str) -> None:
        if self.ctx.busy:
            return
        project = self.ctx.project
        if project is None:
            dirs = self.batch_dirs
            if not dirs:
                return
            C.clear_many(dirs, key)
            self.refresh()
            if key != "work":
                # Re-running the batch remakes whatever is missing, deck by deck, skipping
                # anything still up to date.
                self._on_batch_run()
            return
        if key == "renders":
            C.clear(project, "renders")
            self.ctx.start_render()
        elif key == "clips":
            C.clear(project, "clips")
            self.ctx.start_slide_clips()
        elif key == "audio":
            self.ctx.start_synthesis(force=True)
        elif key == "work":
            C.clear(project, "work")
        elif key == "output":
            self.ctx.start_export(lambda _r: self.refresh())
        self.refresh()

    async def clear(self, key: str) -> None:
        if self.ctx.busy:
            return
        project = self.ctx.project
        dirs = self.batch_dirs
        if project is None and not dirs:
            return
        survey = C.survey(project) if project is not None else C.survey_many(dirs)
        component = next(c for c in survey if c.key == key)
        across = "" if project is not None else f" across {len(dirs)} decks"
        label = component.label.lower()
        if not await confirm(
            f"Clear {label}?",
            f"Delete {label}{across} ({component.size_mb:,.1f} MB)?\n\n"
            "They will be made again next time they are needed.",
            ok="Clear",
            danger=True,
        ):
            return
        if self._started_meanwhile():
            return
        freed = C.clear(project, key) if project is not None else C.clear_many(dirs, key)
        self.ctx.message.emit("info", f"Cleared {label}: {freed / 1e6:,.1f} MB")
        self.ctx.slides_changed.emit()
        self.refresh()

    def _started_meanwhile(self) -> bool:
        """A task may have started while a confirmation dialog was open."""
        return self.ctx.busy

    async def regenerate_slides(self) -> None:
        if self.ctx.project is None or self.ctx.busy:
            return
        try:
            only = self.parse_slides()
        except ValueError:
            await alert("Slide numbers", "Use numbers and ranges, for example: 3, 7, 12-15")
            return
        self.ctx.start_synthesis(only=only, force=True)

    async def rebuild_everything(self) -> None:
        if self.ctx.busy:
            return
        project = self.ctx.project
        dirs = self.batch_dirs
        if project is None and not dirs:
            return
        total = (
            C.total_bytes(project) / 1e6
            if project is not None
            else sum(c.bytes for c in C.survey_many(dirs)) / 1e6
        )
        if not await confirm(
            "Rebuild everything?",
            f"Clear every cache ({total:,.0f} MB) and make the whole video again?\n\n"
            "Narration for a long lecture can take a while to re-synthesize.",
            ok="Rebuild everything",
            danger=True,
        ):
            return
        if self._started_meanwhile():
            return
        for key in C.COMPONENT_KEYS:
            if project is not None:
                C.clear(project, key)
            else:
                C.clear_many(dirs, key)
        self.ctx.slides_changed.emit()
        self.refresh()
        if project is not None:
            await self._on_rebuild_all()
        else:
            self._on_batch_run()
