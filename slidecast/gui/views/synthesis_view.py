"""Synthesis step: text preparation settings, cache overview, run/cancel, per-slide preview."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from nicegui import ui

from slidecast.core import pipeline as P
from slidecast.core.project import Project, SlideStatus
from slidecast.core.timeline import format_duration
from slidecast.gui.context import AppContext
from slidecast.gui.theme import TOKENS
from slidecast.gui.ui_kit import button, card, field, tip
from slidecast.gui.views.voice_view import clamp, number
from slidecast.gui.widgets.slide_table import cleaned_preview

# (bucket, colour, legend suffix) in the order the bar fills.
_BUCKETS = (
    ("cached", TOKENS["green"], "cached"),
    ("todo", TOKENS["accent"], "to generate"),
    ("errors", TOKENS["red"], "failed"),
    ("silent", "rgba(138, 144, 153, 0.4)", "silent"),  # muted at ~40%: in the deck, no sound
)


class NarrationBar:
    """Proportional strip: how much of the included narration is cached, pending or silent."""

    def __init__(self) -> None:
        self._segments: dict[str, tuple[ui.element, str]] = {}
        with ui.element("div").classes("sc-bar"):
            for key, color, _label in _BUCKETS:
                segment = ui.element("div").style(f"background:{color};flex:0 0 0")
                self._segments[key] = (segment, color)

    def set_counts(self, counts: dict[str, int]) -> None:
        for key, (segment, color) in self._segments.items():
            n = counts.get(key, 0)
            segment.style(replace=f"background:{color};flex:{n} {n} 0")


class SynthesisView:
    def __init__(self, ctx: AppContext, on_open_deck: Callable[[], Awaitable[None]]) -> None:
        self.ctx = ctx
        self._loading = False

        with ui.column().classes("w-full gap-4") as self.root:
            with ui.row().classes("w-full items-center"):
                ui.label("Synthesis").classes("sc-title")
                ui.space()
                self.unload_btn = button(
                    "Release GPU memory", self.ctx.unload_backend, kind="quiet", small=True
                )
            ui.label(
                "Prepare the narration, review each slide, then generate only what has changed."
            ).classes("sc-subtitle")

            self.empty_state = ui.row().classes("sc-card w-full items-center no-wrap gap-5")
            with self.empty_state:
                with ui.column().classes("gap-1 flex-1"):
                    ui.label("Start with a presentation").classes("sc-panel-title")
                    ui.label(
                        "Open a deck to see its narration and generation status. "
                        "You can still adjust the shared text settings below."
                    ).classes("sc-muted")
                self.open_btn = button("Open a deck", on_open_deck, kind="primary")

            with card("Narration overview") as self.summary_card:
                self.overview = ui.label("").classes("sc-panel-title")
                self.bar = NarrationBar()
                with ui.row().classes("gap-4"):
                    self.legend: dict[str, ui.label] = {}
                    for key, color, _label in _BUCKETS:
                        self.legend[key] = ui.label("").classes("sc-dot").style(f"--dot:{color}")
                with ui.row().classes("items-center gap-2"):
                    self.run_btn = button(
                        "Generate narration", lambda: self.ctx.start_synthesis(), kind="primary"
                    )
                    self.force_btn = button(
                        "Regenerate all", lambda: self.ctx.start_synthesis(force=True), "danger"
                    )
                self.action_hint = ui.label("").classes("sc-muted")

            with ui.row().classes("w-full gap-4 no-wrap items-start"):
                with card("Text preparation", "flex-1"):
                    with field("Chunk size", 140):
                        self.chunk = number(250, 60, 600, 10, decimals=0, suffix="chars")
                    with field("Reading estimate", 140):
                        self.wpm = number(150, 60, 300, 5, decimals=0, suffix="wpm")
                    tip(
                        self.wpm,
                        "Used to estimate duration; this does not change the synthesized "
                        "speaking speed.",
                    )
                    self.abbrev = ui.checkbox("Expand abbreviations")
                    tip(
                        self.abbrev,
                        "Expand abbreviations such as mg, e.g. and GPU before synthesis.",
                    )
                    self.urls = ui.checkbox("Read web addresses aloud")
                    tip(
                        self.urls,
                        "Read web addresses aloud instead of removing them from narration.",
                    )
                    ui.label(
                        "Chunks never break mid-sentence. Tags like [laugh], [chuckle] and [cough] "
                        "are kept."
                    ).classes("sc-muted")

                with card("Narration preview") as preview_card:
                    preview_card.style("flex: 2 1 0; min-width: 0")
                    with ui.row().classes("w-full items-center no-wrap gap-3") as self.controls:
                        ui.label("Slide")
                        self.slide_no = ui.number(value=1, min=1, max=1, step=1, format="%.0f")
                        self.slide_no.props("outlined dense").style("width: 110px")
                        self.preview_meta = ui.label("").classes("sc-muted sc-ellipsis flex-1")
                    self.preview = ui.textarea().classes("w-full sc-mono")
                    self.preview.props("outlined readonly input-style='min-height: 240px'")
                    with (
                        ui.column()
                        .classes("w-full items-center justify-center gap-2")
                        .style("min-height: 240px") as self.preview_empty
                    ):
                        self.preview_empty_title = ui.label("No narration to preview").classes(
                            "sc-panel-title"
                        )
                        self.preview_empty_help = ui.label("").classes("sc-muted text-center")

        self.chunk.on_value_change(self.apply)
        self.wpm.on_value_change(self.apply)
        self.abbrev.on_value_change(self.apply)
        self.urls.on_value_change(self.apply)
        self.slide_no.on_value_change(lambda _e: self.update_preview())

        ctx.project_changed.connect(self.load)
        ctx.slides_changed.connect(self.refresh)
        ctx.slide_changed.connect(lambda _i: self.refresh())
        ctx.settings_changed.connect(self.refresh)
        ctx.task_started.connect(lambda _t: self.update_buttons())
        ctx.task_finished.connect(lambda *_: self.update_buttons())
        self.load(ctx.project)

    def refresh(self) -> None:
        self.refresh_stats()
        self.update_preview()

    def load(self, project: Project | None = None) -> None:
        """Text settings are editable with or without an open deck (a batch uses them too)."""
        self._loading = True
        try:
            project = self.ctx.project
            t = self.ctx.settings().text
            self.chunk.set_value(t.chunk_chars)
            self.abbrev.set_value(t.expand_abbreviations)
            self.urls.set_value(t.speak_urls)
            self.wpm.set_value(t.words_per_minute)
            count = max(len(project.state.slides), 1) if project else 1
            self.slide_no.props(f"max={count}")
            self.slide_no.set_value(1)
            self.slide_no.set_enabled(project is not None and bool(project.state.slides))
        finally:
            self._loading = False
        self.refresh()

    def apply(self, *_: object) -> None:
        if self._loading:
            return
        t = self.ctx.settings().text
        t.chunk_chars = int(clamp(self.chunk.value, 60, 600, t.chunk_chars))
        t.expand_abbreviations = bool(self.abbrev.value)
        t.speak_urls = bool(self.urls.value)
        t.words_per_minute = int(clamp(self.wpm.value, 60, 300, t.words_per_minute))
        self.ctx.settings_edited()

    def refresh_stats(self) -> None:
        project = self.ctx.project
        self.empty_state.set_visibility(project is None)
        self.summary_card.set_visibility(project is not None)
        if project is None:
            self.overview.set_text("")
            self.bar.set_counts({})
            self.update_buttons()
            return
        inc = project.state.included_slides()
        # Errors get their own bucket first; silent slides are simply everything included
        # without narration that did not fail.
        errors = sum(1 for s in inc if s.status == SlideStatus.ERROR)
        narrated = [s for s in inc if s.has_narration and s.status != SlideStatus.ERROR]
        cached = sum(1 for s in narrated if s.status in (SlideStatus.CACHED, SlideStatus.DONE))
        todo = len(narrated) - cached
        try:
            ref = project.settings.tts.reference_clip
            if ref and ref.is_file() and self.ctx.fake_tts:
                plans = P.plan_synthesis(project, self.ctx.backend().model_id)
                cached = sum(
                    1
                    for p in plans
                    if p.cached and p.key is not None and p.slide.status != SlideStatus.ERROR
                )
                todo = sum(1 for p in plans if not p.cached and p.slide.status != SlideStatus.ERROR)
        except Exception:
            pass
        silent = len(inc) - cached - todo - errors
        duration = format_duration(sum((s.duration or s.estimated_seconds) for s in inc))
        if not inc:
            text = "No slides included"
        elif todo and cached:
            text = f"{cached} of {cached + todo} slides narrated · {duration} total"
        elif todo:
            text = f"{todo} {'slide' if todo == 1 else 'slides'} to narrate · {duration} total"
        elif cached:
            text = f"All {cached} {'slide' if cached == 1 else 'slides'} narrated · {duration}"
        elif errors:
            text = f"{errors} {'slide' if errors == 1 else 'slides'} failed · {duration}"
        else:
            text = f"No narration yet · {duration} planned"
        self.overview.set_text(text)
        counts = {"cached": cached, "todo": todo, "errors": errors, "silent": silent}
        for key, _color, label in _BUCKETS:
            n = counts[key]
            # "to generate" always shows, the others only when there is something in them.
            self.legend[key].set_visibility(bool(n) or key == "todo")
            self.legend[key].set_text(f"{n} {label}")
        self.bar.set_counts(counts)
        self.update_buttons()

    def update_buttons(self) -> None:
        project = self.ctx.project
        busy = self.ctx.busy
        ref = self.ctx.settings().tts.reference_clip
        has_ref = ref is not None and ref.is_file()
        included = project.state.included_slides() if project else []
        has_narration = any(slide.has_narration for slide in included)
        ok = project is not None and not busy and has_ref and has_narration
        self.run_btn.set_enabled(ok)
        self.force_btn.set_enabled(ok)
        self.unload_btn.set_enabled(not busy)
        self.open_btn.set_enabled(not busy)
        if busy:
            hint = "A task is running. Generation will be available when it finishes."
        elif project is None:
            hint = "These text settings apply to future decks and folder batches."
        elif not included:
            hint = "No slides are included. Choose slides in Decks first."
        elif not has_narration:
            hint = "The included slides have no narration. Add speaker notes in Decks."
        elif not has_ref:
            hint = "Choose an existing reference clip in Voice to generate narration."
        else:
            hint = "Only new or changed narration is generated. Cached audio is reused."
        self.action_hint.set_text(hint)
        tip(self.run_btn, hint)
        tip(self.force_btn, hint)

    def _show_empty(self, title: str, help_text: str) -> None:
        self.preview.set_value("")
        self.preview_empty_title.set_text(title)
        self.preview_empty_help.set_text(help_text)
        self.preview.set_visibility(False)
        self.preview_empty.set_visibility(True)

    def update_preview(self) -> None:
        project = self.ctx.project
        has_slides = project is not None and bool(project.state.slides)
        self.controls.set_visibility(has_slides)
        if project is None or not project.state.slides:
            self.preview_meta.set_text("")
            self._show_empty(
                "No narration to preview",
                "Open a presentation to review its speaker notes, cleaned and split into "
                "synthesis-ready parts."
                if project is None
                else "This presentation has no slides to preview.",
            )
            return
        n = len(project.state.slides)
        row = int(clamp(self.slide_no.value, 1, n, 1)) - 1
        cleaned, chunks = cleaned_preview(project, row)
        slide = project.state.slides[row]
        if not chunks:
            self.preview_meta.set_text(slide.title)
            self._show_empty(
                "This slide has no narration",
                "Add speaker notes in Decks, or leave this slide silent.",
            )
            return
        body = "\n\n".join(
            f"Part {i + 1} of {len(chunks)}\n{chunk}" for i, chunk in enumerate(chunks)
        )
        if self.preview.value != body:
            self.preview.set_value(body)
        parts = "part" if len(chunks) == 1 else "parts"
        characters = "character" if len(cleaned) == 1 else "characters"
        self.preview_meta.set_text(
            f"{slide.title}   ·   {len(chunks)} {parts}, {len(cleaned)} {characters}"
        )
        self.preview_empty.set_visibility(False)
        self.preview.set_visibility(True)
