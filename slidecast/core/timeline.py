"""Frame-accurate timeline: slide holds, inter-slide gaps and cross-dissolves.

The audio track is ``a_1 + gap + a_2 + gap + ... + a_n``.  Slide *i* is displayed for its audio
duration plus the following gap.  A dissolve of ``T`` seconds straddles each boundary, taking
``ceil(n/2)`` frames from the outgoing slide and ``floor(n/2)`` from the incoming one, so the
total length is unchanged and audio stays in sync.  All durations are quantised to whole frames
using cumulative rounding so drift never accumulates.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SlideClip:
    index: int
    image: Path
    audio_seconds: float


@dataclass(frozen=True)
class FrameSpec:
    image: Path
    frames: int

    def seconds(self, fps: int) -> float:
        return self.frames / fps


@dataclass(frozen=True)
class TransitionSpec:
    from_index: int
    to_index: int
    from_image: Path
    to_image: Path
    frames: int
    prefix: str


@dataclass
class Timeline:
    fps: int
    holds: list[FrameSpec] = field(default_factory=list)
    transitions: list[TransitionSpec] = field(default_factory=list)
    slide_starts: list[float] = field(default_factory=list)
    slide_ends: list[float] = field(default_factory=list)
    total_frames: int = 0

    @property
    def total_seconds(self) -> float:
        return self.total_frames / self.fps

    def frame_sequence(self, transition_frames: dict[str, list[Path]]) -> list[tuple[Path, float]]:
        """Expand holds + rendered dissolve frames into (image, seconds) pairs in display order."""
        seq: list[tuple[Path, float]] = []
        by_from = {t.from_index: t for t in self.transitions}
        for i, hold in enumerate(self.holds):
            seq.append((hold.image, hold.frames / self.fps))
            t = by_from.get(i)
            if t is not None:
                for img in transition_frames[t.prefix]:
                    seq.append((img, 1.0 / self.fps))
        return seq


def _quantize_boundaries(durations: list[float], fps: int) -> list[int]:
    """Cumulative-rounded frame boundaries so sum of holds == round(total * fps)."""
    bounds: list[int] = []
    t = 0.0
    for d in durations:
        t += d
        bounds.append(round(t * fps))
    return bounds


def build_timeline(
    clips: list[SlideClip], fps: int, inter_slide_gap: float, transition_seconds: float
) -> Timeline:
    tl = Timeline(fps=fps)
    if not clips:
        return tl
    n = len(clips)
    holds_s = [
        c.audio_seconds + (inter_slide_gap if i < n - 1 else 0.0) for i, c in enumerate(clips)
    ]
    bounds = _quantize_boundaries(holds_s, fps)
    hold_frames = [bounds[0]] + [bounds[i] - bounds[i - 1] for i in range(1, n)]
    hold_frames = [max(h, 1) for h in hold_frames]

    # Audio positions (independent of the video quantisation).
    t = 0.0
    for i, c in enumerate(clips):
        tl.slide_starts.append(t)
        t += c.audio_seconds
        tl.slide_ends.append(t)
        if i < n - 1:
            t += inter_slide_gap

    want = round(transition_seconds * fps)
    taken_from_head = [0] * n
    taken_from_tail = [0] * n
    if want >= 2:
        for i in range(n - 1):
            a, b = clips[i], clips[i + 1]
            # Keep at least 2 clean frames of each slide.
            avail = min(hold_frames[i] - taken_from_head[i] - 2, hold_frames[i + 1] - 2)
            nf = min(want, max(avail, 0))
            if nf < 2:
                continue
            tail = math.ceil(nf / 2)
            head = nf - tail
            taken_from_tail[i] = tail
            taken_from_head[i + 1] = head
            tl.transitions.append(
                TransitionSpec(
                    from_index=i,
                    to_index=i + 1,
                    from_image=a.image,
                    to_image=b.image,
                    frames=nf,
                    prefix=f"xf_{a.index + 1:03d}_{b.index + 1:03d}_{nf:02d}",
                )
            )
    for i, c in enumerate(clips):
        tl.holds.append(
            FrameSpec(c.image, hold_frames[i] - taken_from_tail[i] - taken_from_head[i])
        )
    tl.total_frames = sum(h.frames for h in tl.holds) + sum(t.frames for t in tl.transitions)
    return tl


def format_duration(seconds: float) -> str:
    """``h:mm:ss`` or ``m:ss`` with tabular-friendly zero padding."""
    if seconds < 0 or not math.isfinite(seconds):
        return "--:--"
    total = round(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
