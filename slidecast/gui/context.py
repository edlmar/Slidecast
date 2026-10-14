"""Application context shared by all views: current project, TTS backend, task runner.

It outlives any one page: the window can be reloaded mid-run and the new page picks up the same
project and the same running task.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from slidecast.core import pipeline as P
from slidecast.core.config import ProjectSettings
from slidecast.core.errors import SlidecastError
from slidecast.core.logging_setup import configure_logging, get_logger
from slidecast.core.project import Project
from slidecast.core.tts import ChatterboxTurboBackend, FakeBackend, TTSBackend
from slidecast.gui.settings_store import SettingsStore
from slidecast.gui.signals import Signal, call_soon
from slidecast.gui.workers import LogBridge, Worker

log = get_logger("gui")


class AppContext:
    def __init__(self, fake_tts: bool = False) -> None:
        self.project_changed = Signal()  # Project | None
        self.slide_changed = Signal()  # int: one row updated
        self.slides_changed = Signal()  # many rows / statuses updated
        self.renders_changed = Signal()  # renders available/invalidated
        self.settings_changed = Signal()  # project settings edited in UI
        self.task_started = Signal()  # str title
        self.task_progress = Signal()  # Progress
        self.task_finished = Signal()  # (message, ok)
        self.paused_changed = Signal()  # bool
        self.message = Signal()  # (level, text) transient notice for the status bar
        self.encoders_probed = Signal()  # dict[str, str]
        self.batch_changed = Signal()  # the folder scan produced a new set of decks
        self.batch_item_changed = Signal()  # (index, BatchItem)
        self.player_stop = Signal()  # ask every page to stop audio playback

        self.store = SettingsStore()
        self.logs = LogBridge()
        self.project: Project | None = None
        # Editable settings when no single deck is open - what a batch run will use.
        self._defaults: ProjectSettings = self.store.default_settings()
        self.fake_tts = fake_tts
        self._backend: TTSBackend | None = None
        self._worker: Worker | None = None
        self.task_title = ""
        self.last_progress: P.Progress | None = None
        self.encoder_report: dict[str, str] = {}
        # Project folders of the decks in the current folder scan, so the Rebuild step can work
        # on a whole batch when no single deck is open.
        self.batch_project_dirs: list[Path] = []
        # The native window is sized to its content once per run, not on every reload.
        self.window_fitted = False

    # -- project ----------------------------------------------------------------------------

    def open_deck(self, deck: Path, project_dir: Path | None = None) -> Project:
        self._require_idle("opening a deck")
        project_dir = project_dir or deck.with_name(deck.stem + "_slidecast")
        if Project.exists(project_dir):
            project = Project.load(project_dir)
            if project.refresh_deck():
                log.info("Deck changed since last session; slide list refreshed")
        else:
            project = Project.create(project_dir, deck, self.store.default_settings())
        self.set_project(project)
        return project

    def open_project(self, project_dir: Path) -> Project:
        self._require_idle("opening a project")
        project = Project.load(project_dir)
        self.set_project(project)
        return project

    def set_project(self, project: Project | None) -> None:
        self.player_stop.emit()
        self.project = project
        if project is not None:
            configure_logging(project.log_file, console=False)
            self.store.add_recent_project(project.directory)
            if self._backend is not None and isinstance(self._backend, ChatterboxTurboBackend):
                self._backend.settings = project.settings.tts
        self.project_changed.emit(project)

    def close_project(self) -> None:
        if self.busy:
            self.message.emit("warn", "A task is still running - cancel it before closing.")
            return
        self.set_project(None)

    def _require_idle(self, what: str) -> None:
        """A running task owns the open project; swapping it under the worker corrupts both."""
        if self.busy:
            raise SlidecastError(
                f"A task is still running - cancel it or wait for it before {what}."
            )

    def save_project(self) -> None:
        if self.project is not None:
            self.project.save()
            self.store.save_default_settings(self.project.settings)

    def settings(self) -> ProjectSettings:
        """The settings the UI edits: the open project's, or the shared defaults for a batch."""
        return self.project.settings if self.project is not None else self._defaults

    def settings_edited(self) -> None:
        if self.project is not None:
            self.save_project()
        else:
            self.store.save_default_settings(self._defaults)
        self.settings_changed.emit()

    # -- backend ----------------------------------------------------------------------------

    def backend(self) -> TTSBackend:
        if self._backend is None:
            if self.fake_tts:
                self._backend = FakeBackend()
            else:
                self._backend = ChatterboxTurboBackend(self.settings().tts)
        return self._backend

    def unload_backend(self) -> None:
        if self._backend is not None:
            self._backend.unload()

    # -- tasks ------------------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        # _worker stays set until the finish callback has run on the UI loop, not just until
        # the thread returns - a second task started in that gap would otherwise be clobbered.
        return self._worker is not None

    def run_task(
        self,
        title: str,
        fn: Callable[[Callable[[P.Progress], None], P.CancelToken], Any],
        on_success: Callable[[Any], None] | None = None,
        on_done: Callable[[], None] | None = None,
    ) -> bool:
        if self.busy:
            self.message.emit("warn", "Another task is still running.")
            return False

        def _finish(msg: str, ok: bool) -> None:
            self._worker = None
            self.task_title = ""
            self.last_progress = None
            self.task_finished.emit(msg, ok)
            if on_done:
                on_done()

        def _ok(result: Any) -> None:
            if on_success:
                try:
                    on_success(result)
                except Exception:
                    log.exception("%s: result handler failed", title)
            _finish(f"{title}: done", True)

        def _progress(p: P.Progress) -> None:
            self.last_progress = p
            self.task_progress.emit(p)

        worker = Worker(
            title,
            fn,
            on_progress=_progress,
            on_success=lambda r: call_soon(_ok, r),
            on_failed=lambda m: call_soon(_finish, f"{title} failed: {m}", False),
            on_cancelled=lambda: call_soon(_finish, f"{title} cancelled", False),
        )
        self._worker = worker
        self.task_title = title
        self.last_progress = None
        self.paused_changed.emit(False)
        self.task_started.emit(title)
        worker.start()
        return True

    def cancel_task(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

    def set_paused(self, paused: bool) -> None:
        """Pause/resume the running task. Takes effect at the next safe point."""
        if self._worker is not None:
            self._worker.set_paused(paused)
            self.paused_changed.emit(paused)

    @property
    def paused(self) -> bool:
        return self._worker is not None and self._worker.is_paused

    def wait_for_task(self, timeout_s: float = 30.0) -> None:
        """Cancel the running task and wait for it (used at shutdown). A thread that ignores the
        cancel is a daemon and dies with the process; its subprocesses sit in proc.py's
        kill-on-close job either way."""
        worker = self._worker
        if worker is None:
            return
        worker.cancel()
        worker.join(timeout_s)
        if worker.is_alive():
            log.error("Task %r ignored cancellation; leaving it to process exit", worker.title)

    # -- pipeline shortcuts ----------------------------------------------------------------

    def start_render(self, on_done: Callable[[], None] | None = None) -> bool:
        project = self.project
        if project is None:
            return False

        def work(progress: Callable[[P.Progress], None], cancel: P.CancelToken) -> Any:
            return P.stage_render(project, progress, cancel)

        def done() -> None:
            self.renders_changed.emit()
            if on_done:
                on_done()

        return self.run_task("Rendering slides", work, on_done=done)

    def start_slide_clips(self, on_done: Callable[[], None] | None = None) -> bool:
        """Render each slide to video via PowerPoint (animations, transitions, media)."""
        project = self.project
        if project is None:
            return False

        def work(progress: Callable[[P.Progress], None], cancel: P.CancelToken) -> Any:
            return P.stage_slide_clips(project, progress, cancel)

        def done() -> None:
            self.renders_changed.emit()
            if on_done:
                on_done()

        return self.run_task("Rendering slide videos", work, on_done=done)

    def start_synthesis(self, only: set[int] | None = None, force: bool = False) -> bool:
        project = self.project
        if project is None:
            return False
        backend = self.backend()

        def work(progress: Callable[[P.Progress], None], cancel: P.CancelToken) -> Any:
            def _p(p: P.Progress) -> None:
                progress(p)
                if p.slide_index is not None:
                    self.slide_changed.emit(p.slide_index)

            return P.stage_synthesize(project, backend, _p, cancel, only=only, force=force)

        return self.run_task("Synthesizing", work, on_done=self.slides_changed.emit)

    def start_export(self, on_success: Callable[[P.ExportResult], None]) -> bool:
        project = self.project
        if project is None:
            return False

        def work(progress: Callable[[P.Progress], None], cancel: P.CancelToken) -> Any:
            return P.stage_export(project, progress, cancel)

        return self.run_task(
            "Exporting", work, on_success=on_success, on_done=self.renders_changed.emit
        )

    def start_run_all(self, on_success: Callable[[Any], None] | None = None) -> bool:
        """One click: slides -> narration -> encoded video."""
        project = self.project
        if project is None:
            return False
        backend = self.backend()

        def work(progress: Callable[[P.Progress], None], cancel: P.CancelToken) -> Any:
            def _p(p: P.Progress) -> None:
                progress(p)
                if p.slide_index is not None:
                    self.slide_changed.emit(p.slide_index)

            return P.run_all(project, backend, _p, cancel)

        def done() -> None:
            self.slides_changed.emit()
            self.renders_changed.emit()

        return self.run_task("Running pipeline", work, on_success=on_success, on_done=done)

    def start_audition(self, text: str, on_success: Callable[[Any], None]) -> bool:
        """Audition the voice. Works without an open deck, so a batch can be tuned first."""
        settings = self.settings()
        project = self.project
        backend = self.backend()

        def work(progress: Callable[[P.Progress], None], cancel: P.CancelToken) -> Any:
            progress(P.Progress("synth", 0, 1, "Loading model and synthesizing test phrase"))
            wav, sr = P.audition(backend, settings, text, progress)
            import tempfile

            from slidecast.core.audio import save_wav

            folder = project.work_dir if project is not None else Path(tempfile.gettempdir())
            out = folder / "slidecast_audition.wav"
            save_wav(out, wav, sr)
            return wav, sr, out

        return self.run_task("Test phrase", work, on_success=on_success)

    def batch_backend(self, template: ProjectSettings) -> TTSBackend:
        """Backend for batch runs (works without an open project)."""
        if self._backend is None:
            self._backend = FakeBackend() if self.fake_tts else ChatterboxTurboBackend(template.tts)
        elif isinstance(self._backend, ChatterboxTurboBackend):
            self._backend.settings = template.tts
        return self._backend

    def start_batch(
        self,
        items: list[Any],
        template: ProjectSettings,
        skip_up_to_date: bool,
        on_success: Callable[[Any], None] | None = None,
    ) -> bool:
        from slidecast.core.batch import run_batch

        backend = self.batch_backend(template)
        self.store.save_default_settings(template)

        def work(progress: Callable[[P.Progress], None], cancel: P.CancelToken) -> Any:
            return run_batch(
                items,
                template,
                backend,
                progress,
                cancel,
                skip_up_to_date,
                on_item=self.batch_item_changed.emit,
            )

        return self.run_task("Batch", work, on_success=on_success)

    def probe_encoders(self) -> None:
        """Probe ffmpeg encoders in the background (results cached on the context)."""
        from slidecast.core import ffmpeg as F

        settings = self.project.settings.export if self.project else None
        explicit = settings.ffmpeg_path if settings else None

        def work(progress: Callable[[P.Progress], None], cancel: P.CancelToken) -> Any:
            report: dict[str, str] = {}
            try:
                ff = F.find_ffmpeg(explicit)
            except Exception as exc:
                report["ffmpeg"] = f"missing: {exc}"
                return report
            report["ffmpeg"] = f"{ff} ({F.ffmpeg_version(ff)})"
            avail = F.probe_encoders(ff)
            for codec in ("av1", "hevc"):
                for mode in ("nvenc", "software"):
                    name = F.ENCODERS[codec][mode]
                    if name not in avail:
                        report[name] = "missing"
                    elif mode == "nvenc" and not F.nvenc_smoke_test(ff, name):
                        report[name] = "present, failed to initialise"
                    else:
                        report[name] = "ok"
            return report

        def done(result: Any) -> None:
            self.encoder_report = dict(result)
            self.encoders_probed.emit(self.encoder_report)

        self.run_task("Checking encoders", work, on_success=done)
