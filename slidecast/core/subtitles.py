"""Subtitles: one cue per synthesized chunk, timed against the narration track.

The per-slide cache metadata already stores each chunk's text and its offset inside that
slide's audio, so cues are just those offsets shifted by the slide's start on the timeline.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from slidecast.core.cache import atomic_write_text


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


def _hms(t: float, sep: str) -> str:
    t = max(t, 0.0)
    ms = round(t * 1000.0)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def format_srt(cues: list[Cue]) -> str:
    blocks = []
    for i, c in enumerate(cues, 1):
        blocks.append(f"{i}\n{_hms(c.start, ',')} --> {_hms(c.end, ',')}\n{c.text}")
    return "\n\n".join(blocks) + "\n" if blocks else ""


def format_vtt(cues: list[Cue]) -> str:
    blocks = ["WEBVTT"]
    for c in cues:
        # "-->" inside cue text would break the cue line; it cannot appear in cleaned text,
        # but guard anyway because cue text ultimately comes from user notes.
        text = c.text.replace("-->", "->")
        blocks.append(f"{_hms(c.start, '.')} --> {_hms(c.end, '.')}\n{text}")
    return "\n\n".join(blocks) + "\n"


def sane_cues(cues: list[Cue]) -> list[Cue]:
    """Drop empty/degenerate cues, clamp ends after starts, keep display order."""
    out: list[Cue] = []
    for c in cues:
        text = c.text.strip()
        if not text or not (math.isfinite(c.start) and math.isfinite(c.end)):
            continue
        out.append(Cue(c.start, max(c.end, c.start + 0.001), text))
    out.sort(key=lambda c: c.start)
    return out


def write(path: Path, cues: list[Cue], fmt: str) -> Path:
    """Write cues as ``srt`` or ``vtt``. The file is always written, even when empty."""
    ordered = sane_cues(cues)
    text = format_srt(ordered) if fmt == "srt" else format_vtt(ordered)
    atomic_write_text(path, text)
    return path
