"""Stage runner: render -> synthesize -> export. Each stage is individually re-runnable, reports
progress through a callback and honours a cancel token, leaving caches consistent."""

from __future__ import annotations

import contextlib
import json
import math
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from slidecast.core import audio as A
from slidecast.core import ffmpeg as F
from slidecast.core import subtitles as SUB
from slidecast.core.cache import atomic_replace, atomic_write_json, hash_file, slide_audio_key
from slidecast.core.config import ProjectSettings
from slidecast.core.errors import (
    Cancelled,
    FFmpegError,
    ProjectError,
    ReferenceClipError,
    SlidecastError,
)
from slidecast.core.logging_setup import get_logger, timed, timed_stage
from slidecast.core.project import Project, SlideState, SlideStatus
from slidecast.core.render import RenderResult, blend_frames, render_deck
from slidecast.core.textprep import TEXTPREP_VERSION, PreparedText, prepare
from slidecast.core.timeline import SlideClip, Timeline, build_timeline
from slidecast.core.tts import TTSBackend, derive_seed

if TYPE_CHECKING:
    from slidecast.core.slidevideo import ClipSet

log = get_logger("pipeline")


@dataclass
class Progress:
    stage: str  # "render" | "synth" | "export"
    current: int
    total: int
    message: str
    slide_index: int | None = None

    @property
    def fraction(self) -> float:
        return self.current / self.total if self.total else 0.0


ProgressFn = Callable[[Progress], None]


