"""Project folder + ``project.json`` state. One project per deck.

Layout::

    <project>/
      project.json     state (this module)
      renders/<key>/   slide PNGs keyed by deck hash + resolution
      audio/           per-slide narration keyed by content hash
      work/            scratch for export (narration.wav, transitions, concat list)
      output/          final videos + manifest.json
      logs/            slidecast.log
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from slidecast.core.cache import AudioCache, atomic_write_text
from slidecast.core.config import ProjectSettings
from slidecast.core.deck import DeckInfo, parse_deck
from slidecast.core.errors import ProjectError
from slidecast.core.logging_setup import get_logger
from slidecast.core.textprep import prepare

log = get_logger("project")

STATE_FILENAME = "project.json"
STATE_VERSION = 1


def _migrate_state(data: dict[str, Any], version: int) -> dict[str, Any]:
    """Upgrade a project.json dict written at ``version`` to the current schema.

    One entry per version boundary: ``_migrate_state(data, 1)`` returns the version-2
    shape, and so on. Currently at version 1, so nothing to do - this is where future
    migrations chain. Returns a dict that ``ProjectState.model_validate`` can read.
    """
    # Example for the future:
    # if version < 2:
    #     data = {**data, "new_field": "default"}
    return data


class SlideStatus(StrEnum):
    PENDING = "pending"
    CACHED = "cached"
    GENERATING = "generating"
    DONE = "done"
    ERROR = "error"
    SKIPPED = "skipped"


class SlideState(BaseModel):
    index: int
    title: str
    slide_id: int | None = None  # PowerPoint slide id; None in states saved before it existed
    notes: str = ""
    notes_override: str | None = None
    hidden: bool = False
    include: bool = True
    status: SlideStatus = SlideStatus.PENDING
    audio_key: str | None = None
    audio_path: str | None = None
    duration: float | None = None
    chunk_count: int = 0
    word_count: int = 0
    estimated_seconds: float = 0.0
    error: str | None = None

    @property
    def number(self) -> int:
        return self.index + 1

    @property
    def effective_notes(self) -> str:
        return self.notes if self.notes_override is None else self.notes_override

    @property
    def has_narration(self) -> bool:
        return bool(self.effective_notes.strip())

    def reset_audio(self) -> None:
        self.status = SlideStatus.PENDING
        self.audio_key = None
        self.audio_path = None
        self.duration = None
        self.chunk_count = 0
        self.error = None


class DeckMotionState(BaseModel):
    animated_slides: int = 0
    transition_slides: int = 0
    video_files: int = 0
    audio_files: int = 0
    animated_images: int = 0

    @property
    def any_motion(self) -> bool:
        return bool(
            self.animated_slides
            or self.transition_slides
            or self.video_files
            or self.animated_images
        )


def _motion_state(info: DeckInfo) -> DeckMotionState:
    """What the deck does that a flat PNG cannot show."""
    return DeckMotionState(
        animated_slides=info.motion.animated_slides,
        transition_slides=info.motion.transition_slides,
        video_files=info.motion.video_files,
        audio_files=info.motion.audio_files,
        animated_images=info.motion.animated_images,
    )


class ProjectState(BaseModel):
    version: int = STATE_VERSION
    deck_path: Path
    deck_sha256: str
    # Hash of only the deck parts that appear on slides (notes and metadata excluded):
    # what the render cache is keyed on. Empty in states written before it existed.
    deck_visual_sha256: str = ""
    motion: DeckMotionState = Field(default_factory=DeckMotionState)
    settings: ProjectSettings = Field(default_factory=ProjectSettings)
    slides: list[SlideState] = Field(default_factory=list)
    render_key: str | None = None
    last_output: str | None = None
    encoder_used: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    def included_slides(self) -> list[SlideState]:
        return [s for s in self.slides if s.include]

    @property
    def has_motion(self) -> bool:
        """True when the deck has animations, transitions or embedded video to preserve."""
        return self.motion.any_motion


class Project:
    """A project directory with load/save helpers."""

    def __init__(self, directory: Path, state: ProjectState) -> None:
        self.directory = Path(directory)
        self.state = state
        # Save serialisation: the pipeline worker saves mid-task while the GUI thread may save
        # too (settings edits, include flags). The lock keeps project.json from ever being torn
        # between the two writers; the dump sits inside it so a save is a consistent snapshot.
        self._save_lock = threading.Lock()

    # -- paths --------------------------------------------------------------------------

    @property
    def state_file(self) -> Path:
        return self.directory / STATE_FILENAME

    @property
    def renders_dir(self) -> Path:
        return self.directory / "renders"

    @property
    def audio_dir(self) -> Path:
        return self.directory / "audio"

    @property
    def work_dir(self) -> Path:
        return self.directory / "work"

    @property
    def output_dir(self) -> Path:
        return self.directory / "output"

    @property
    def logs_dir(self) -> Path:
        return self.directory / "logs"

    @property
    def log_file(self) -> Path:
        return self.logs_dir / "slidecast.log"

    @property
    def clips_dir(self) -> Path:
        return self.directory / "clips"

    @property
    def audio_cache(self) -> AudioCache:
        return AudioCache(self.audio_dir)

    @property
    def settings(self) -> ProjectSettings:
        return self.state.settings

    @property
    def deck_path(self) -> Path:
        return Path(self.state.deck_path)

    def ensure_dirs(self) -> None:
        for d in (
            self.renders_dir,
            self.clips_dir,
            self.audio_dir,
            self.work_dir,
            self.output_dir,
            self.logs_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    # -- persistence ----------------------------------------------------------------------

    def save(self) -> None:
        self.state.updated_at = datetime.now(UTC).isoformat()
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._save_lock:
            atomic_write_text(self.state_file, self.state.model_dump_json(indent=2))

    @classmethod
    def load(cls, directory: Path) -> Project:
        directory = Path(directory)
        f = directory / STATE_FILENAME
        if not f.is_file():
            raise ProjectError(f"No {STATE_FILENAME} in {directory}")
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ProjectError(f"Could not read {f}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ProjectError(f"Corrupt project state in {f}: {exc}") from exc
        version = data.get("version") if isinstance(data, dict) else None
        if not isinstance(version, int):
            raise ProjectError(f"Corrupt project state in {f}: missing or invalid version")
        if version > STATE_VERSION:
            raise ProjectError(
                f"{f} was written by a newer Slidecast (state version {version} > "
                f"{STATE_VERSION}). Upgrade the application instead of opening this project."
            )
        data = _migrate_state(data, version)
        try:
            state = ProjectState.model_validate(data)
        except ValidationError as exc:
            raise ProjectError(f"Corrupt project state in {f}: {exc}") from exc
        project = cls(directory, state)
        # Any partial audio left by a crash or hard kill is discarded.
        removed = project.audio_cache.cleanup_partials()
        if removed:
            log.info("Removed %d partial audio file(s) from a previous run", removed)
        for s in state.slides:
            if s.status == SlideStatus.GENERATING:
                s.reset_audio()
        return project

    @classmethod
    def exists(cls, directory: Path) -> bool:
        return (Path(directory) / STATE_FILENAME).is_file()

    # -- creation / deck sync ------------------------------------------------------------

    @classmethod
    def create(
        cls, directory: Path, deck: Path, settings: ProjectSettings | None = None
    ) -> Project:
        settings = settings or ProjectSettings()
        info = parse_deck(Path(deck))
        state = ProjectState(
            deck_path=info.path.resolve(),
            deck_sha256=info.sha256,
            deck_visual_sha256=info.visual_sha256,
            settings=settings,
        )
        project = cls(Path(directory), state)
        project._apply_deck(info, initial=True)
        project.ensure_dirs()
        project.save()
        return project

    def refresh_deck(self) -> bool:
        """Re-parse the deck. Returns True if it changed. Notes overrides are preserved."""
        info = parse_deck(self.deck_path)
        if info.sha256 == self.state.deck_sha256 and len(info.slides) == len(self.state.slides):
            # The deck has not changed, but what we recorded *about* it can still be out of
            # date: a project written by an older build has no count of animated images, and
            # motion is what decides whether slides render as video or as flat stills. Refresh
            # it even on an unchanged deck - parse_deck has already done the work. The same
            # goes for the visual hash that the render cache is keyed on.
            dirty = False
            motion = _motion_state(info)
            if motion != self.state.motion:
                log.info("Deck motion re-detected: %s", info.motion.describe())
                self.state.motion = motion
                dirty = True
            if not self.state.deck_visual_sha256:
                self.state.deck_visual_sha256 = info.visual_sha256
                dirty = True
            if dirty:
                self.save()
            return False
        self._apply_deck(info, initial=False)
        self.state.deck_sha256 = info.sha256
        # Renders are keyed on the slide visuals only: a notes edit rewrites the deck file
        # but leaves the images untouched, so they stay cached. Any other change drops them.
        if info.visual_sha256 != self.state.deck_visual_sha256:
            self.state.render_key = None
            self.state.deck_visual_sha256 = info.visual_sha256
        self.save()
        return True

    def _apply_deck(self, info: DeckInfo, initial: bool) -> None:
        self.state.motion = _motion_state(info)
        # Identity is the PowerPoint slide id: it follows the slide when the deck is reordered,
        # so overrides and finished audio move with their slide instead of staying glued to a
        # position. States written before ids existed carry none and match by position - all
        # they can do.
        old_by_id = {s.slide_id: s for s in self.state.slides if s.slide_id is not None}
        old_by_pos = {s.index: s for s in self.state.slides if s.slide_id is None}
        skip_hidden = self.settings.skip_hidden_slides
        slides: list[SlideState] = []
        for rec in info.slides:
            prev = old_by_id.get(rec.slide_id) if rec.slide_id is not None else None
            if prev is None:
                prev = old_by_pos.get(rec.index)
            prepared = prepare(rec.notes, self.settings.text)
            s = SlideState(
                index=rec.index,
                title=rec.title,
                slide_id=rec.slide_id,
                notes=rec.notes,
                hidden=rec.hidden,
                include=(not rec.hidden or not skip_hidden)
                if initial or prev is None
                else prev.include,
                word_count=prepared.word_count,
                estimated_seconds=prepared.estimated_seconds,
            )
            if prev is not None and not initial:
                s.notes_override = prev.notes_override
                s.status = prev.status
                s.audio_key = prev.audio_key
                s.audio_path = prev.audio_path
                s.duration = prev.duration
                s.chunk_count = prev.chunk_count
                if prev.notes != rec.notes and prev.notes_override is None:
                    s.reset_audio()  # text changed -> key changes -> regenerate
                if s.effective_notes.strip():
                    p2 = prepare(s.effective_notes, self.settings.text)
                    s.word_count, s.estimated_seconds = p2.word_count, p2.estimated_seconds
            if rec.hidden and skip_hidden and (initial or prev is None):
                s.status = SlideStatus.SKIPPED
            slides.append(s)
        self.state.slides = slides

    # -- editing helpers ------------------------------------------------------------------

    def set_notes_override(self, index: int, text: str | None) -> None:
        s = self.state.slides[index]
        new_text = None if (text is None or text == s.notes) else text
        if new_text == s.notes_override:
            return
        s.notes_override = new_text
        prepared = prepare(s.effective_notes, self.settings.text)
        s.word_count, s.estimated_seconds = prepared.word_count, prepared.estimated_seconds
        s.reset_audio()
        self.save()

    def set_include(self, index: int, include: bool) -> None:
        s = self.state.slides[index]
        s.include = include
        if not include:
            s.status = SlideStatus.SKIPPED
        elif s.status == SlideStatus.SKIPPED:
            s.status = SlideStatus.PENDING
        self.save()
