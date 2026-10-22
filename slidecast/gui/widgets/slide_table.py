"""Slide table: a Quasar QTable with a Vue row template, plus the thumbnail loader.

Rows are plain dicts rebuilt from the project state; the template renders the thumbnail, the
state chip, the play pill and a right-click menu, and reports clicks back as table events.
Changes are batched and pushed to the page at most four times a second, so a synthesis run that
touches every row does not flood the connection.
"""

from __future__ import annotations

import inspect
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nicegui import ui
from nicegui.events import GenericEventArguments

from slidecast.core.logging_setup import get_logger
from slidecast.core.pipeline import ThumbSource
from slidecast.core.project import Project, SlideState
from slidecast.core.textprep import prepare
from slidecast.core.timeline import format_duration
from slidecast.gui import media
from slidecast.gui.signals import call_soon
from slidecast.gui.theme import STATE_COLORS

log = get_logger("gui")

THUMB_W = 128
ROW_H = 84

COLUMNS: list[dict[str, Any]] = [
    {"name": "num", "label": "#", "field": "num", "align": "right", "style": "width: 40px"},
    {"name": "thumb", "label": "Slide", "field": "thumb", "align": "left", "style": "width: 144px"},
    {"name": "title", "label": "Title", "field": "title", "align": "left", "style": "width: 220px"},
    {"name": "notes", "label": "Notes", "field": "notes", "align": "left"},
    {"name": "words", "label": "Words", "field": "words", "align": "right", "style": "width: 64px"},
    {"name": "duration", "label": "Duration", "field": "duration", "align": "right"},
    {
        "name": "audio",
        "label": "Audio",
        "field": "audio",
        "align": "center",
        "style": "width: 110px",
    },
    {"name": "state", "label": "State", "field": "state", "align": "left", "style": "width: 110px"},
]

# One Vue template for the whole row: the notes column needs ellipsis + override colouring, the
# audio column a button, the state column a chip, and the row a context menu.
_ROW = r"""
<q-tr :props="props"
      :class="[props.row.sel ? 'selected' : '', props.row.include ? '' : 'excluded']"
      @click="$parent.$emit('select', props.row.idx)"
      @dblclick="$parent.$emit('action', {row: props.row.idx, action: 'edit'})">
  <q-td key="num" :props="props">{{ props.row.num }}</q-td>
  <q-td key="thumb" :props="props">
    <img v-if="props.row.thumb" :src="props.row.thumb" class="sc-thumb" loading="lazy" />
    <div v-else class="sc-thumb sc-thumb-empty"></div>
  </q-td>
  <q-td key="title" :props="props" class="sc-ellipsis" style="max-width: 220px"
        :title="props.row.override ? 'Notes edited in Slidecast (override)' : props.row.title">
    {{ props.row.title }}
  </q-td>
  <q-td key="notes" :props="props" class="sc-notes-cell"
        :class="props.row.override && props.row.include ? 'sc-override' : ''"
        :title="props.row.notes_full">
    {{ props.row.notes }}
  </q-td>
  <q-td key="words" :props="props">{{ props.row.words }}</q-td>
  <q-td key="duration" :props="props">{{ props.row.duration }}</q-td>
  <q-td key="audio" :props="props">
    <q-btn dense rounded no-caps unelevated size="sm" style="min-width: 64px"
           :outline="!props.row.playing" :disable="!props.row.has_audio"
           :color="props.row.has_audio ? 'primary' : 'grey-7'"
           :icon="props.row.playing ? 'pause' : 'play_arrow'"
           :label="props.row.playing ? '' : 'Play'"
           @click.stop="$parent.$emit('action', {row: props.row.idx, action: 'play'})" />
  </q-td>
  <q-td key="state" :props="props">
    <span class="sc-badge" :class="'sc-badge-' + props.row.state_color" :title="props.row.error">
      {{ props.row.state }}
    </span>
  </q-td>
  <q-menu context-menu touch-position
          @before-show="$parent.$emit('select', props.row.idx)">
    <q-list dense style="min-width: 220px">
      <q-item clickable v-close-popup
              @click="$parent.$emit('action', {row: props.row.idx, action: 'edit'})">
        <q-item-section>Edit notes</q-item-section>
      </q-item>
      <q-item clickable v-close-popup
              @click="$parent.$emit('action', {row: props.row.idx, action: 'regenerate'})">
        <q-item-section>Regenerate this slide</q-item-section>
      </q-item>
      <q-item clickable v-close-popup :disable="!props.row.has_audio"
              @click="$parent.$emit('action', {row: props.row.idx, action: 'play'})">
        <q-item-section>Play generated audio</q-item-section>
      </q-item>
      <q-separator />
      <q-item clickable v-close-popup
              @click="$parent.$emit('action', {row: props.row.idx, action: 'include'})">
        <q-item-section>{{ props.row.include ? 'Exclude' : 'Include' }}</q-item-section>
      </q-item>
      <q-item clickable v-close-popup :disable="!props.row.override"
              @click="$parent.$emit('action', {row: props.row.idx, action: 'reset'})">
        <q-item-section>Reset notes to deck</q-item-section>
      </q-item>
    </q-list>
  </q-menu>
</q-tr>
"""