class CancelToken:
    """Cancel + pause for a running stage.

    Every stage calls :meth:`check` at points where stopping is safe (between chunks, slides,
    decks and before each ffmpeg run), so pause and cancel both take effect there and nowhere
    else. Work already handed to ffmpeg or the TTS model finishes first; pause is not a freeze.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._running = threading.Event()
        self._running.set()

    # -- cancel ---------------------------------------------------------------------------

    def cancel(self) -> None:
        self._event.set()
        self._running.set()  # let anyone blocked in check() wake up and raise

    def is_set(self) -> bool:
        return self._event.is_set()

    # -- pause ----------------------------------------------------------------------------

    def pause(self) -> None:
        if not self._event.is_set():
            self._running.clear()

    def resume(self) -> None:
        self._running.set()

    def set_paused(self, paused: bool) -> None:
        self.pause() if paused else self.resume()

    @property
    def is_paused(self) -> bool:
        return not self._running.is_set()

    # -- the one call every stage makes ----------------------------------------------------

    def check(self) -> None:
        """Raise if cancelled; block here while paused."""
        if self._event.is_set():
            raise Cancelled("Cancelled by user")
        if self._running.is_set():
            return
        while not self._running.wait(0.1):
            if self._event.is_set():
                raise Cancelled("Cancelled by user")
        if self._event.is_set():
            raise Cancelled("Cancelled by user")

    def reset(self) -> None:
        self._event.clear()
        self._running.set()


def _noop(_: Progress) -> None:
    return None


def _status_progress(progress: ProgressFn) -> Callable[[str, float | None], None]:
    """Adapt a backend ``on_status`` callback to synth-stage progress events.

    Download progress arrives as a 0..1 fraction; indeterminate phase messages
    ("Loading voice model…", "Encoding reference voice…") arrive with ``frac=None``.
    """

    def _status(msg: str, frac: float | None) -> None:
        if frac is None:
            progress(Progress("synth", 0, 1, msg))
        else:
            progress(Progress("synth", int(frac * 1000), 1000, msg))

    return _status


def _gate(cancel: CancelToken) -> Callable[[], bool]:
    """Adapt a token to the ``() -> bool`` cancel callback used by render/ffmpeg helpers.

    Blocks while paused and reports True once cancelled, so those stages honour pause at their
    own check points without needing to know about the token.
    """

    def gate() -> bool:
        try:
            cancel.check()
        except Cancelled:
            return True
        return False

    return gate


# ---------------------------------------------------------------------------------------------
# Planning / cache keys
# ---------------------------------------------------------------------------------------------


@dataclass
class SlidePlan:
    slide: SlideState
    prepared: PreparedText
    key: str | None  # None for slides without narration
    cached: bool


def reference_hash(project: Project) -> str:
    ref = project.settings.tts.reference_clip
    if ref is None or not Path(ref).is_file():
        raise ReferenceClipError("No reference voice clip is set. Pick one in the Voice step.")
    return hash_file(Path(ref))


def plan_synthesis(project: Project, model_id: str, ref_hash: str | None = None) -> list[SlidePlan]:
    """Compute cache keys for every included slide without touching the model."""
    ref_hash = ref_hash or reference_hash(project)
    cache = project.audio_cache
    tts_params = project.settings.tts.cache_params()
    post_params = project.settings.audio.cache_params()
    plans: list[SlidePlan] = []
    for s in project.state.included_slides():
        prepared = prepare(s.effective_notes, project.settings.text)
        if not prepared.chunks:
            plans.append(SlidePlan(s, prepared, None, True))
            continue
        key = slide_audio_key(
            prepared.cleaned,
            prepared.chunks,
            tts_params,
            post_params,
            ref_hash,
            model_id,
            TEXTPREP_VERSION,
        )
        plans.append(SlidePlan(s, prepared, key, cache.has(key)))
    return plans


# ---------------------------------------------------------------------------------------------
# Stage 1: render
# ---------------------------------------------------------------------------------------------


def stage_render(
    project: Project, progress: ProgressFn = _noop, cancel: CancelToken | None = None
) -> RenderResult:
    cancel = cancel or CancelToken()
    project.ensure_dirs()

    def _p(cur: int, tot: int, msg: str) -> None:
        progress(Progress("render", cur, tot, msg))

    with timed_stage("render", slides=len(project.state.slides)):
        result = render_deck(
            project.deck_path,
            _visual_key_input(project),
            project.renders_dir,
            project.settings.render,
            expected_slides=len(project.state.slides),
            progress=_p,
            cancel=_gate(cancel),
        )
    project.state.render_key = result.key
    project.save()
    return result


def _visual_key_input(project: Project) -> str:
    """What slide renders are keyed on: the visual hash, so notes edits keep cached images.

    States saved before the visual hash existed fall back to the whole-file hash - their
    render directories were built with it, so they stay valid and re-render once.
    """
    return project.state.deck_visual_sha256 or project.state.deck_sha256


def cached_render(project: Project) -> RenderResult | None:
    """Return the render for the current deck if it exists, without invoking LibreOffice."""
    from slidecast.core.cache import render_key

    r = project.settings.render
    key = render_key(_visual_key_input(project), r.width, r.height, r.background)
    d = project.renders_dir / key
    marker = d / "done.json"
    if not marker.is_file():
        return None
    try:
        names = json.loads(marker.read_text(encoding="utf-8"))["pngs"]
    except (OSError, KeyError, json.JSONDecodeError):
        return None
    pngs = [d / n for n in names]
    if len(pngs) != len(project.state.slides) or not all(p.is_file() for p in pngs):
        return None
    return RenderResult(d, pngs, True, key)


# ---------------------------------------------------------------------------------------------
# Stage 2: synthesis
# ---------------------------------------------------------------------------------------------


def synthesize_text(
    backend: TTSBackend,
    prepared: PreparedText,
    project: Project,
    cancel: CancelToken | None = None,
    on_chunk: Callable[[int, int], None] | None = None,
) -> A.ConcatResult:
    """Synthesize all chunks of one slide and join them with inter-sentence gaps."""
    return synthesize_chunks(backend, prepared, project.settings, cancel, on_chunk)


def synthesize_chunks(
    backend: TTSBackend,
    prepared: PreparedText,
    settings: ProjectSettings,
    cancel: CancelToken | None = None,
    on_chunk: Callable[[int, int], None] | None = None,
) -> A.ConcatResult:
    """Synthesize all chunks of a passage and join them with inter-sentence gaps."""
    cancel = cancel or CancelToken()
    tts = settings.tts
    post = settings.audio
    pieces: list[tuple[str, A.FloatArray]] = []
    n = len(prepared.chunks)
    for i, chunk in enumerate(prepared.chunks):
        cancel.check()
        seed = derive_seed(tts.seed, i)
        wav = backend.synthesize(chunk, seed)
        wav = A.postprocess_chunk(wav, backend.sr, post)
        pieces.append((chunk, wav))
        if on_chunk:
            on_chunk(i + 1, n)
    return A.concat_with_gaps(pieces, backend.sr, tts.inter_sentence_gap)


@timed("synth")
def stage_synthesize(
    project: Project,
    backend: TTSBackend,
    progress: ProgressFn = _noop,
    cancel: CancelToken | None = None,
    only: set[int] | None = None,
    force: bool = False,
) -> int:
    """Synthesize narration for included slides. Returns number of slides synthesized."""
    cancel = cancel or CancelToken()
    project.ensure_dirs()
    ref = project.settings.tts.reference_clip
    if ref is None:
        raise ReferenceClipError("No reference voice clip is set. Pick one in the Voice step.")
    report = A.validate_reference_clip(Path(ref))
    if not report.ok:
        raise ReferenceClipError("; ".join(report.errors))
    for w in report.warnings:
        log.warning("Reference clip: %s", w)

    backend.on_status = _status_progress(progress)
    try:
        progress(Progress("synth", 0, 1, "Resolving model version"))
        model_id = backend.model_id
        plans = plan_synthesis(project, model_id, report_hash := hash_file(Path(ref)))
        del report_hash
        todo = [
            p
            for p in plans
            if (only is None or p.slide.index in only)
            and (force or not p.cached)
            and p.key is not None
        ]
        cache = project.audio_cache

        # Mark cached / empty slides immediately so the UI reflects reality before the model loads.
        for p in plans:
            s = p.slide
            if only is not None and s.index not in only:
                continue
            if p.key is None:
                s.status = SlideStatus.DONE
                s.audio_key = None
                s.audio_path = None
                s.duration = project.settings.tts.empty_slide_hold
                s.chunk_count = 0
                s.error = None
            elif p.cached and not force:
                meta = cache.read_meta(p.key)
                s.status = SlideStatus.CACHED
                s.audio_key = p.key
                s.audio_path = str(cache.wav_path(p.key))
                s.duration = float(meta.get("duration", 0.0))
                s.chunk_count = int(meta.get("chunk_count", len(p.prepared.chunks)))
                s.error = None
        project.save()

        total = len(todo)
        log.info(
            "Synthesis plan: %d slide(s) to generate, %d cached, %d silent",
            total,
            sum(1 for p in plans if p.cached and p.key is not None),
            sum(1 for p in plans if p.key is None),
        )
        if total == 0:
            progress(Progress("synth", 1, 1, "All slides already cached"))
            return 0

        progress(Progress("synth", 0, total, "Loading TTS model"))
        backend.load()
        backend.set_reference(Path(ref))
        done = 0
        try:
            for p in todo:
                cancel.check()
                s = p.slide
                assert p.key is not None
                s.status = SlideStatus.GENERATING
                s.error = None
                project.save()
                progress(Progress("synth", done, total, f"Slide {s.number}: {s.title}", s.index))
                t0 = time.perf_counter()
                try:

                    def _chunk(i: int, n: int, s: SlideState = s, done: int = done) -> None:
                        progress(
                            Progress(
                                "synth", done, total, f"Slide {s.number}: chunk {i}/{n}", s.index
                            )
                        )

                    result = synthesize_text(backend, p.prepared, project, cancel, _chunk)
                    tmp = cache.temp_wav_path(p.key)
                    A.save_wav(tmp, result.wav, backend.sr)
                    duration = A.duration_seconds(result.wav, backend.sr)
                    wav_path = cache.commit(
                        p.key,
                        {
                            "slide_index": s.index,
                            "text": p.prepared.cleaned,
                            "chunks": [
                                {"text": t.text, "start": t.start, "end": t.end}
                                for t in result.timings
                            ],
                            "chunk_count": len(result.timings),
                            "duration": duration,
                            "sample_rate": backend.sr,
                            "model_id": backend.model_id,
                            "tts_params": project.settings.tts.cache_params(),
                            "post_params": project.settings.audio.cache_params(),
                            "seconds_to_generate": time.perf_counter() - t0,
                        },
                    )
                    s.status = SlideStatus.DONE
                    s.audio_key = p.key
                    s.audio_path = str(wav_path)
                    s.duration = duration
                    s.chunk_count = len(result.timings)
                    done += 1
                    log.info(
                        "Slide %d: %.1fs of audio in %.1fs (%d chunks)",
                        s.number,
                        duration,
                        time.perf_counter() - t0,
                        len(result.timings),
                    )
                except Cancelled:
                    cache.discard_partial(p.key)
                    s.reset_audio()
                    raise
                except SlidecastError as exc:
                    cache.discard_partial(p.key)
                    s.status = SlideStatus.ERROR
                    s.error = str(exc)
                    log.error("Slide %d failed: %s", s.number, exc)
                    if "out of memory" in str(exc).lower():
                        raise  # OOM will hit every slide; stop now
                finally:
                    project.save()
            progress(Progress("synth", total, total, f"Synthesized {done}/{total} slides"))
        finally:
            if project.settings.tts.unload_when_idle:
                backend.unload()
        return done
    finally:
        backend.on_status = None


def audition(
    backend: TTSBackend,
    settings: ProjectSettings,
    text: str,
    progress: ProgressFn = _noop,
) -> tuple[A.FloatArray, int]:
    """Synthesize a short test phrase with these settings (not cached).

    Takes settings rather than a project so the voice can be auditioned before any deck is
    opened - which is how you tune a voice for a whole batch.
    """
    ref = settings.tts.reference_clip
    if ref is None:
        raise ReferenceClipError("Pick a reference clip first.")
    backend.on_status = _status_progress(progress)
    try:
        backend.load()
        backend.set_reference(Path(ref))
    finally:
        backend.on_status = None
    prepared = prepare(text, settings.text)
    result = synthesize_chunks(backend, prepared, settings)
    wav, _ = A.loudness_normalize(
        result.wav,
        backend.sr,
        settings.audio.target_lufs,
        settings.audio.true_peak_db,
    )
    return wav, backend.sr


# ---------------------------------------------------------------------------------------------
# Stage 3: export
# ---------------------------------------------------------------------------------------------


def output_directory(project: Project) -> Path:
    """Chosen output folder (settings.export.output_dir) or the project's own output/."""
    chosen = project.settings.export.output_dir
    return Path(chosen) if chosen else project.output_dir


