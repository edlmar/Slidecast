"""Render slides as *video* clips via PowerPoint, preserving animations, transitions and
embedded media.

The still-image path (LibreOffice -> PDF -> PNG) flattens a deck: builds, transitions and
embedded videos are lost. PowerPoint's own ``Presentation.CreateVideo`` renders them properly, so
when PowerPoint is available we export one clip per slide and compose the lecture from those.

Measured behaviour that the design relies on (verified against PowerPoint 16 on Windows 11):

* ``CreateVideo`` always plays embedded media to completion - a slide holding a 46 s video yields
  a ~47 s clip whether its slide time is set to 0.1 s, 5 s or 30 s. A slide with embedded media
  therefore has a natural length and is rendered as-is.
* Every other slide is rendered for exactly as long as its narration, so PowerPoint itself decides
  how animations fill that time. A GIF keeps looping at its authored rate for the whole slide
  (measured: 100 % of sampled frames still moving 100 s in, longest freeze 0.10 s), which is
  something no amount of repeating a short clip can reproduce - two GIFs with different cycles on
  one slide cannot both land on a seam.
* Rendering runs at ~10x realtime for long holds and ~2-3x for short ones, the difference being
  fixed start-up cost: a 100 s slide takes ~9.5 s. A 58-minute lecture is ~6 minutes of
  PowerPoint, once, then free until that slide's narration changes.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from slidecast.core.cache import retry_locked_io
from slidecast.core.errors import Cancelled, RenderError
from slidecast.core.logging_setup import get_logger

log = get_logger("slidevideo")

ProgressFn = Callable[[int, int, str], None]
CancelFn = Callable[[], bool]

# PowerPoint's CreateVideoStatus values.
_STATUS_IN_PROGRESS, _STATUS_QUEUED, _STATUS_DONE, _STATUS_FAILED = 1, 2, 3, 4
_MSO_MEDIA = 16
_MIN_SLIDE_SECONDS = 1.0
# Click-triggered builds are advanced automatically during export; give each step a beat. Only
# used for media slides, whose length comes from the deck rather than from the narration.
_SECONDS_PER_ANIMATION = 1.0
_MAX_ANIMATION_HOLD = 30.0
# Requested holds are rounded up to this step before they reach a filename, so re-recording a
# slide whose narration shifts by a fraction of a second still hits the cached clip.
_HOLD_QUANTUM = 0.5
_EXPORT_TIMEOUT = 900.0


def powerpoint_available() -> bool:
    """True if this machine can drive PowerPoint through COM."""
    if sys.platform != "win32":
        return False
    try:
        import win32com.client  # noqa: F401
    except ImportError:
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Classes\PowerPoint.Application"):
            return True
    except OSError:
        return False


def unavailable_reason() -> str:
    if sys.platform != "win32":
        return "PowerPoint automation is only available on Windows."
    try:
        import win32com.client  # noqa: F401
    except ImportError:
        return "pywin32 is not installed (pip install pywin32)."
    return "Microsoft PowerPoint is not installed on this machine."


@dataclass
class ClipSet:
    directory: Path
    clips: dict[int, Path]
    from_cache: bool
    key: str


def clip_key(deck_hash: str, width: int, height: int, fps: int) -> str:
    import hashlib

    raw = f"{deck_hash}|{width}x{height}|{fps}|pptvideo4"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def quantize_hold(seconds: float) -> float:
    """Round a required on-screen time up to the next cache step."""
    import math

    step = max(math.ceil(seconds / _HOLD_QUANTUM) * _HOLD_QUANTUM, _MIN_SLIDE_SECONDS)
    return round(step, 3)


def clip_name(n: int, hold: float | None) -> str:
    """Filename for slide ``n`` held for ``hold`` seconds (``None`` = the deck's own length).

    The hold is part of the name, so the cache is per slide: re-recording one slide's narration
    re-renders that clip alone and leaves the rest of the deck untouched.
    """
    if hold is None:
        return f"slide_{n:03d}_natural.mp4"
    return f"slide_{n:03d}_{round(quantize_hold(hold) * 1000):07d}.mp4"


def _com() -> Any:
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:  # pragma: no cover - guarded by powerpoint_available()
        raise RenderError(unavailable_reason()) from exc
    return pythoncom, win32com.client


def _close(pres: Any, app: Any) -> None:
    for obj, method in ((pres, "Close"), (app, "Quit")):
        if obj is not None:
            try:
                getattr(obj, method)()
            except Exception as exc:  # COM teardown is best effort
                log.debug("PowerPoint %s failed: %s", method, exc)


def export_slide_clips(
    deck: Path,
    deck_hash: str,
    cache_root: Path,
    width: int,
    height: int,
    fps: int,
    slide_count: int,
    holds: dict[int, float] | None = None,
    natural: set[int] | None = None,
    progress: ProgressFn | None = None,
    cancel: CancelFn | None = None,
) -> ClipSet:
    """Export (or reuse) one video clip per slide, with animations and media intact.

    ``holds`` gives the seconds each slide is on screen, so PowerPoint renders the slide for
    exactly that long and decides for itself how its animations fill the time. Slides listed in
    ``natural`` embed audio or video and are rendered at the deck's own length instead.

    Caching is per slide - the hold is part of the filename - so changing the narration on one
    slide re-renders one clip.
    """
    holds = holds or {}
    natural = natural or set()
    key = clip_key(deck_hash, width, height, fps)
    out_dir = Path(cache_root) / key
    wanted = {
        n: out_dir / clip_name(n, None if n in natural else holds.get(n, _MIN_SLIDE_SECONDS))
        for n in range(1, slide_count + 1)
    }
    todo = sorted(n for n, path in wanted.items() if not path.is_file())
    if not todo:
        log.info("Using cached slide clips (%d) from %s", len(wanted), out_dir)
        return ClipSet(out_dir, wanted, True, key)

    if not powerpoint_available():
        raise RenderError(
            "Slides with animations need PowerPoint to render. " + unavailable_reason()
        )

    deck = Path(deck).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    _sweep_scratch(out_dir)
    # A fresh scratch directory per run. A run killed mid-render leaves one behind that Windows
    # will not always let us delete - OneDrive and PowerPoint keep files open for a while after -
    # and with a fixed name every later run then died at mkdir before rendering anything.
    work = out_dir / f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True, exist_ok=True)

    total = sum(holds.get(n, 0.0) for n in todo if n not in natural)
    log.info(
        "Rendering %d of %d slide clip(s), %.0f s of slide video (~%.0f s of PowerPoint)",
        len(todo),
        slide_count,
        total,
        total / 10.0 + 2.0 * len(todo),
    )
    pythoncom, win32 = _com()
    pythoncom.CoInitialize()
    app = None
    try:
        app = win32.Dispatch("PowerPoint.Application")
        for done, n in enumerate(todo):
            if cancel and cancel():
                raise Cancelled("Slide video export cancelled")
            if progress:
                progress(done, len(todo), f"Rendering slide {n}/{slide_count} with animations")
            hold = None if n in natural else quantize_hold(holds.get(n, _MIN_SLIDE_SECONDS))
            clip = _export_one(app, deck, n, slide_count, work, width, height, fps, hold)
            retry_locked_io(
                lambda: os.replace(clip, wanted[n]),  # noqa: B023 - called before the next loop
                f"publish clip for slide {n}",
            )
            _drop_stale(out_dir, n, wanted[n])
        if progress:
            progress(len(todo), len(todo), f"Rendered {len(todo)} slide(s)")
    finally:
        _close(None, app)
        pythoncom.CoUninitialize()
        shutil.rmtree(work, ignore_errors=True)

    return ClipSet(out_dir, wanted, False, key)


def _sweep_scratch(out_dir: Path) -> None:
    """Delete scratch directories left by earlier runs, as far as Windows will allow.

    Best effort by design: one that is still locked is left alone and reported at debug level,
    because a leftover must never be able to stop this run from rendering.
    """
    for stale in out_dir.glob(".tmp*"):
        if not stale.is_dir():
            continue
        shutil.rmtree(stale, ignore_errors=True)
        if stale.exists():
            log.debug("Left scratch directory %s in place; something still has it open", stale.name)


def _drop_stale(out_dir: Path, n: int, keep: Path) -> None:
    """Delete earlier clips for slide ``n`` rendered at a hold we no longer use.

    Without this a lecture whose narration is re-recorded a few times would keep every past
    length on disk, and a slide's clip is now as long as the slide itself.
    """
    for old in out_dir.glob(f"slide_{n:03d}_*.mp4"):
        if old == keep:
            continue
        with contextlib.suppress(OSError):
            old.unlink()
            log.debug("Dropped superseded clip %s", old.name)
    thumb = out_dir / "thumbs" / f"slide_{n:03d}.png"
    with contextlib.suppress(OSError):
        thumb.unlink()


def _export_one(
    app: Any,
    deck: Path,
    n: int,
    slide_count: int,
    work: Path,
    width: int,
    height: int,
    fps: int,
    hold: float | None,
) -> Path:
    """Export slide ``n`` alone, held for ``hold`` seconds (``None`` = the deck's own length)."""
    tmp_deck = work / f"_slide_{n:03d}.pptx"
    shutil.copy(deck, tmp_deck)
    out = work / f"slide_{n:03d}.mp4"
    pres = None
    try:
        pres = app.Presentations.Open(str(tmp_deck), ReadOnly=False, WithWindow=False)
        for i in range(pres.Slides.Count, 0, -1):
            if i != n:
                pres.Slides(i).Delete()
        slide = pres.Slides(1)
        # Media set to "on click" is skipped entirely by CreateVideo (verified: a click-to-play
        # clip exported as a 1 s silent slide). Force auto-play so every video actually runs.
        muted: list[str] = []
        for shape in slide.Shapes:
            if int(shape.Type) != _MSO_MEDIA:
                continue
            with contextlib.suppress(Exception):
                shape.AnimationSettings.PlaySettings.PlayOnEntry = True
            with contextlib.suppress(Exception):
                if bool(shape.MediaFormat.Muted):
                    muted.append(str(shape.Name))
        if muted:
            log.warning(
                "Slide %d: media %s is muted in the deck, so it stays silent in the video. "
                "Unmute it in PowerPoint (Playback > Volume) if you want its sound.",
                n,
                ", ".join(repr(m) for m in muted),
            )
        if hold is None:
            # A media slide runs as long as the deck says: PowerPoint plays embedded video to
            # completion regardless of the slide timing, and builds get a beat each.
            steps = 0
            with contextlib.suppress(Exception):
                steps = int(slide.TimeLine.MainSequence.Count)
            hold = min(max(steps * _SECONDS_PER_ANIMATION, _MIN_SLIDE_SECONDS), _MAX_ANIMATION_HOLD)
        transition = slide.SlideShowTransition
        transition.AdvanceOnTime = True
        transition.AdvanceTime = hold
        transition.AdvanceOnClick = False
        if out.exists():
            out.unlink()
        started = time.perf_counter()
        pres.CreateVideo(
            str(out),
            UseTimingsAndNarrations=True,
            DefaultSlideDuration=int(hold),
            VertResolution=height,
            FramesPerSecond=fps,
            Quality=95,
        )
        while True:
            status = int(pres.CreateVideoStatus)
            if status == _STATUS_DONE:
                break
            if status == _STATUS_FAILED:
                raise RenderError(f"PowerPoint failed to render slide {n} to video.")
            if time.perf_counter() - started > _EXPORT_TIMEOUT:
                raise RenderError(f"PowerPoint timed out rendering slide {n} to video.")
            time.sleep(0.25)
    finally:
        _close(pres, None)
        with contextlib.suppress(OSError):
            tmp_deck.unlink()
    if not out.is_file():
        raise RenderError(f"PowerPoint reported success but produced no clip for slide {n}.")
    log.debug("slide %d/%d -> %s (hold=%.1fs)", n, slide_count, out.name, hold)
    return out