class SlideTable:
    def __init__(
        self,
        on_select: Callable[[int], None],
        on_action: Callable[[int, str], object],
    ) -> None:
        self._project: Project | None = None
        self._thumbs: dict[int, str] = {}
        self.selected: int | None = None
        self._playing_row: int | None = None
        self._rows: list[dict[str, Any]] = []
        self._dirty = False
        self._on_select = on_select
        self._on_action = on_action

        self.table = (
            ui.table(columns=COLUMNS, rows=[], row_key="idx", pagination=0)
            .classes("sc-table w-full")
            .props(
                f"flat dense virtual-scroll :virtual-scroll-item-size={ROW_H} "
                "hide-pagination :rows-per-page-options=[0] separator=horizontal"
            )
            .style("height: calc(100vh - 330px); min-height: 280px")
        )
        self.table.add_slot("body", _ROW)
        self.table.add_slot(
            "no-data",
            '<div class="w-full text-center q-py-lg sc-muted">This deck has no slides.</div>',
        )
        self.table.on("select", self._select_event)
        self.table.on("action", self._action_event)
        ui.timer(0.25, self._flush)

    # -- events -------------------------------------------------------------------------------

    def _select_event(self, e: GenericEventArguments) -> None:
        row = int(e.args)
        self.select(row)
        self._on_select(row)

    async def _action_event(self, e: GenericEventArguments) -> None:
        args = e.args if isinstance(e.args, dict) else {}
        row = int(args.get("row", -1))
        if row < 0:
            return
        self.select(row)
        self._on_select(row)
        result = self._on_action(row, str(args.get("action", "")))
        if inspect.isawaitable(result):
            await result

    # -- data binding -------------------------------------------------------------------------

    def set_project(self, project: Project | None) -> None:
        self._project = project
        self._thumbs.clear()
        self._playing_row = None
        self.selected = None
        self.refresh()

    def slide(self, row: int) -> SlideState | None:
        if self._project is None or not (0 <= row < len(self._project.state.slides)):
            return None
        return self._project.state.slides[row]

    def _row(self, i: int, s: SlideState) -> dict[str, Any]:
        text = " ".join(s.effective_notes.split())
        if s.duration is not None:
            duration = format_duration(s.duration)
        else:
            duration = f"~{format_duration(s.estimated_seconds)}" if s.has_narration else "hold"
        has_audio = s.audio_path is not None and Path(s.audio_path).is_file()
        return {
            "idx": i,
            "num": s.number,
            "thumb": self._thumbs.get(i),
            "title": s.title,
            "notes": text[:200] + ("..." if len(text) > 200 else ""),
            "notes_full": s.effective_notes or "(no notes: silent hold)",
            "override": s.notes_override is not None,
            "words": s.word_count,
            "duration": duration,
            "has_audio": has_audio,
            "playing": self._playing_row == i,
            "state": s.status.value,
            "state_color": STATE_COLORS.get(s.status.value, "muted"),
            "error": s.error or "",
            "include": s.include,
            "sel": self.selected == i,
        }

    def refresh(self) -> None:
        project = self._project
        slides = project.state.slides if project is not None else []
        self._rows = [self._row(i, s) for i, s in enumerate(slides)]
        self._dirty = True

    def refresh_row(self, row: int) -> None:
        s = self.slide(row)
        if s is not None and row < len(self._rows):
            self._rows[row] = self._row(row, s)
            self._dirty = True

    def set_thumbnail(self, row: int, url: str) -> None:
        self._thumbs[row] = url
        if row < len(self._rows):
            self._rows[row]["thumb"] = url
            self._dirty = True

    def set_playing_row(self, row: int | None) -> None:
        old, self._playing_row = self._playing_row, row
        for r in (old, row):
            if r is not None:
                self.refresh_row(r)

    def select(self, row: int | None) -> None:
        old, self.selected = self.selected, row
        for r in (old, row):
            if r is not None:
                self.refresh_row(r)

    def scroll_to(self, row: int) -> None:
        self.table.run_method("scrollTo", row, "center-force")

    def _flush(self) -> None:
        if self._dirty:
            self._dirty = False
            self.table.rows = list(self._rows)


