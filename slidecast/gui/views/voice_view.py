"""Voice step: pick/preview the reference clip, tune generation settings, audition a phrase."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nicegui import ui

from slidecast.core.audio import load_wav, validate_reference_clip
from slidecast.core.project import Project
from slidecast.core.timeline import format_duration
from slidecast.gui import file_dialogs as D
from slidecast.gui.context import AppContext
from slidecast.gui.ui_kit import button, card, field, header, role, tip
from slidecast.gui.widgets.player import Player

TEST_PHRASE = (
    "This is a quick test of the cloned voice. The patient presented with chest pain, "
    "e.g. after exertion [chuckle], and the algorithm runs in O of n log n time."
)


def number(
    value: float, lo: float, hi: float, step: float, decimals: int = 2, suffix: str = ""
) -> ui.number:
    n = ui.number(value=value, min=lo, max=hi, step=step, format=f"%.{decimals}f")
    n.props("outlined dense debounce=400" + (f' suffix="{suffix}"' if suffix else ""))
    return n.classes("flex-1").style("max-width: 200px")


def clamp(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(v, lo), hi)


class VoiceView:
    def __init__(self, ctx: AppContext, player: Player) -> None:
        self.ctx = ctx
        self.player = player
        self._loading = False
        self._audition_path: Path | None = None

        with ui.column().classes("w-full gap-4") as self.root:
            header(
                "Voice",
                "Pick 10-20 seconds of the voice you want to clone - clean speech, no music or "
                "background noise. Only the first 10 seconds are used.",
            )
            with ui.row().classes("w-full gap-4 no-wrap items-start"):
                # Reference clip and Audition share the left column; stacked under both cards
                # the audition made this the tallest step and pushed it past the window.
                left = ui.column().classes("flex-1 gap-3 min-w-0")
                # ---- reference clip card -------------------------------------------------
                with left, card("Reference clip"):
                    with ui.row().classes("w-full items-center no-wrap gap-2"):
                        self.ref_path = ui.input(placeholder="Choose a WAV file")
                        self.ref_path.props("outlined dense readonly").classes("flex-1")
                        button("Browse", self.browse)
                        self.ref_play = button("Play", self.play_reference)
                    self.ref_info = ui.label("").classes("sc-muted")
                    self.ref_warn = ui.label("").classes("sc-warn")

                # ---- voice & pacing settings card ----------------------------------------
                with card("Voice & pacing", "flex-1"):
                    with field("Expressiveness"):
                        self.temperature = number(0.8, 0.05, 2.0, 0.05)
                    tip(
                        self.temperature,
                        "How lively the voice sounds. Higher = more expressive, lower = "
                        "steadier.\n0.8 is a good starting point.",
                    )
                    with field("Pause between sentences"):
                        self.sentence_gap = number(0.0, 0.0, 3.0, 0.05, suffix="s")
                    tip(self.sentence_gap, "Extra pause between sentences.")
                    with field("Pause between slides"):
                        self.slide_gap = number(0.0, 0.0, 5.0, 0.1, suffix="s")
                    tip(self.slide_gap, "Extra pause when the video moves to the next slide.")
                    with field("Silence on slides without narration"):
                        self.hold = number(3.0, 0.5, 60.0, 0.5, suffix="s")
                    tip(self.hold, "How long a slide without narration stays on screen.")

                    # ---- advanced (collapsed by default; two abreast so opening it does
                    # not push the page past the window) ------------------------------------
                    with (
                        ui.expansion("Advanced").classes("w-full").props("dense"),
                        ui.grid(columns=2).classes("w-full gap-x-6 gap-y-2"),
                    ):
                        with field("Top-p", 120):
                            self.top_p = number(0.95, 0.1, 1.0, 0.05)
                        tip(
                            self.top_p,
                            "How much of the model's vocabulary it can choose from.\n"
                            "Leave at the default unless the voice sounds unstable.",
                        )
                        with field("Top-k", 120):
                            self.top_k = number(1000, 1, 5000, 1, decimals=0)
                        tip(
                            self.top_k,
                            "How many candidate sounds the model considers per step.\n"
                            "Leave at the default.",
                        )
                        with field("Repetition penalty", 120):
                            self.rep = number(1.2, 1.0, 3.0, 0.05)
                        tip(
                            self.rep,
                            "Discourages repeated phrasing. Higher = less repetition.",
                        )
                        with field("Seed", 120):
                            self.seed = number(0, 0, 2**31 - 1, 1, decimals=0)
                        tip(
                            self.seed,
                            "Fixes the model's random choices so re-runs sound identical.\n"
                            "Change it only to get a different take of the same text.",
                        )
                        with field("Processor", 120):
                            self.device = ui.select(["cuda", "cpu"], value="cuda")
                            self.device.props("outlined dense").classes("flex-1").style(
                                "max-width: 200px"
                            )
                        tip(
                            self.device,
                            "Leave on cuda (your graphics card). cpu is a slow fallback.",
                        )
                        self.unload = ui.checkbox("Free graphics memory when idle")

                    ui.label(
                        "Changing any of these settings makes the app re-generate all narration - "
                        "cached audio is discarded."
                    ).classes("sc-muted")

                # ---- audition card -------------------------------------------------------
                with left, card("Audition"):
                    self.phrase = ui.textarea(value=TEST_PHRASE).classes("w-full")
                    # dense: without it Quasar keeps room for a floating label this field does
                    # not have, and the text sits low in the box. Fixed rows, not autogrow:
                    # autogrow measures on mount, when this step is still hidden, and sticks at
                    # one line.
                    self.phrase.props("outlined dense rows=4 aria-label='Test phrase'")
                    self.audition_hint = ui.label("").classes("sc-muted")
                    with ui.row().classes("items-center gap-2"):
                        self.audition_btn = button(
                            "Synthesize test phrase", self.audition, kind="primary"
                        )
                        self.aud_play = button("Play result", self.play_audition)
                    self.aud_result = ui.label("").classes("sc-muted")

        for w in (
            self.temperature,
            self.top_p,
            self.top_k,
            self.rep,
            self.seed,
            self.sentence_gap,
            self.slide_gap,
            self.hold,
            self.device,
            self.unload,
        ):
            w.on_value_change(self.apply)
        self.phrase.on_value_change(lambda _e: self.update_buttons())

        ctx.project_changed.connect(self.load)
        ctx.task_started.connect(lambda _t: self.update_buttons())
        ctx.task_finished.connect(lambda *_: self.update_buttons())
        player.changed.connect(self._sync_play_buttons)
        self.load(ctx.project)

    # -- settings <-> widgets --------------------------------------------------------------

    def load(self, project: Project | None = None) -> None:
        """Bind to the open project's settings, or to the shared defaults used by a batch."""
        self._loading = True
        try:
            t = self.ctx.settings().tts
            self.temperature.set_value(t.temperature)
            self.top_p.set_value(t.top_p)
            self.top_k.set_value(t.top_k)
            self.rep.set_value(t.repetition_penalty)
            self.seed.set_value(t.seed)
            self.sentence_gap.set_value(t.inter_sentence_gap)
            self.slide_gap.set_value(t.inter_slide_gap)
            self.hold.set_value(t.empty_slide_hold)
            self.device.set_value(t.device if t.device in ("cuda", "cpu") else "cuda")
            self.unload.set_value(t.unload_when_idle)
            self.show_reference(t.reference_clip)
        finally:
            self._loading = False
        self.update_buttons()

    def apply(self, *_: object) -> None:
        if self._loading:
            return
        t = self.ctx.settings().tts
        t.temperature = clamp(self.temperature.value, 0.05, 2.0, t.temperature)
        t.top_p = clamp(self.top_p.value, 0.1, 1.0, t.top_p)
        t.top_k = int(clamp(self.top_k.value, 1, 5000, t.top_k))
        t.repetition_penalty = clamp(self.rep.value, 1.0, 3.0, t.repetition_penalty)
        t.seed = int(clamp(self.seed.value, 0, 2**31 - 1, t.seed))
        t.inter_sentence_gap = clamp(self.sentence_gap.value, 0.0, 3.0, t.inter_sentence_gap)
        t.inter_slide_gap = clamp(self.slide_gap.value, 0.0, 5.0, t.inter_slide_gap)
        t.empty_slide_hold = clamp(self.hold.value, 0.5, 60.0, t.empty_slide_hold)
        t.device = str(self.device.value or "cuda")
        t.unload_when_idle = bool(self.unload.value)
        self.ctx.settings_edited()

    def update_buttons(self) -> None:
        busy = self.ctx.busy
        reference = self.ctx.settings().tts.reference_clip
        has_ref = reference is not None and Path(reference).is_file()
        has_text = bool((self.phrase.value or "").strip())
        has_result = self._audition_path is not None and self._audition_path.is_file()
        self.audition_btn.set_enabled(has_ref and has_text and not busy)
        self.aud_play.set_enabled(has_result)
        if busy:
            hint = "A task is running. You can synthesize a test phrase when it finishes."
        elif not has_ref:
            hint = "Choose an existing reference clip above to enable synthesis."
        elif not has_text:
            hint = "Enter a test phrase to preview your voice."
        elif has_result:
            hint = "Preview ready. Play the result or edit the phrase to try another take."
        else:
            hint = "Synthesize a short phrase to hear your voice before processing a deck."
        self.audition_hint.set_text(hint)
        tip(self.audition_btn, hint)
        self._sync_play_buttons()

    # -- reference clip --------------------------------------------------------------------

    async def browse(self) -> None:
        path = await D.pick_file("Reference clip", self.ctx.store.last_dir("voice"), D.AUDIO)
        if path is None:
            return
        self.ctx.store.set_last_dir("voice", path)
        self.ctx.settings().tts.reference_clip = path.resolve()
        self.ctx.settings_edited()
        self.show_reference(path)
        self.update_buttons()

    def show_reference(self, path: Path | None) -> None:
        self.ref_path.set_value(str(path) if path else "")
        if path is None or not Path(path).is_file():
            self.ref_info.set_text("")
            self.ref_warn.set_text("" if path is None else f"Missing: {path}")
            role(self.ref_warn, "warn")
            self.ref_play.set_enabled(False)
            return
        report = validate_reference_clip(Path(path))
        self.ref_info.set_text(
            f"{report.summary()}, peak {report.peak_db:.1f} dBFS, {report.loudness_lufs:.1f} LUFS"
        )
        role(self.ref_warn, "error" if report.errors else "warn")
        self.ref_warn.set_text("\n".join(report.errors + report.warnings))
        try:
            load_wav(Path(path))  # decode check: Play stays disabled on a corrupt file
            self.ref_play.set_enabled(True)
        except (RuntimeError, OSError) as exc:
            self.ref_warn.set_text(f"Cannot decode: {exc}")
            self.ref_play.set_enabled(False)

    def play_reference(self) -> None:
        p = self.ctx.settings().tts.reference_clip
        if p:
            self.player.toggle(Path(p))

    def _sync_play_buttons(self) -> None:
        """Button labels follow whichever source the shared player is playing."""
        player = self.player
        ref = self.ctx.settings().tts.reference_clip
        ref_playing = ref is not None and player.is_playing() and player.current == Path(ref)
        aud_playing = (
            self._audition_path is not None
            and player.is_playing()
            and player.current == self._audition_path
        )
        self.ref_play.set_text("Pause" if ref_playing else "Play")
        self.aud_play.set_text("Pause" if aud_playing else "Play result")
        has_result = self._audition_path is not None
        tip(
            self.aud_play,
            "Pause"
            if aud_playing
            else "Play the last synthesized preview"
            if has_result
            else "Synthesize a test phrase first",
        )

    # -- audition ---------------------------------------------------------------------------

    def audition(self) -> None:
        text = (self.phrase.value or "").strip()
        if text:
            self.ctx.start_audition(text, self.on_audition_done)

    def on_audition_done(self, result: Any) -> None:
        wav, sr, path = result
        self._audition_path = Path(path)
        self.aud_result.set_text(
            f"Last take: {format_duration(len(wav) / sr)} - plays automatically"
        )
        self.update_buttons()
        self.player.play(self._audition_path)

    def play_audition(self) -> None:
        if self._audition_path:
            self.player.toggle(self._audition_path)
