"""Batch panel: pick a folder of decks (recursive), an output folder, and run them all."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from nicegui import ui
from nicegui.events import GenericEventArguments

from slidecast.core.batch import BatchItem, find_decks, plan_batch, scan_decks
from slidecast.core.config import ProjectSettings
from slidecast.core.timeline import format_duration
from slidecast.gui import file_dialogs as D
from slidecast.gui.context import AppContext
from slidecast.gui.theme import STATE_COLORS
from slidecast.gui.ui_kit import button, card, field, tip

STATUS_COLORS = {
    **STATE_COLORS,
    "running": "amber",
    "skipped": "muted",
    "exported": "green",
    "narrated": "accent",
    "new": "muted",
}

COLUMNS: list[dict[str, Any]] = [
    {"name": "deck", "label": "Deck", "field": "deck", "align": "left"},
    {"name": "folder", "label": "Folder", "field": "folder", "align": "left"},
    {"name": "status", "label": "Status", "field": "status", "align": "left"},
    {"name": "output", "label": "Output", "field": "output", "align": "left"},
    {"name": "time", "label": "Time", "field": "time", "align": "right"},
]

_ROW = r"""
<q-tr :props="props" :id="'sc-batch-' + props.row.idx"
      @dblclick="$parent.$emit('open', props.row.idx)">
  <q-td key="deck" :props="props">{{ props.row.deck }}</q-td>
  <q-td key="folder" :props="props" class="sc-muted">{{ props.row.folder }}</q-td>
  <q-td key="status" :props="props">
    <span class="sc-badge" :class="'sc-badge-' + props.row.color" :title="props.row.error">
      {{ props.row.status }}
    </span>
  </q-td>
  <q-td key="output" :props="props" class="sc-ellipsis" style="max-width: 420px"
        :title="props.row.output">{{ props.row.output }}</q-td>
  <q-td key="time" :props="props">{{ props.row.time }}</q-td>
</q-tr>
"""


# Replaces Quasar's warning-triangle "No data available": an empty list is the normal state
# before a folder is picked, not an error.
_EMPTY = r"""
<div class="w-full column items-center q-py-lg sc-muted" style="gap: 6px">
  <q-icon name="folder_open" size="28px" />
  <div>Pick a presentations folder above - every .pptx inside it is listed here.</div>