def default_output_path(project: Project) -> Path:
    exp = project.settings.export
    return output_directory(project) / f"{project.deck_path.stem}.{exp.container}"


@dataclass
class ExportResult:
    output: Path
    manifest: Path
    encoder: str
    duration_seconds: float
    size_bytes: int
    subtitles: list[Path] = field(default_factory=list)


def _verify_audio_freshness(project: Project, included: list[SlideState]) -> None:
    """Refuse to export narration that no longer matches the slide's text or settings.

    ``audio_path`` pointing at a file is not proof the audio is current: the state file can be
    edited, a deck refresh can change notes, or TTS settings can change after synthesis. The
    cache metadata records exactly what was synthesized, so compare it to what would be
    synthesized now. A matching entry also repairs ``audio_path`` - useful when the project
    folder moved and the stored absolute path went stale.
    """
    cache = project.audio_cache
    tts_params = project.settings.tts.cache_params()
    post_params = project.settings.audio.cache_params()
    missing: list[int] = []  # no usable cache entry at all
    stale: list[int] = []  # an entry exists but no longer matches the slide
    for s in included:
        if not s.has_narration:
            continue
        key = s.audio_key
        if not key or not cache.has(key):
            missing.append(s.number)
            continue
        try:
            meta = cache.read_meta(key)
        except (OSError, json.JSONDecodeError):
            missing.append(s.number)
            continue
        prepared = prepare(s.effective_notes, project.settings.text)
        chunks = meta.get("chunks")
        chunk_texts = [c.get("text") for c in chunks] if isinstance(chunks, list) else []
        fresh = (
            meta.get("text") == prepared.cleaned
            and meta.get("tts_params") == tts_params
            and meta.get("post_params") == post_params
            and chunk_texts == prepared.chunks
        )
        if fresh:
            # Repair audio_path while we are here: the cache key is the real identity, and a
            # stored absolute path goes stale when the project folder moves.
            s.audio_path = str(cache.wav_path(key))
        else:
            stale.append(s.number)
    if missing:
        raise ProjectError(
            f"{len(missing)} slide(s) still need narration audio (e.g. slide {missing[0]}). "
            "Run synthesis first."
        )
    if stale:
        raise ProjectError(
            f"{len(stale)} slide(s) have stale narration (e.g. slide {stale[0]}): the notes or "
            "voice settings changed after synthesis. Re-run synthesis before exporting."
        )


