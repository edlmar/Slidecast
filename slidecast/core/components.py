"""What a project is built from, and how to throw individual pieces away.

Everything Slidecast produces is cached, which is what makes a 90-minute lecture cheap to re-run.
The flip side is that when one piece is wrong - the voice changed, PowerPoint rendered a slide
oddly, an encode looks soft - you want to rebuild *that piece* without paying for the rest. This
module names the pieces, reports what each currently costs on disk, and deletes them safely.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from slidecast.core.logging_setup import get_logger
from slidecast.core.project import Project, SlideStatus

log = get_logger("components")

# Ordered as the pipeline runs, which is also the order the UI shows them in.
COMPONENT_KEYS = ("renders", "clips", "audio", "work", "output")


@dataclass(frozen=True)
class Component:
    key: str
    label: str
    description: str
    items: int
    bytes: int
    present: bool
    rebuildable: bool  # Slidecast can regenerate it on demand

    @property
    def size_mb(self) -> float:
        return self.bytes / 1e6


def _measure(paths: Iterable[Path]) -> tuple[int, int]:
    items = 0
    total = 0
    for p in paths:
        if p.is_file():
            items += 1
            total += p.stat().st_size
    return items, total


def _tree(directory: Path, pattern: str = "*") -> tuple[int, int]:
    if not directory.is_dir():
        return 0, 0
    return _measure(directory.rglob(pattern))


def survey(project: Project) -> list[Component]:
    """Current state of every rebuildable piece of the project."""
    renders_n, renders_b = _tree(project.renders_dir, "*.png")
    clips_n, clips_b = _tree(project.clips_dir, "*.mp4")
    audio_n, audio_b = _measure(project.audio_dir.glob("*.wav"))
    work_n, work_b = _tree(project.work_dir)
    outputs = (
        [p for p in project.output_dir.glob("*") if p.is_file()]
        if project.output_dir.is_dir()
        else []
    )
    out_n, out_b = _measure(outputs)

    narrated = sum(
        1
        for s in project.state.included_slides()
        if s.has_narration and s.status in (SlideStatus.DONE, SlideStatus.CACHED)
    )
    needs = sum(1 for s in project.state.included_slides() if s.has_narration)

    return [
        Component(
            "renders",
            "Slide images",
            f"{renders_n} PNG page renders (LibreOffice). Used when the deck has no animations.",
            renders_n,
            renders_b,
            renders_n > 0,
            True,
        ),
        Component(
            "clips",
            "Slide videos",
            f"{clips_n} per-slide clips (PowerPoint). Keep animations, transitions and media.",
            clips_n,
            clips_b,
            clips_n > 0,
            True,
        ),
        Component(
            "audio",
            "Narration",
            f"{narrated}/{needs} slides narrated in your cloned voice.",
            audio_n,
            audio_b,
            audio_n > 0,
            True,
        ),
        Component(
            "work",
            "Scratch files",
            "Mixed narration track, posters and frame lists. Rebuilt on every export.",
            work_n,
            work_b,
            work_n > 0,
            True,
        ),
        Component(
            "output",
            "Finished video",
            "The exported video and its manifest.",
            out_n,
            out_b,
            out_n > 0,
            True,
        ),
    ]


def clear(project: Project, key: str) -> int:
    """Delete one component's files. Returns bytes freed.

    Clearing narration also resets the affected slides to pending, so the UI and the next run
    agree about what still has to be made.
    """
    if key not in COMPONENT_KEYS:
        raise ValueError(f"Unknown component: {key}")
    before = {c.key: c.bytes for c in survey(project)}

    if key == "renders":
        _wipe(project.renders_dir)
        project.state.render_key = None
    elif key == "clips":
        _wipe(project.clips_dir)
    elif key == "audio":
        _wipe(project.audio_dir)
        for slide in project.state.slides:
            if slide.include:
                slide.reset_audio()
    elif key == "work":
        _wipe(project.work_dir)
    elif key == "output":
        _wipe(project.output_dir)
        project.state.last_output = None

    project.ensure_dirs()
    project.save()
    freed = before.get(key, 0)
    log.info("Cleared %s (%.1f MB freed)", key, freed / 1e6)
    return freed


def _wipe(directory: Path) -> None:
    if directory.is_dir():
        shutil.rmtree(directory, ignore_errors=True)


def total_bytes(project: Project) -> int:
    return sum(c.bytes for c in survey(project))


def survey_many(project_dirs: Iterable[Path]) -> list[Component]:
    """Aggregate the components of several projects, for a folder of decks."""
    totals: dict[str, list[int]] = {k: [0, 0, 0] for k in COMPONENT_KEYS}  # items, bytes, decks
    decks = 0
    for directory in project_dirs:
        if not Project.exists(directory):
            continue
        try:
            project = Project.load(directory)
        except Exception as exc:  # a broken project must not hide the rest
            log.warning("Skipping %s in survey: %s", directory, exc)
            continue
        decks += 1
        for component in survey(project):
            row = totals[component.key]
            row[0] += component.items
            row[1] += component.bytes
            row[2] += 1 if component.present else 0

    labels = {
        "renders": ("Slide images", "PNG page renders (LibreOffice)."),
        "clips": ("Slide videos", "Per-slide clips (PowerPoint) with animations and media."),
        "audio": ("Narration", "Synthesized speech in your cloned voice."),
        "work": ("Scratch files", "Mixed tracks, posters and frame lists."),
        "output": ("Finished video", "Exported videos and their manifests."),
    }
    out: list[Component] = []
    for key in COMPONENT_KEYS:
        items, size, present_in = totals[key]
        label, blurb = labels[key]
        detail = f"{blurb} {items} file(s) across {present_in} of {decks} deck(s)."
        out.append(Component(key, label, detail, items, size, size > 0, decks > 0))
    return out


def clear_many(project_dirs: Iterable[Path], key: str) -> int:
    """Clear one component from every project in a folder. Returns bytes freed."""
    freed = 0
    for directory in project_dirs:
        if not Project.exists(directory):
            continue
        try:
            freed += clear(Project.load(directory), key)
        except Exception as exc:  # keep going: one bad project should not block the rest
            log.warning("Could not clear %s in %s: %s", key, directory, exc)
    return freed