</div>
"""


class BatchView:
    def __init__(
        self,
        ctx: AppContext,
        on_open_deck: Callable[[Path], Awaitable[None]],
    ) -> None:
        self.ctx = ctx
        self._on_open_deck = on_open_deck
        self.items: list[BatchItem] = []
        self.states: dict[Path, str] = {}
        self.root: Path | None = None
        self.summary_text = ""

        with ui.column().classes("w-full gap-4") as self.root_el:
            with card("Folders"):
                with field("Presentations", 140):
                    self.decks_dir = ui.input(placeholder="Folder containing .pptx files")
                    self.decks_dir.props("outlined dense readonly").classes("flex-1")
                    button("Browse", self.browse_decks)
                with field("Output folder", 140):
                    self.out_dir = ui.input(
                        placeholder="Next to each deck (<deck>_slidecast/output)"
                    )
                    self.out_dir.props("outlined dense readonly").classes("flex-1")
                    button("Browse", self.browse_output)
                    button("Default", self.reset_output, kind="quiet")
                with field("Reference clip", 140):
                    self.ref_clip = ui.input(placeholder="Reference voice WAV")
                    self.ref_clip.props("outlined dense readonly").classes("flex-1")
                    button("Browse", self.browse_ref)
                self.skip = ui.checkbox("Skip decks whose video is already up to date", value=True)
                ui.label(
                    "Every deck is processed with the settings shown in the Voice, Synthesis and "
                    "Export steps - editable there right now, with or without a deck open. "
                    "Output mirrors the subfolder structure of the presentations folder."
                ).classes("sc-muted")

            with ui.row().classes("w-full items-center gap-2"):
                self.run_btn = button("Run batch", self.run, kind="primary")
                self.rescan_btn = button("Rescan", self.rescan)
                ui.space()
                ui.label("Double-click a deck to open it on its own").classes("sc-muted")

            self.table = (
                ui.table(columns=COLUMNS, rows=[], row_key="idx", pagination=0)
                .classes("sc-table w-full")
                .props("flat dense hide-pagination :rows-per-page-options=[0]")
                .style("max-height: calc(100vh - 520px); min-height: 150px")
            )
            self.table.add_slot("body", _ROW)
            self.table.add_slot("no-data", _EMPTY)
            self.table.on("open", self._open_event)

        ctx.batch_item_changed.connect(self.on_item)
        ctx.task_started.connect(lambda _t: self.update_buttons())
        ctx.task_finished.connect(lambda *_: self.update_buttons())
        ctx.settings_changed.connect(self.sync_from_defaults)
        self.sync_from_defaults()
        self.update_buttons()

    # -- template settings ---------------------------------------------------------------------

    def template(self) -> ProjectSettings:
        """Settings applied to every deck in the batch: exactly what Voice/Synthesis/Export show."""
        t = self.ctx.settings().model_copy(deep=True)
        t.tts.reference_clip = Path(self.ref_clip.value) if self.ref_clip.value else None
        t.export.output_dir = None  # per-item output dirs are set by plan_batch
        return t

    def sync_from_defaults(self) -> None:
        t = self.ctx.settings()
        if not self.ref_clip.value and t.tts.reference_clip:
            self.ref_clip.set_value(str(t.tts.reference_clip))
        if not self.out_dir.value and t.export.output_dir:
            self.out_dir.set_value(str(t.export.output_dir))

    # -- folder pickers --------------------------------------------------------------------------

    def set_root(self, folder: Path) -> None:
        """Prefill the presentations folder (used by the Deck step's folder picker)."""
        self.decks_dir.set_value(str(folder))
        self.ctx.store.set_last_dir("batch", folder)
        self.rescan()

    async def browse_decks(self) -> None:
        path = await D.pick_folder("Folder with presentations", self.ctx.store.last_dir("batch"))
        if path:
            self.ctx.store.set_last_dir("batch", path)
            self.decks_dir.set_value(str(path))
            self.rescan()

    async def browse_output(self) -> None:
        start = self.out_dir.value or self.ctx.store.last_dir("output")
        path = await D.pick_folder("Output folder", start)
        if path:
            self.ctx.store.set_last_dir("output", path)
            self.out_dir.set_value(str(path))
            self.rescan()

    def reset_output(self) -> None:
        self.out_dir.set_value("")
        self.rescan()

    async def browse_ref(self) -> None:
        path = await D.pick_file("Reference clip", self.ctx.store.last_dir("voice"), D.AUDIO)
        if path:
            self.ctx.store.set_last_dir("voice", path)
            self.ref_clip.set_value(str(path))
            self.update_buttons()

    def hint(self) -> str:
        """One-line summary for the sidebar."""
        if not self.items:
            return "no folder"
        done = sum(1 for it in self.items if it.status in ("done", "skipped", "exported"))
        return f"{done}/{len(self.items)} decks"

    # -- scanning / table ----------------------------------------------------------------------

    def rescan(self) -> None:
        if self.ctx.busy:
            # A running batch owns this item list: it writes statuses into it and reports rows
            # by index. Rebuilding the table mid-run would mislabel the decks it is working on.
            return
        text = self.decks_dir.value
        self.root = Path(text) if text else None
        if self.root is None or not self.root.is_dir():
            self.items = []
            self.states = {}
        else:
            out = Path(self.out_dir.value) if self.out_dir.value else None
            self.items = plan_batch(self.root, self.template(), out)
            # Show what each deck already is (new / narrated / exported) before anything runs.
            self.states = {e.deck: e.status for e in scan_decks(self.root)}
            for it in self.items:
                it.status = self.states.get(it.deck, "pending")
        self.fill_table()
        self.update_buttons()

    def _row(self, i: int, item: BatchItem) -> dict[str, Any]:
        rel = item.deck.parent.relative_to(self.root) if self.root else item.deck.parent
        return {
            "idx": i,
            "deck": item.deck.name,
            "folder": str(rel) if str(rel) != "." else "(root)",
            "status": item.status,
            "color": STATUS_COLORS.get(item.status, "muted"),
            "error": item.error or "",
            "output": str(item.output),
            "time": format_duration(item.seconds) if item.seconds else "",
        }

    def fill_table(self) -> None:
        self.table.rows = [self._row(i, it) for i, it in enumerate(self.items)]
        found = len(find_decks(self.root)) if self.root and self.root.is_dir() else 0
        self.summary_text = f"{found} deck{'s' if found != 1 else ''} found" if self.items else ""
        self.ctx.batch_project_dirs = [it.project_dir for it in self.items]
        self.ctx.batch_changed.emit()

    def on_item(self, i: int, item: object) -> None:
        if isinstance(item, BatchItem) and 0 <= i < len(self.table.rows):
            rows = list(self.table.rows)
            rows[i] = self._row(i, item)
            self.table.rows = rows
            self.table.client.run_javascript(
                f"document.getElementById('sc-batch-{i}')?.scrollIntoView({{block: 'nearest'}})"
            )

    # -- run -----------------------------------------------------------------------------------

    def update_buttons(self) -> None:
        busy = self.ctx.busy
        self.run_btn.set_enabled(bool(self.items) and bool(self.ref_clip.value) and not busy)
        self.rescan_btn.set_enabled(not busy)
        missing_ref = bool(self.items) and not self.ref_clip.value
        tip(self.run_btn, "Pick a reference clip first." if missing_ref else "")

    async def _open_event(self, e: GenericEventArguments) -> None:
        """Double-click a row: work on that one deck in the single-deck panel."""
        i = int(e.args)
        if 0 <= i < len(self.items):
            await self._on_open_deck(self.items[i].deck)

    def run(self) -> None:
        if not self.items:
            return
        for it in self.items:
            it.status, it.error, it.seconds = "pending", None, 0.0
        self.fill_table()
        self.ctx.start_batch(self.items, self.template(), bool(self.skip.value), self.on_done)

    def on_done(self, result: Any) -> None:
        self.fill_table()