def _slide_audio(project: Project, s: SlideState, sr: int) -> A.FloatArray:
    if s.audio_path and Path(s.audio_path).is_file():
        wav, file_sr = A.load_wav(Path(s.audio_path))
        return A.resample(wav, file_sr, sr)
    if s.has_narration:
        raise ProjectError(f"Slide {s.number} has narration but no audio. Run synthesis first.")
    return A.silence(s.duration or project.settings.tts.empty_slide_hold, sr)


def _narration_slide(
    project: Project, s: SlideState, sr: int, floor: float
) -> tuple[A.FloatArray, float]:
    """One slide's track segment: its audio, padded with silence up to ``floor`` seconds."""
    wav = _slide_audio(project, s, sr)
    seconds = len(wav) / sr
    if floor > seconds:
        wav = np.concatenate([wav, A.silence(floor - seconds, sr)])
        seconds = len(wav) / sr
    return wav, seconds


def build_narration_track(
    project: Project, sr: int = 24000, minimum: list[float] | None = None
) -> tuple[A.FloatArray, list[float]]:
    """Concatenate per-slide audio with inter-slide gaps; returns track and per-slide durations.

    ``minimum`` (one entry per included slide) holds a slide on screen for at least that long -
    used by the video-clip path so a slide whose animation or embedded video outlasts the
    narration keeps playing, with silence under the remainder.

    Holds the whole track in memory - fine for tests and short decks. Exports go through
    ``write_narration_track`` so a 60-minute lecture never becomes a single array.
    """
    parts: list[A.FloatArray] = []
    durations: list[float] = []
    gap_seconds = project.settings.tts.inter_slide_gap
    included = project.state.included_slides()
    for i, s in enumerate(included):
        floor = minimum[i] if minimum is not None and i < len(minimum) else 0.0
        wav, seconds = _narration_slide(project, s, sr, floor)
        parts.append(wav)
        durations.append(seconds)
        if i < len(included) - 1:
            parts.append(A.silence(gap_seconds, sr))
    track = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
    return track.astype(np.float32, copy=False), durations


def write_narration_track(
    project: Project, out_path: Path, sr: int = 24000, minimum: list[float] | None = None
) -> list[float]:
    """Streaming ``build_narration_track``: writes the same layout to a float WAV and returns
    per-slide durations. Peak memory is one slide, not the whole lecture."""
    included = project.state.included_slides()
    gap = A.silence(project.settings.tts.inter_slide_gap, sr)
    durations: list[float] = []

    def parts() -> Iterator[A.FloatArray]:
        for i, s in enumerate(included):
            floor = minimum[i] if minimum is not None and i < len(minimum) else 0.0
            wav, seconds = _narration_slide(project, s, sr, floor)
            durations.append(seconds)
            yield wav
            if i < len(included) - 1:
                yield gap

    A.write_wav_blocks(parts(), out_path, sr, "FLOAT")
    return durations


def resolve_slide_source(project: Project) -> str:
    """Decide between still images and PowerPoint video clips for this deck."""
    from slidecast.core import slidevideo as SV

    choice = project.settings.render.slide_source
    if choice == "stills":
        return "stills"
    if choice == "video":
        if not SV.powerpoint_available():
            raise ProjectError(
                "This project is set to render slides as video (animations and embedded media), "
                "but " + SV.unavailable_reason()
            )
        return "video"
    # auto: use video only when the deck actually has something moving and PowerPoint is here.
    if not SV.powerpoint_available():
        return "stills"
    return "video" if project.state.has_motion else "stills"


