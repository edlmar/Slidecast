"""Export step: encoding settings with live size estimate, encoder availability, export."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

from nicegui import ui

from slidecast.core import ffmpeg as F
from slidecast.core import pipeline as P
from slidecast.core.project import Project
from slidecast.core.timeline import format_duration
from slidecast.gui import file_dialogs as D
from slidecast.gui.context import AppContext
from slidecast.gui.ui_kit import button, card, field, header, role, tip
from slidecast.gui.views.voice_view import clamp, number

RESOLUTIONS = {
    "1920x1080": "1920 x 1080",
    "2560x1440": "2560 x 1440",
    "3840x2160": "3840 x 2160",
}
CODECS = {"av1": "AV1", "hevc": "HEVC (H.265)"}
MODES = {
    "auto": "Auto (NVENC, then software)",
    "nvenc": "NVENC only",
    "software": "Software only",
}
CONTAINERS = {"mp4": "MP4", "mkv": "MKV"}
AUDIO_CODECS = {"auto": "Auto (AAC in MP4, Opus in MKV)", "opus": "Opus", "aac": "AAC"}
CHANNELS = {1: "Mono", 2: "Stereo"}
ENCODERS = (
    ("av1_nvenc", "AV1", "NVIDIA GPU"),
    ("libsvtav1", "AV1", "Software"),
    ("hevc_nvenc", "HEVC", "NVIDIA GPU"),
    ("libx265", "HEVC", "Software"),
)
_BADGES = {
    "ok": ("Ready", "ready"),
    "missing": ("Unavailable", "error"),
    "present, failed to initialise": ("Check failed", "warning"),
    "unknown": ("Not checked", "neutral"),
}


def select(options: dict[Any, str], value: Any) -> ui.select:
    s = ui.select(options, value=value).props("outlined dense options-dense")
    return s.classes("flex-1 min-w-0").style("max-width: 280px")


class ExportView:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._loading = False

        with ui.column().classes("w-full gap-4") as self.root:
            header(
                "Export",
                "Stills held for their narration, encoded once. NVENC when available, "
                "software fallback otherwise.",
            )
            # Two columns, and the video fields two abreast: this is the tallest step, and the
            # window has to show all of it without scrolling.
            with ui.row().classes("w-full gap-4 no-wrap items-start"):
                left = ui.column().classes("gap-3 min-w-0").style("flex: 3 1 0")
                with left, card("Video"), ui.grid(columns=2).classes("w-full gap-x-6 gap-y-2"):
                    with field("Resolution", 110):
                        self.resolution = select(RESOLUTIONS, "1920x1080")
                    with field("Frame rate", 110):
                        self.fps = number(30, 1, 120, 1, decimals=0)
                    with field("Codec", 110):
                        self.codec = select(CODECS, "av1")
                    with field("Encoder", 110):
                        self.mode = select(MODES, "auto")
                    with field("Quality (CQ/CRF)", 110) as quality:
                        quality.classes("col-span-2")
                        self.cq_slider = ui.slider(min=0, max=63, step=1, value=30).classes(
                            "flex-1"
                        )
                        self.cq = number(30, 0, 63, 1, decimals=0).style("max-width: 90px")
                    with field("Preset", 110):
                        self.preset = ui.input(placeholder="default (p5 / 6 / medium)")
                        self.preset.props("outlined dense").classes("flex-1").style(
                            "max-width: 280px"
                        )
                    with field("Keyframe interval", 110):
                        self.keyint = number(2.0, 0.1, 30.0, 0.5, decimals=1, suffix="s")
                    with field("Cross-dissolve", 110):
                        self.transition = number(0.4, 0.0, 3.0, 0.1, decimals=1, suffix="s")
                    self.media_after = ui.checkbox("Play slide videos after the narration")
                    tip(
                        self.media_after,
                        "A slide video that has its own sound waits until the narration "
                        "finishes, then plays in full. Silent animations always run under the "
                        "narration.",
                    )

                right = ui.column().classes("gap-3 min-w-0").style("flex: 2 1 0")
                with right, card("Audio & container"):
                    with field("Container", 150):
                        self.container = select(CONTAINERS, "mp4")
                    with field("Audio codec", 150):
                        self.acodec = select(AUDIO_CODECS, "auto")
                    with field("Audio bitrate", 150):
                        self.abitrate = number(128, 32, 512, 16, decimals=0, suffix="kbps")
                    with field("Channels", 150):
                        self.channels = select(CHANNELS, 1)

                    ui.element("div").classes("sc-divider my-1")
                    with ui.row().classes("w-full items-center"):
                        ui.label("Encoder availability").classes("sc-section")
                        ui.space()
                        self.recheck_btn = button("Re-check", self.ctx.probe_encoders, small=True)
                        tip(
                            self.recheck_btn,
                            "Check installed encoders and test NVIDIA hardware encoding",
                        )
                    self.enc_badges: dict[str, ui.label] = {}
                    with ui.grid(columns="1fr auto auto").classes("w-full items-center gap-x-3"):
                        for name, codec, backend in ENCODERS:
                            tip(ui.label(codec), name)
                            tip(ui.label(backend).classes("sc-muted"), name)
                            badge = ui.label("Not checked").classes("sc-badge sc-badge-neutral")
                            badge.style("min-width: 96px")
                            tip(badge, name)
                            self.enc_badges[name] = badge
                    with ui.row().classes("w-full items-center"):
                        self.ffmpeg_label = ui.label("FFmpeg not checked").classes("sc-muted")
                        ui.space()
                        self.details_btn = button("Details", self.toggle_details, small=True)
                        self.details_btn.set_enabled(False)
                    self.ffmpeg_details = ui.label("").classes("sc-mono sc-muted w-full")
                    self.ffmpeg_details.style("white-space: pre-wrap; word-break: break-all")
                    self.ffmpeg_details.set_visibility(False)

                with left, card("Output"):
                    with ui.row().classes("w-full items-center no-wrap gap-2"):
                        self.out_dir = ui.input(placeholder="Project folder (<project>/output)")
                        self.out_dir.props("outlined dense readonly").classes("flex-1 min-w-0")
                        button("Browse", self.browse_output_dir)
                        button("Use project folder", self.reset_output_dir, kind="quiet")
                    self.estimate = ui.label("").classes("sc-mono")
                    with ui.row().classes("w-full items-center gap-2"):
                        self.export_btn = button("Export video", self.export, kind="primary")
                        self.open_btn = button("Open output folder", self.open_folder)
                    self.result = ui.label("").classes("sc-muted")

        # Slider and box edit the same value; each only writes when it differs, so no loop.
        self.cq_slider.on_value_change(lambda e: self._sync_cq(e.value))
        self.cq.on_value_change(lambda e: self._sync_cq(e.value))
        for w in (
            self.resolution,
            self.codec,
            self.mode,
            self.container,
            self.acodec,
            self.channels,
            self.fps,
            self.abitrate,
            self.keyint,
            self.transition,
            self.media_after,
        ):
            w.on_value_change(self.apply)
        self.preset.on("blur", self.apply)
        self.preset.on("keydown.enter", self.apply)

        ctx.project_changed.connect(self.load)
        ctx.slides_changed.connect(self.update_estimate)
        ctx.settings_changed.connect(self.update_estimate)
        ctx.encoders_probed.connect(self.show_encoders)
        ctx.task_started.connect(lambda _t: self.update_buttons())
        ctx.task_finished.connect(lambda *_: self.update_buttons())
        self.load(ctx.project)

    def _sync_cq(self, value: Any) -> None:
        v = int(clamp(value, 0, 63, 30))
        if self.cq_slider.value != v:
            self.cq_slider.set_value(v)
        if self.cq.value != v:
            self.cq.set_value(v)
        self.apply()

    def load(self, project: Project | None = None) -> None:
        """Encoding settings are editable with or without an open deck (a batch uses them too)."""
        self._loading = True
        try:
            project = self.ctx.project
            e = self.ctx.settings().export
            r = self.ctx.settings().render
            res = f"{r.width}x{r.height}"
            self.resolution.set_value(res if res in RESOLUTIONS else "1920x1080")
            self.fps.set_value(e.fps)
            self.codec.set_value(e.video_codec)
            self.mode.set_value(e.encoder_mode)
            self.cq.set_value(e.cq)
            self.cq_slider.set_value(e.cq)
            self.preset.set_value(e.preset)
            self.keyint.set_value(e.keyint_seconds)
            self.transition.set_value(e.transition_seconds)
            self.media_after.set_value(r.media_after_narration)
            self.container.set_value(e.container)
            self.acodec.set_value(e.audio_codec)
            self.abitrate.set_value(e.audio_bitrate_kbps)
            self.channels.set_value(e.audio_channels)
            self.out_dir.set_value(str(e.output_dir) if e.output_dir else "")
            self.result.set_text(
                f"Last output: {project.state.last_output}"
                if project and project.state.last_output
                else ""
            )
        finally:
            self._loading = False
        self.update_estimate()
        self.update_buttons()
        if self.ctx.encoder_report:
            self.show_encoders(self.ctx.encoder_report)

    def apply(self, *_: object) -> None:
        if self._loading:
            return
        e = self.ctx.settings().export
        r = self.ctx.settings().render
        w, h = (int(x) for x in str(self.resolution.value or "1920x1080").split("x"))
        r.width, r.height = w, h
        e.fps = int(clamp(self.fps.value, 1, 120, e.fps))
        e.video_codec = self.codec.value
        e.encoder_mode = self.mode.value
        e.cq = int(clamp(self.cq.value, 0, 63, e.cq))
        e.preset = (self.preset.value or "").strip()
        e.keyint_seconds = clamp(self.keyint.value, 0.1, 30.0, e.keyint_seconds)
        e.transition_seconds = clamp(self.transition.value, 0.0, 3.0, e.transition_seconds)
        r.media_after_narration = bool(self.media_after.value)
        e.container = self.container.value
        e.audio_codec = self.acodec.value
        e.audio_bitrate_kbps = int(clamp(self.abitrate.value, 32, 512, e.audio_bitrate_kbps))
        e.audio_channels = 2 if int(self.channels.value) == 2 else 1
        self.ctx.settings_edited()

    def update_estimate(self) -> None:
        project = self.ctx.project
        settings = self.ctx.settings()
        r, e = settings.render, settings.export
        codec = e.resolved_audio_codec()
        spec = (
            f"{r.width}x{r.height} {e.fps} fps {e.video_codec.upper()} + {codec.upper()} "
            f"{e.audio_bitrate_kbps} kbps"
        )
        if project is None:
            self.estimate.set_text(
                f"{spec}   ·   .{e.container}   ·   open a deck for a length and size estimate"
            )
            return
        inc = project.state.included_slides()
        gap = project.settings.tts.inter_slide_gap
        duration = sum((s.duration or s.estimated_seconds) for s in inc) + gap * max(
            len(inc) - 1, 0
        )
        size = F.estimate_output_bytes(
            duration, e, r.width, r.height, transitions=max(len(inc) - 1, 0)
        )
        self.estimate.set_text(
            f"~{format_duration(duration)}   ·   estimated {size / 1e6:.0f} MB   ·   {spec}"
            f"   ·   {project.deck_path.stem}.{e.container}"
        )

    def show_encoders(self, report: dict[str, str]) -> None:
        for name, badge in self.enc_badges.items():
            status = report.get(name, "unknown")
            text, state = _BADGES.get(status, ("Check failed", "warning"))
            badge.set_text(text)
            badge.classes(replace=f"sc-badge sc-badge-{state}")
            tip(badge, f"{name}: {status}")
        details = report.get("ffmpeg", "")
        missing = details.startswith("missing:")
        if missing:
            self.ffmpeg_label.set_text("FFmpeg not found")
        else:
            self.ffmpeg_label.set_text("FFmpeg detected" if details else "FFmpeg not checked")
        role(self.ffmpeg_label, "error" if missing else "muted")
        tip(self.ffmpeg_label, details)
        self.ffmpeg_details.set_text(details)
        self.details_btn.set_enabled(bool(details))
        if not details:
            self.ffmpeg_details.set_visibility(False)

    def toggle_details(self) -> None:
        self.ffmpeg_details.set_visibility(not self.ffmpeg_details.visible)

    def update_buttons(self) -> None:
        project = self.ctx.project
        self.export_btn.set_enabled(project is not None and not self.ctx.busy)
        tip(
            self.export_btn,
            "" if project else "Open a deck to export; these settings apply to batches too.",
        )
        self.open_btn.set_enabled(project is not None and project.output_dir.is_dir())
        self.recheck_btn.set_enabled(not self.ctx.busy)

    def export(self) -> None:
        self.ctx.start_export(self.on_exported)

    def on_exported(self, result: Any) -> None:
        r: P.ExportResult = result
        self.result.set_text(
            f"{r.output.name}   {r.size_bytes / 1e6:.1f} MB   "
            f"{format_duration(r.duration_seconds)}   {r.encoder}"
        )
        self.update_buttons()

    async def browse_output_dir(self) -> None:
        start = self.out_dir.value or self.ctx.store.last_dir("output")
        path = await D.pick_folder("Output folder", start)
        if path is None:
            return
        self.ctx.store.set_last_dir("output", path)
        self.ctx.settings().export.output_dir = path
        self.out_dir.set_value(str(path))
        self.ctx.settings_edited()
        self.update_buttons()

    def reset_output_dir(self) -> None:
        self.ctx.settings().export.output_dir = None
        self.out_dir.set_value("")
        self.ctx.settings_edited()
        self.update_buttons()

    def open_folder(self) -> None:
        project = self.ctx.project
        if project is None:
            return
        folder = P.output_directory(project)
        folder.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(folder))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
