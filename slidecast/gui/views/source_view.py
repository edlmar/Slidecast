"""Step 1: choose what to work on - a single deck, or a folder of decks (batch).

Both live in one step because they are the same decision ("what am I narrating?"), just at
different scales. The mode switch flips between the single-deck slide table and the folder table;
opening a deck or a folder selects the matching mode on its own.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from nicegui import ui

from slidecast.core.project import Project
from slidecast.gui.context import AppContext
from slidecast.gui.views.batch_view import BatchView
from slidecast.gui.views.deck_view import DeckView
from slidecast.gui.widgets.player import Player

SINGLE, FOLDER = 0, 1


class SourceView:
    def __init__(
        self, ctx: AppContext, player: Player, on_make_video: Callable[[], object]
    ) -> None:
        self.ctx = ctx
        self._mode = SINGLE
        with ui.column().classes("w-full gap-4") as self.root:
            with ui.row().classes("w-full items-center gap-4"):
                ui.label("Decks").classes("sc-title")
                self.toggle = ui.toggle(
                    {SINGLE: "Single deck", FOLDER: "Folder (batch)"},
                    value=SINGLE,
                    on_change=lambda e: self.set_mode(int(e.value)),
                ).props("no-caps unelevated toggle-color=primary color=dark text-color=grey-5")
                ui.space()
                self.summary = ui.label("").classes("sc-muted")
            self.deck_view = DeckView(
                ctx, player, on_batch_folder=self.open_folder, on_make_video=on_make_video
            )
            self.batch_view = BatchView(ctx, on_open_deck=self.open_deck)

        ctx.project_changed.connect(self.on_project_changed)
        ctx.slides_changed.connect(self.update_summary)
        ctx.batch_changed.connect(self.update_summary)
        ctx.task_finished.connect(lambda *_: self.update_summary())
        self.set_mode(SINGLE)

    # -- mode ------------------------------------------------------------------------------

    def set_mode(self, mode: int) -> None:
        self._mode = mode
        if self.toggle.value != mode:
            self.toggle.set_value(mode)
        self.deck_view.root.set_visibility(mode == SINGLE)
        self.batch_view.root_el.set_visibility(mode == FOLDER)
        self.update_summary()

    @property
    def mode(self) -> int:
        return self._mode

    def open_folder(self, folder: Path) -> None:
        self.set_mode(FOLDER)
        self.batch_view.set_root(folder)

    async def open_deck(self, deck: Path) -> None:
        await self.deck_view.open_deck(deck)

    def on_project_changed(self, project: Project | None) -> None:
        if project is not None:
            self.set_mode(SINGLE)
        self.update_summary()

    # -- shared surface for the main window -------------------------------------------------

    def update_summary(self) -> None:
        text = self.batch_view.summary_text if self.mode == FOLDER else self.deck_view.summary_text
        if self.summary.text != text:
            self.summary.set_text(text)

    def hint(self) -> str:
        """One-line sidebar hint for whichever panel is showing."""
        if self.mode == FOLDER:
            return self.batch_view.hint()
        project = self.ctx.project
        if project is None:
            return "no deck"
        inc = len(project.state.included_slides())
        return f"{inc} slide{'s' if inc != 1 else ''}"

    async def browse_deck(self) -> None:
        self.set_mode(SINGLE)
        await self.deck_view.browse_deck()

    async def browse_folder(self) -> None:
        self.set_mode(FOLDER)
        await self.batch_view.browse_decks()