@timed("export")
def stage_export(
    project: Project,
    progress: ProgressFn = _noop,
    cancel: CancelToken | None = None,
    output: Path | None = None,
) -> ExportResult:
    cancel = cancel or CancelToken()
    project.ensure_dirs()
    included = project.state.included_slides()
    if not included:
        raise ProjectError("No slides are included in the export.")
    _verify_audio_freshness(project, included)
    missing = [
        s.number
        for s in included
        if s.has_narration and not (s.audio_path and Path(s.audio_path).is_file())
    ]
    if missing:
        raise ProjectError(
            f"{len(missing)} slide(s) still need narration audio (e.g. slide {missing[0]}). "
            "Run synthesis first."
        )

    exp = project.settings.export
    rnd = project.settings.render
    ffmpeg = F.find_ffmpeg(exp.ffmpeg_path)

    progress(Progress("export", 0, 100, "Checking encoders"))
    available = F.probe_encoders(ffmpeg)
    choice = F.select_encoder(
        exp.video_codec, exp.encoder_mode, available, lambda e: F.nvenc_smoke_test(ffmpeg, e)
    )
    log.info("Using %s (%s)", choice.name, choice.reason)
    cancel.check()

    source = resolve_slide_source(project)
    clip_set = None
    clip_info: dict[int, F.MediaInfo] = {}
    media_slides: set[int] = set()
    asked_holds: dict[int, float] = {}
    if source == "video":
        progress(Progress("export", 3, 100, "Rendering slides with animations (PowerPoint)"))
        from slidecast.core.deck import slides_with_media

        media_slides = slides_with_media(project.deck_path)
        asked_holds = clip_holds(project)[0]
        clip_set = stage_slide_clips(project, progress, cancel)
        cancel.check()
        # One ffprobe per clip; independent subprocesses, so probe them concurrently.
        # Each probe is itself time-bounded inside proc.run.
        probes = [(s.index, clip_set.clips[s.number]) for s in included]
        with ThreadPoolExecutor(max_workers=min(4, len(probes))) as pool:
            infos = list(pool.map(lambda c: F.probe_media(ffmpeg, c[1]), probes))
        clip_info = dict(zip((c[0] for c in probes), infos, strict=True))
        render = None
    else:
        progress(Progress("export", 3, 100, "Rendering slides"))
        render = cached_render(project) or stage_render(project, progress, cancel)
    cancel.check()

    progress(Progress("export", 10, 100, "Building narration track"))
    sr = 24000
    # A slide with embedded media stays on screen at least as long as its own clip, so the video
    # always finishes; narration shorter than that is padded with silence. Every other slide was
    # rendered to fit its narration, so the two already agree.
    plan = (
        plan_slide_layout(project, included, clip_info, media_slides, asked_holds)
        if clip_set
        else None
    )
    minimum = [item.total for item in plan] if plan else None
    work = project.work_dir
    work.mkdir(parents=True, exist_ok=True)
    raw_track = work / "narration_raw.wav"
    durations = write_narration_track(project, raw_track, sr, minimum)
    narration = work / "narration.wav"
    stats = A.loudness_normalize_file(
        raw_track,
        narration,
        sr,
        project.settings.audio.target_lufs,
        project.settings.audio.true_peak_db,
    )
    raw_track.unlink(missing_ok=True)
    log.info(
        "Loudness: %.1f LUFS -> %.1f LUFS (gain %+.1f dB, peak %.1f dBTP)",
        stats["input_lufs"],
        stats["output_lufs"],
        stats["gain_db"],
        stats["true_peak_db"],
    )
    cancel.check()

    if clip_set is not None:
        assert plan is not None
        return _export_from_clips(
            project,
            included,
            plan,
            clip_set,
            narration,
            output,
            ffmpeg,
            choice,
            stats,
            progress,
            cancel,
        )

    assert render is not None
    progress(Progress("export", 15, 100, "Building timeline"))
    clips = [
        SlideClip(index=s.index, image=render.pngs[s.index], audio_seconds=d)
        for s, d in zip(included, durations, strict=True)
    ]
    timeline = build_timeline(
        clips, exp.fps, project.settings.tts.inter_slide_gap, exp.transition_seconds
    )

    trans_dir = work / "transitions" / render.key
    trans_frames: dict[str, list[Path]] = {}
    for i, t in enumerate(timeline.transitions):
        cancel.check()
        progress(
            Progress(
                "export",
                15 + int(10 * i / max(len(timeline.transitions), 1)),
                100,
                f"Dissolve {i + 1}/{len(timeline.transitions)}",
            )
        )
        trans_frames[t.prefix] = blend_frames(
            t.from_image, t.to_image, trans_dir, t.prefix, t.frames
        )
    sequence = timeline.frame_sequence(trans_frames)
    concat_list = F.write_concat_list(work / "frames.ffconcat", sequence)

    if output is None:
        output = default_output_path(project)
    output.parent.mkdir(parents=True, exist_ok=True)
    part = _part_path(output)
    cmd = F.build_mux_command(
        ffmpeg,
        concat_list,
        narration,
        part,
        exp,
        choice.name,
        rnd.width,
        rnd.height,
        total_frames=timeline.total_frames,
    )

    def _ff(frac: float) -> None:
        progress(
            Progress(
                "export",
                25 + int(frac * 74),
                100,
                f"Encoding with {choice.name} ({frac * 100:.0f}%)",
            )
        )

    log.info(
        "Encoding %s (%.1f s, %d frames)",
        output.name,
        timeline.total_seconds,
        timeline.total_frames,
    )
    # Not _gate(): blocking inside ffmpeg's progress reader would stall the encoder, so a
    # pause requested mid-encode takes effect once this run finishes.
    try:
        F.run_ffmpeg(cmd, timeline.total_seconds, _ff, cancel.is_set)
        _verify_output(ffmpeg, part, timeline.total_seconds)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    atomic_replace(part, output)

    subs = write_subtitles(project, included, timeline.slide_starts, output)
    manifest = write_manifest(project, timeline, output, choice.name, stats, subs)
    project.state.last_output = str(output)
    project.state.encoder_used = choice.name
    project.save()
    size = output.stat().st_size
    progress(Progress("export", 100, 100, f"Done: {output.name}"))
    return ExportResult(output, manifest, choice.name, timeline.total_seconds, size, subs)


def _part_path(output: Path) -> Path:
    """The hidden sibling an encode writes to before the result is published atomically."""
    return output.with_name(f"{output.name}.part")