class ThumbnailLoader(threading.Thread):
    """Loads slide thumbnails off the UI loop.

    Still-image projects already have the PNGs; video projects get one frame extracted from each
    slide clip on first view, cached next to the clips. When neither exists yet, ``preview_fn``
    rasterises low-res previews straight from the deck so the table is not blank pre-render.
    ``on_loaded(row, path)`` is delivered on the UI loop.
    """

    def __init__(
        self,
        sources: list[ThumbSource],
        on_loaded: Callable[[int, Path], None],
        ffmpeg: Path | None = None,
        preview_fn: Callable[[Callable[[], bool]], list[Path]] | None = None,
    ) -> None:
        super().__init__(name="slidecast: thumbnails", daemon=True)
        self._sources = sources
        self._on_loaded = on_loaded
        self._ffmpeg = ffmpeg
        self._preview_fn = preview_fn
        self._stop_flag = False

    def stop(self) -> None:
        self._stop_flag = True

    def _emit(self, row: int, path: Path) -> None:
        if not self._stop_flag:
            call_soon(self._on_loaded, row, path)

    def run(self) -> None:
        from slidecast.core import ffmpeg as F
        from slidecast.core.errors import SlidecastError

        for source in self._sources:
            if self._stop_flag:
                return
            path = source.image
            if not path.is_file():
                if source.clip is None or self._ffmpeg is None:
                    continue
                try:
                    path = F.extract_thumbnail(self._ffmpeg, source.clip, path, width=THUMB_W * 2)
                except SlidecastError as exc:
                    log.warning("No thumbnail for slide %d: %s", source.index + 1, exc)
                    continue
            self._emit(source.index, path)
        if self._preview_fn is None:
            return
        try:
            paths = self._preview_fn(lambda: self._stop_flag)
        except SlidecastError as exc:
            # Previews are a nicety, not a requirement: the table just stays blank.
            log.warning("Slide previews failed: %s", exc)
            return
        for row, path in enumerate(paths):
            if self._stop_flag:
                return
            if path.is_file():
                self._emit(row, path)


def thumb_url(path: Path) -> str:
    return media.thumb_url(path)


def cleaned_preview(project: Project, row: int) -> tuple[str, list[str]]:
    s = project.state.slides[row]
    p = prepare(s.effective_notes, project.settings.text)
    return p.cleaned, p.chunks
