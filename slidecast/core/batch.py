"""Batch mode: find every .pptx under a folder tree and run the full pipeline on each.

One TTS model load serves all decks. Each deck keeps its own project folder (next to the deck by
default, or under a chosen projects folder), so caching and resumability work exactly as for a
single deck. Outputs mirror the source tree under the chosen output folder.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from slidecast.core import pipeline as P
from slidecast.core.config import ProjectSettings
from slidecast.core.errors import Cancelled, SlidecastError
from slidecast.core.logging_setup import get_logger
from slidecast.core.project import Project
from slidecast.core.tts import TTSBackend

log = get_logger("batch")

PROJECT_SUFFIX = "_slidecast"


def find_decks(root: Path) -> list[Path]:
    """All .pptx files under ``root`` (recursive), skipping Office temp files, hidden folders
    and anything inside a slidecast project folder."""
    root = Path(root)
    found: list[Path] = []
    # Folder first, then file name, so a folder's own decks precede its subfolders' decks.
    for p in sorted(
        root.rglob("*.pptx"), key=lambda q: (q.parent.as_posix().lower(), q.name.lower())
    ):
        if p.name.startswith("~$") or not p.is_file():
            continue
        rel_parts = p.relative_to(root).parts[:-1]
        if any(part.startswith(".") or part.endswith(PROJECT_SUFFIX) for part in rel_parts):
            continue
        found.append(p)
    return found


@dataclass(frozen=True)
class DeckEntry:
    """A deck found by scanning a folder, with whatever an existing project says about it."""

    deck: Path
    project_dir: Path
    has_project: bool = False
    slides: int | None = None
    narrated: int = 0
    needs_narration: int = 0
    last_output: Path | None = None

    @property
    def status(self) -> str:
        if not self.has_project:
            return "new"
        if self.last_output and self.last_output.is_file():
            return "exported"
        if self.needs_narration and self.narrated >= self.needs_narration:
            return "narrated"
        if self.narrated:
            return f"{self.narrated}/{self.needs_narration} narrated"
        return "opened"


@dataclass(frozen=True)
class _Summary:
    slides: int
    narrated: int
    needs_narration: int
    last_output: Path | None


def _read_project_summary(project_dir: Path) -> _Summary | None:
    """Read project.json without loading/mutating the project. Tolerant of any corruption."""
    import json

    try:
        data = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    slides = data.get("slides")
    if not isinstance(slides, list):
        return None
    included = [s for s in slides if isinstance(s, dict) and s.get("include", True)]
    with_notes = [
        s for s in included if str(s.get("notes_override") or s.get("notes") or "").strip()
    ]
    out = data.get("last_output")
    return _Summary(
        slides=len(slides),
        narrated=sum(1 for s in with_notes if s.get("status") in ("done", "cached")),
        needs_narration=len(with_notes),
        last_output=Path(str(out)) if out else None,
    )


def scan_decks(root: Path, projects_dir: Path | None = None) -> list[DeckEntry]:
    """Every deck under ``root`` with the state of its project, if it has one.

    Used by the GUI's "Open folder" picker; cheap enough for large trees because it reads each
    ``project.json`` directly instead of loading the project.
    """
    root = Path(root)
    entries: list[DeckEntry] = []
    for deck in find_decks(root):
        rel_parent = deck.relative_to(root).parent
        project_dir = (
            projects_dir / rel_parent / f"{deck.stem}{PROJECT_SUFFIX}"
            if projects_dir
            else deck.with_name(f"{deck.stem}{PROJECT_SUFFIX}")
        )
        s = _read_project_summary(project_dir) if project_dir.is_dir() else None
        entries.append(
            DeckEntry(
                deck=deck,
                project_dir=project_dir,
                has_project=s is not None,
                slides=s.slides if s else None,
                narrated=s.narrated if s else 0,
                needs_narration=s.needs_narration if s else 0,
                last_output=s.last_output if s else None,
            )
        )
    return entries


@dataclass
class BatchItem:
    deck: Path
    project_dir: Path
    output: Path
    status: str = "pending"  # pending | running | done | skipped | error
    error: str | None = None
    seconds: float = 0.0


def plan_batch(
    root: Path,
    template: ProjectSettings,
    output_dir: Path | None = None,
    projects_dir: Path | None = None,
) -> list[BatchItem]:
    """Decide project folder and output path for every deck under ``root``."""
    root = Path(root)
    items: list[BatchItem] = []
    for deck in find_decks(root):
        rel_parent = deck.relative_to(root).parent
        project_dir = (
            projects_dir / rel_parent / f"{deck.stem}{PROJECT_SUFFIX}"
            if projects_dir
            else deck.with_name(f"{deck.stem}{PROJECT_SUFFIX}")
        )
        out_base = (Path(output_dir) / rel_parent) if output_dir else project_dir / "output"
        items.append(
            BatchItem(
                deck=deck,
                project_dir=project_dir,
                output=out_base / f"{deck.stem}.{template.export.container}",
            )
        )
    return items


def open_batch_project(item: BatchItem, template: ProjectSettings) -> Project:
    """Load or create the deck's project and apply the batch settings to it.

    Slide-level edits (notes overrides, include flags) are preserved; settings come from the batch.
    """
    settings = template.model_copy(deep=True)
    settings.export.output_dir = item.output.parent
    if Project.exists(item.project_dir):
        project = Project.load(item.project_dir)
        project.state.settings = settings
        project.refresh_deck()
        project.save()
    else:
        project = Project.create(item.project_dir, item.deck, settings)
    return project


def deck_is_up_to_date(
    project: Project,
    backend: TTSBackend,
    output: Path,
    progress: P.ProgressFn | None = None,
) -> bool:
    """True when the output exists and no slide needs (re)synthesis."""
    if not output.is_file():
        return False
    # Planning touches backend.model_id, which can trigger the first-run model download.
    if progress is not None:
        backend.on_status = P._status_progress(progress)
    try:
        plans = P.plan_synthesis(project, backend.model_id)
    except Exception as exc:
        # A planning failure is not a reason to skip the deck: it gets attempted below and any
        # real error is reported there.
        log.warning("Could not plan synthesis for %s: %s", project.deck_path.name, exc)
        return False
    finally:
        backend.on_status = None
    return all(p.cached for p in plans)


def run_batch(
    items: list[BatchItem],
    template: ProjectSettings,
    backend: TTSBackend,
    progress: P.ProgressFn = lambda _p: None,
    cancel: P.CancelToken | None = None,
    skip_up_to_date: bool = True,
    on_item: Callable[[int, BatchItem], None] | None = None,
) -> list[BatchItem]:
    """Run render -> synth -> export for every item. One failure does not stop the others."""
    cancel = cancel or P.CancelToken()
    n = len(items)

    def notify(i: int) -> None:
        if on_item:
            on_item(i, items[i])

    for i, item in enumerate(items):
        cancel.check()
        label = f"[{i + 1}/{n}] {item.deck.name}"
        item.status = "running"
        item.error = None
        notify(i)
        t0 = time.perf_counter()

        def _inner(p: P.Progress, i: int = i, label: str = label) -> None:
            overall = (i + p.fraction) / n
            progress(
                P.Progress(
                    "batch", int(overall * 1000), 1000, f"{label}: {p.message}", p.slide_index
                )
            )

        try:
            progress(P.Progress("batch", int(i / n * 1000), 1000, f"{label}: opening project"))
            project = open_batch_project(item, template)
            if skip_up_to_date and deck_is_up_to_date(project, backend, item.output, _inner):
                item.status = "skipped"
                log.info("%s already up to date, skipping", item.deck.name)
            else:
                result = P.run_all(project, backend, _inner, cancel, output=item.output)
                item.output = result.output
                item.status = "done"
                log.info(
                    "%s -> %s (%.1f MB)", item.deck.name, result.output, result.size_bytes / 1e6
                )
        except Cancelled:
            item.status = "pending"
            notify(i)
            raise
        except (SlidecastError, OSError) as exc:
            # One deck must not take the run down with it. OSError covers the Windows realities
            # this hits in practice - a file OneDrive has locked, a scratch directory PowerPoint
            # has not let go of yet - which are per-deck problems, not reasons to stop.
            item.status = "error"
            item.error = str(exc)
            log.error("%s failed: %s", item.deck.name, exc)
            if "out of memory" in str(exc).lower():
                notify(i)
                raise
        except Exception as exc:
            # An unexpected bug in one deck's pipeline is still only that deck's problem.
            item.status = "error"
            item.error = f"Unexpected error: {exc}"
            log.exception("%s crashed", item.deck.name)
            if "out of memory" in str(exc).lower():
                notify(i)
                raise
        finally:
            item.seconds = time.perf_counter() - t0
        notify(i)
    done = sum(1 for it in items if it.status == "done")
    skipped = sum(1 for it in items if it.status == "skipped")
    failed = sum(1 for it in items if it.status == "error")
    progress(
        P.Progress(
            "batch",
            1000,
            1000,
            f"Batch finished: {done} exported, {skipped} up to date, {failed} failed",
        )
    )
    return items