def _verify_output(ffmpeg: Path, path: Path, expected_seconds: float) -> None:
    """Refuse to publish a file ffprobe cannot read or whose length disagrees with the plan."""
    info = F.probe_media(ffmpeg, path)
    if not math.isfinite(info.duration) or info.duration <= 0:
        raise FFmpegError(f"Export produced an unreadable file: {path.name}")
    if abs(info.duration - expected_seconds) > 2.0:
        raise FFmpegError(
            f"Export is {info.duration:.1f}s but the timeline is {expected_seconds:.1f}s"
        )


def _subtitle_cues(
    project: Project, included: list[SlideState], starts: list[float]
) -> list[SUB.Cue]:
    """One cue per synthesized chunk: its offset inside the slide plus the slide's start."""
    cache = project.audio_cache
    cues: list[SUB.Cue] = []
    for pos, s in enumerate(included):
        if pos >= len(starts) or not s.audio_key or not cache.has(s.audio_key):
            continue
        try:
            chunks = cache.read_meta(s.audio_key).get("chunks")
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(chunks, list):
            continue
        for c in chunks:
            if isinstance(c, dict):
                cues.append(
                    SUB.Cue(
                        starts[pos] + float(c.get("start", 0.0)),
                        starts[pos] + float(c.get("end", 0.0)),
                        str(c.get("text", "")),
                    )
                )
    return cues


def write_subtitles(
    project: Project, included: list[SlideState], starts: list[float], output: Path
) -> list[Path]:
    """Write ``<output>.srt`` and ``<output>.vtt`` next to the video."""
    cues = _subtitle_cues(project, included, starts)
    return [
        SUB.write(output.with_suffix(".srt"), cues, "srt"),
        SUB.write(output.with_suffix(".vtt"), cues, "vtt"),
    ]


def write_manifest(
    project: Project,
    timeline: Timeline,
    output: Path,
    encoder: str,
    loudness: dict[str, float],
    subtitles: list[Path] | None = None,
) -> Path:
    included = project.state.included_slides()
    cache = project.audio_cache
    slides = []
    for pos, s in enumerate(included):
        entry: dict[str, object] = {
            "slide": s.number,
            "title": s.title,
            "included": True,
            "text": prepare(s.effective_notes, project.settings.text).cleaned,
            "audio": s.audio_path,
            "audio_key": s.audio_key,
            "duration": s.duration,
            "start": timeline.slide_starts[pos] if pos < len(timeline.slide_starts) else None,
            "end": timeline.slide_ends[pos] if pos < len(timeline.slide_ends) else None,
            "chunks": None,
        }
        if s.audio_key and cache.has(s.audio_key):
            meta = cache.read_meta(s.audio_key)
            entry["chunks"] = meta.get("chunks")
            entry["params_hash"] = s.audio_key
        slides.append(entry)
    doc = {
        "deck": str(project.deck_path),
        "deck_sha256": project.state.deck_sha256,
        "output": str(output),
        "encoder": encoder,
        "fps": project.settings.export.fps,
        "resolution": [project.settings.render.width, project.settings.render.height],
        "total_seconds": timeline.total_seconds,
        "loudness": loudness,
        "subtitles": [str(p) for p in subtitles or []],
        "settings": project.settings.model_dump(mode="json"),
        "slides": slides,
    }
    path = output.with_suffix(".manifest.json")
    atomic_write_json(path, doc)
    return path


def run_all(
    project: Project,
    backend: TTSBackend,
    progress: ProgressFn = _noop,
    cancel: CancelToken | None = None,
    output: Path | None = None,
) -> ExportResult:
    cancel = cancel or CancelToken()
    if resolve_slide_source(project) == "video":
        # Slide clips are rendered inside the export, not here: a clip is as long as the
        # slide's own narration, so it cannot be made until that narration exists.
        stage_synthesize(project, backend, progress, cancel)
    else:
        # Render (LibreOffice, CPU) and synthesis (GPU) share nothing: run them together so
        # the deck rasterizes while the voice is being generated. Both watch the same cancel
        # token and serialize their own project.json writes through Project.save's lock.
        with ThreadPoolExecutor(max_workers=2) as pool:
            render_done = pool.submit(stage_render, project, progress, cancel)
            try:
                stage_synthesize(project, backend, progress, cancel)
            except BaseException:
                cancel.cancel()
                # Wait for the render to unwind on the cancelled token, then re-raise the
                # synthesis error as the primary failure.
                with contextlib.suppress(Exception):
                    render_done.result()
                raise
            render_done.result()
    failed = [s.number for s in project.state.included_slides() if s.status == SlideStatus.ERROR]
    if failed:
        raise ProjectError(f"Slides failed to synthesize: {failed}. Fix and re-run synthesis.")
    return stage_export(project, progress, cancel, output)


# ---------------------------------------------------------------------------------------------
# Video-clip slide source (animations, transitions and embedded media preserved)
# ---------------------------------------------------------------------------------------------


def cached_clips(project: Project) -> ClipSet | None:
    """Whatever clips are already on disk for this deck, without invoking PowerPoint.

    A clip's filename carries the hold it was rendered at, so this finds a slide's clip without
    knowing how long the narration currently is - which is what thumbnails and previews need.
    """
    from slidecast.core import slidevideo as SV

    r, e = project.settings.render, project.settings.export
    key = SV.clip_key(project.state.deck_sha256, r.width, r.height, e.fps)
    directory = project.clips_dir / key
    if not directory.is_dir():
        return None
    clips: dict[int, Path] = {}
    for path in sorted(directory.glob("slide_*.mp4")):
        try:
            n = int(path.name.split("_")[1])
        except (IndexError, ValueError):
            continue
        clips.setdefault(n, path)
    if len(clips) != len(project.state.slides):
        return None
    return SV.ClipSet(directory, clips, True, key)


def stage_slide_clips(
    project: Project, progress: ProgressFn = _noop, cancel: CancelToken | None = None
) -> Any:
    """Render (or reuse) one video clip per slide via PowerPoint."""
    from slidecast.core import slidevideo as SV

    cancel = cancel or CancelToken()
    project.ensure_dirs()
    existing = cached_clips(project)
    if existing is not None:
        return existing

    def _p(cur: int, tot: int, msg: str) -> None:
        progress(Progress("render", cur, tot, msg))

    holds, natural = clip_holds(project)
    r, e = project.settings.render, project.settings.export
    result = SV.export_slide_clips(
        project.deck_path,
        project.state.deck_sha256,
        project.clips_dir,
        r.width,
        r.height,
        e.fps,
        slide_count=len(project.state.slides),
        holds=holds,
        natural=natural,
        progress=_p,
        cancel=_gate(cancel),
    )
    project.save()
    return result


def clip_holds(project: Project) -> tuple[dict[int, float], set[int]]:
    """How long each slide is on screen, and which slides set their own length.

    A slide's clip is rendered for exactly as long as its narration, so PowerPoint decides how
    the slide's animations fill that time instead of us repeating a short clip. Slides that
    embed audio or video keep the deck's own length: their media plays out after the narration.
    """
    from slidecast.core.deck import slides_with_media

    natural = slides_with_media(project.deck_path)
    hold = project.settings.tts.empty_slide_hold
    gap = project.settings.tts.inter_slide_gap
    holds: dict[int, float] = {}
    guessed: list[int] = []
    for slide in project.state.included_slides():
        if slide.duration:
            seconds = float(slide.duration)
        elif not slide.has_narration:
            seconds = hold
        else:
            # Narration has not been synthesized yet: render to the estimate so there is
            # something to look at, and accept that this clip is remade once the audio exists.
            seconds = max(slide.estimated_seconds, hold)
            guessed.append(slide.number)
        holds[slide.number] = seconds + gap
    if guessed:
        log.info(
            "Slides %s have no narration yet; their clips use the estimated length and will be "
            "rendered again after synthesis.",
            ", ".join(str(n) for n in guessed[:8]) + ("..." if len(guessed) > 8 else ""),
        )
    return holds, natural


@dataclass(frozen=True)
class SlideLayout:
    """How one slide is laid out on the timeline when its slides are video clips."""

    index: int  # 0-based position among the included slides
    narration: float  # seconds of narration (or the silent hold for a slide with no notes)
    clip: float  # length of the slide's own clip
    sequential: bool  # narration first, then the video plays with its sound
    loop: bool = False  # repeat a silent video that is shorter than the narration

    @property
    def total(self) -> float:
        """On-screen seconds, before the inter-slide gap."""
        return self.narration + self.clip if self.sequential else max(self.narration, self.clip)


# PowerPoint returns a clip a little longer than the hold it was given - the opening transition
# sits on top. A clip longer than this much past its hold is playing media, not padding.
_CLIP_OVERRUN = 2.0


def plan_slide_layout(
    project: Project,
    included: list[SlideState],
    clip_info: dict[int, Any],
    media: set[int] | None = None,
    holds: dict[int, float] | None = None,
) -> list[SlideLayout]:
    """Decide, per slide, whether the narration and the slide's video overlap or follow on.

    An ordinary slide's clip was rendered to the length of its own narration, so the two simply
    run together and the clip is trimmed to fit. A video with a soundtrack instead waits: the
    narration finishes over the video's opening frame, then the video plays in full with its own
    audio, so the two are never audible at once.

    A slide counts as carrying media when the deck says so, *or* when the clip that came back
    says so - it has a soundtrack, or it outran the hold it was given because PowerPoint played
    something to the end. Trusting the clip means a video still plays in full even when the deck
    scan missed it, which it can: media can be linked from outside the deck rather than embedded.
    """
    after = project.settings.render.media_after_narration
    hold = project.settings.tts.empty_slide_hold
    media = media if media is not None else set()
    holds = holds or {}
    layout: list[SlideLayout] = []
    for i, slide in enumerate(included):
        info = clip_info[slide.index]
        narration = slide.duration if slide.duration else (0.0 if slide.has_narration else hold)
        asked = holds.get(slide.number)
        overran = asked is not None and float(info.duration) > asked + _CLIP_OVERRUN
        if slide.number not in media and not info.has_audio and not overran:
            # Rendered to fit. Taking the clip's own length instead would pad every slide by the
            # opening transition PowerPoint adds on top of the hold, and with it the narration.
            layout.append(SlideLayout(i, narration, narration, False))
            continue
        if overran and asked is not None and slide.number not in media:
            log.info(
                "Slide %d ran %.1f s past its %.1f s hold, so it is playing media the deck scan "
                "did not report; keeping the clip's own length.",
                slide.number,
                float(info.duration) - asked,
                asked,
            )
        sequential = bool(after and info.has_audio and narration > 0.0)
        # A silent video is an animation, so it should run for as long as the slide is up. It
        # cannot be left to PowerPoint: CreateVideo plays embedded media exactly once and then
        # holds the last frame, even when the deck says loop and COM reports LoopUntilStopped is
        # already on (verified on a 47 s clip under a 140 s hold - 95 s of it frozen).
        repeat = not info.has_audio and narration > float(info.duration) + 1.0
        layout.append(SlideLayout(i, narration, float(info.duration), sequential, repeat))
    return layout


def _export_from_clips(
    project: Project,
    included: list[SlideState],
    layout: list[SlideLayout],
    clip_set: Any,
    narration: Path,
    output: Path | None,
    ffmpeg: Path,
    choice: F.EncoderChoice,
    stats: dict[str, float],
    progress: ProgressFn,
    cancel: CancelToken,
) -> ExportResult:
    """Concatenate per-slide clips, each held for its slide's full duration."""
    exp = project.settings.export
    rnd = project.settings.render
    gap = project.settings.tts.inter_slide_gap
    if output is None:
        output = default_output_path(project)
    output.parent.mkdir(parents=True, exist_ok=True)
    part = _part_path(output)

    posters = Path(clip_set.directory) / "posters"
    segments: list[F.ClipSegment] = []
    starts: list[float] = []
    ends: list[float] = []
    cursor = 0.0
    sequential_slides: list[int] = []
    looped: list[int] = []
    for i, (slide, item) in enumerate(zip(included, layout, strict=True)):
        clip = clip_set.clips[slide.number]
        repeat = item.loop
        if repeat:
            # Repeat only what follows the slide's opening transition, so a lap never replays the
            # fade from black. Cached beside the clip, so a re-export costs nothing.
            start = F.first_visible_time(ffmpeg, clip)
            body = item.clip - start
            if body > 0.5:
                clip = F.extract_segment(
                    ffmpeg,
                    clip,
                    Path(clip_set.directory) / "loops" / f"slide_{slide.number:03d}.mp4",
                    start,
                    body,
                )
                looped.append(slide.number)
            else:
                repeat = False
        tail = gap if i < len(included) - 1 else 0.0
        starts.append(cursor)
        ends.append(cursor + item.narration)
        if item.sequential:
            sequential_slides.append(slide.number)
            # A still of the slide - taken after its transition has faded in, so never the black
            # opening frame - holds while the narration plays...
            poster = F.extract_poster(ffmpeg, clip, posters / f"slide_{slide.number:03d}.png")
            segments.append(F.ClipSegment(poster, item.narration, is_image=True))
            # ...then the video plays in full, with its own soundtrack.
            segments.append(F.ClipSegment(clip, item.clip + tail, use_clip_audio=True))
        else:
            segments.append(
                F.ClipSegment(clip, item.total + tail, use_clip_audio=False, loop=repeat)
            )
        cursor += item.total + tail
    total = cursor
    if looped:
        log.info(
            "Slides %s hold a silent video shorter than the narration; it repeats to fill them.",
            ", ".join(str(n) for n in looped),
        )
    if sequential_slides:
        log.info(
            "Slides %s have a video with sound: narration plays first, then the video.",
            ", ".join(str(n) for n in sequential_slides),
        )

    cancel.check()
    cmd = F.build_clip_mux_command(
        ffmpeg,
        segments,
        narration,
        part,
        exp,
        choice.name,
        rnd.width,
        rnd.height,
        media_volume=rnd.media_volume,
    )

    def _ff(frac: float) -> None:
        progress(
            Progress(
                "export",
                25 + int(frac * 74),
                100,
                f"Encoding with {choice.name} ({frac * 100:.0f}%)",
            )
        )

    log.info(
        "Encoding %s from %d slide clips (%.1f s, animations and media preserved)",
        output.name,
        len(included),
        total,
    )
    try:
        F.run_ffmpeg(cmd, total, _ff, cancel.is_set)
        _verify_output(ffmpeg, part, total)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    atomic_replace(part, output)

    timeline = Timeline(fps=exp.fps)
    timeline.slide_starts = starts
    timeline.slide_ends = ends
    timeline.total_frames = round(total * exp.fps)
    subs = write_subtitles(project, included, starts, output)
    manifest = write_manifest(project, timeline, output, choice.name, stats, subs)
    project.state.last_output = str(output)
    project.state.encoder_used = choice.name
    project.save()
    progress(Progress("export", 100, 100, f"Done: {output.name}"))
    return ExportResult(output, manifest, choice.name, total, output.stat().st_size, subs)


@dataclass(frozen=True)
class ThumbSource:
    """Where the UI can get a thumbnail for one slide, whatever the slide source is."""

    index: int  # 0-based
    image: Path  # final PNG (already present for the stills path)
    clip: Path | None = None  # extract from this clip when the PNG is missing


def preview_sources(project: Project) -> Path:
    """Where low-res previews of the raw deck live until a real render replaces them."""
    return project.work_dir / "previews"


def thumbnail_sources(project: Project) -> list[ThumbSource]:
    """One thumbnail source per slide, matching whichever slide source this project uses.

    Clips win when the project renders video, so the thumbnail shows what the viewer will see
    (a frame of the playing animation) rather than a stale flattened still.
    """
    clip_set = cached_clips(project) if project.settings.render.slide_source != "stills" else None
    if clip_set is None:
        render = cached_render(project)
        if render is None:
            return []
        return [ThumbSource(i, p) for i, p in enumerate(render.pngs)]
    thumbs = Path(clip_set.directory) / "thumbs"
    out: list[ThumbSource] = []
    for i in range(len(project.state.slides)):
        clip = clip_set.clips.get(i + 1)
        if clip is None:
            continue
        out.append(ThumbSource(i, thumbs / f"slide_{i + 1:03d}.png", clip))
    return out
