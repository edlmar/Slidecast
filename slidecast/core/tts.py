"""TTS backends. The real one wraps Chatterbox-Turbo; a fake one keeps tests GPU-free.

Facts about the Chatterbox-Turbo API this module relies on (verified against
``src/chatterbox/tts_turbo.py``):

* ``ChatterboxTurboTTS.from_pretrained(device)`` downloads ``ResembleAI/chatterbox-turbo``.
* ``generate(text, audio_prompt_path=..., ...)`` re-encodes the reference clip on *every* call
  when ``audio_prompt_path`` is passed.  We therefore call ``prepare_conditionals()`` once and
  ``generate(text)`` without the path afterwards.
* ``exaggeration``, ``cfg_weight`` and ``min_p`` are accepted but ignored by Turbo.  The real
  knobs are ``temperature``, ``top_p``, ``top_k`` and ``repetition_penalty``.
* Output is ``(1, N)`` float32 at ``model.sr`` (24 kHz) and already Perth-watermarked.
* ``generate`` does not use ``inference_mode``; we wrap it ourselves.  There is no seed
  argument; we seed torch around each call.
"""

from __future__ import annotations

import importlib.metadata
import math
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable

import numpy as np

from slidecast.core.audio import FloatArray
from slidecast.core.config import TTSSettings
from slidecast.core.errors import ModelLoadError, ReferenceClipError, TTSError, TTSOutOfMemory
from slidecast.core.logging_setup import get_logger

log = get_logger("tts")

CHATTERBOX_TURBO_REPO = "ResembleAI/chatterbox-turbo"
_HF_PATTERNS = ["*.safetensors", "*.json", "*.txt", "*.pt", "*.model"]

# Backend status callback: (message, fraction) where fraction is 0..1 download progress,
# or None for an indeterminate phase message.
StatusFn = Callable[[str, float | None], None]


class _DownloadTracker:
    """Aggregates byte counts across the progress bars of one ``snapshot_download`` call.

    huggingface_hub runs two byte bars per snapshot (network transfer + file reconstruction)
    that advance over the same bytes, so the aggregate takes the max of each, not the sum -
    summing would double the reported GB.
    """

    def __init__(self, cb: Callable[[int, int], None]) -> None:
        self._cb = cb
        self._lock = threading.Lock()
        self._bars: list[_HFProgressBar] = []
        self._last_emit = 0.0
        self.downloaded = 0
        self.total = 0

    def register(self, bar: _HFProgressBar) -> None:
        with self._lock:
            self._bars.append(bar)

    def report(self) -> None:
        """Push aggregated counts to the callback, throttled to ~every 200 ms."""
        with self._lock:
            self.total = int(max((b.total for b in self._bars), default=0) or 0)
            self.downloaded = int(max((b.n for b in self._bars), default=0) or 0)
            if self.total <= 0 or self.downloaded <= 0:
                return
            now = time.monotonic()
            finished = self.downloaded >= self.total
            if not finished and now - self._last_emit < 0.2:
                return
            self._last_emit = now
            done, total = self.downloaded, self.total
        self._cb(done, total)


class _HFProgressBar:
    """Minimal tqdm stand-in passed as ``tqdm_class`` to ``snapshot_download``.

    huggingface_hub instantiates it per call with plain kwargs: a "Downloading bytes"
    transfer bar and a "Reconstructing" bar (both ``unit="B"``), plus a "Fetching N files"
    item counter. Only the byte bars register with ``tracker``; the rest of the tqdm API
    (``.total`` assignment, ``refresh``, postfix/setters, context manager) is accepted and
    ignored so nothing is ever drawn to a console.
    """

    tracker: ClassVar[_DownloadTracker | None] = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.n: float = kwargs.get("initial") or 0
        self._total: float = kwargs.get("total") or 0
        self.is_bytes = kwargs.get("unit") == "B"
        if self.is_bytes and self.tracker is not None:
            self.tracker.register(self)

    @property
    def total(self) -> float:
        return self._total

    @total.setter
    def total(self, value: float | None) -> None:
        self._total = value or 0

    @property
    def format_dict(self) -> dict[str, Any]:
        return {"n": self.n, "total": self._total, "rate": None}

    def update(self, n: float | None = 1) -> None:
        self.n += n or 0
        self._report()

    def update_transfer(self, n: float | None = 1) -> None:
        self.update(n)

    def refresh(self) -> None:
        self._report()

    def close(self) -> None:
        self._report()

    def __getattr__(self, name: str) -> Any:
        # Every other tqdm call huggingface_hub makes (set_description, set_description_str,
        # set_postfix_str, ... and whatever a later release adds) only changes what a console
        # bar would show. Accept and ignore them all, so a new one never aborts a download.
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *args, **kwargs: None

    def _report(self) -> None:
        if self.is_bytes and self.tracker is not None:
            self.tracker.report()

    def __enter__(self) -> _HFProgressBar:
        return self

    def __exit__(self, *args: object) -> None:
        pass


def _tracked_bar_class(tracker: _DownloadTracker) -> type[_HFProgressBar]:
    """A fresh ``_HFProgressBar`` subclass bound to one snapshot_download's tracker."""

    class _BoundBar(_HFProgressBar):
        pass

    _BoundBar.tracker = tracker
    return _BoundBar


def derive_seed(base_seed: int, chunk_index: int) -> int:
    """Per-chunk seed that depends only on the base seed and chunk position within a slide."""
    return (base_seed * 1_000_003 + chunk_index * 7919) % (2**31 - 1)


@runtime_checkable
class TTSBackend(Protocol):
    """Minimal interface the pipeline needs."""

    on_status: StatusFn | None

    @property
    def sr(self) -> int: ...

    @property
    def model_id(self) -> str: ...

    @property
    def is_loaded(self) -> bool: ...

    def load(self) -> None: ...

    def set_reference(self, clip: Path) -> None: ...

    def synthesize(self, text: str, seed: int) -> FloatArray: ...

    def unload(self) -> None: ...


class ChatterboxTurboBackend:
    """Chatterbox-Turbo wrapper. Model is loaded once and reused for the whole session."""

    def __init__(self, settings: TTSSettings) -> None:
        self.settings = settings
        self.on_status: StatusFn | None = None
        self._model: object | None = None
        self._model_id: str | None = None
        self._reference: Path | None = None
        self._sr = 24000
        self._download_logged = False

    def _status(self, msg: str, frac: float | None = None) -> None:
        if self.on_status is not None:
            self.on_status(msg, frac)

    # -- identity -------------------------------------------------------------------------

    @property
    def sr(self) -> int:
        return self._sr

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def model_id(self) -> str:
        """``chatterbox-turbo@<hf commit>+chatterbox-tts <pkg version>``.

        Resolving this does not load the model; it only makes sure the weights are downloaded so
        the snapshot commit hash is known, which the cache key depends on.
        """
        if self._model_id is None:
            self._model_id = self.resolve_model_id()
        return self._model_id

    @staticmethod
    def package_version() -> str:
        try:
            return importlib.metadata.version("chatterbox-tts")
        except importlib.metadata.PackageNotFoundError:
            return "missing"

    def _report_download(self, done: int, total: int) -> None:
        if not self._download_logged:
            self._download_logged = True
            log.info("Downloading %s weights (~%.1f GB)", CHATTERBOX_TURBO_REPO, total / 1e9)
        self._status(
            f"Downloading voice model — {done / 1e9:.1f} of {total / 1e9:.1f} GB (first run only)",
            min(done / total, 1.0),
        )

    def resolve_model_id(self) -> str:
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise ModelLoadError("huggingface_hub is not installed") from exc
        self._status("Checking voice model…", None)
        tracker = _DownloadTracker(self._report_download)
        try:
            local = snapshot_download(
                CHATTERBOX_TURBO_REPO,
                allow_patterns=_HF_PATTERNS,
                tqdm_class=_tracked_bar_class(tracker),
            )
        except Exception as exc:  # network / auth / disk errors all end up here
            raise ModelLoadError(
                f"Could not download {CHATTERBOX_TURBO_REPO} from Hugging Face: {exc}"
            ) from exc
        if tracker.downloaded:
            log.info("Voice model download finished (%.1f GB)", tracker.downloaded / 1e9)
        commit = Path(local).name
        return f"chatterbox-turbo@{commit}+chatterbox-tts {self.package_version()}"

    # -- lifecycle ------------------------------------------------------------------------

    def load(self) -> None:
        if self._model is not None:
            return
        self._status("Loading voice model onto the GPU…", None)
        try:
            import torch
        except ImportError as exc:
            raise ModelLoadError("torch is not installed. Install the [tts] extra.") from exc
        device = self.settings.device
        if device.startswith("cuda") and not torch.cuda.is_available():
            raise ModelLoadError(
                "CUDA is not available to torch. Make sure a CUDA build of torch is installed "
                "(pixi: torch from the cu128 index) and the NVIDIA driver is current, or set the "
                "device to 'cpu'."
            )
        try:
            from chatterbox.tts_turbo import ChatterboxTurboTTS
        except ImportError as exc:
            raise ModelLoadError(
                "chatterbox-tts is not installed. Run: pip install --no-deps chatterbox-tts==0.1.7"
            ) from exc
        # Resolve the weights ourselves first: on a cold cache this runs the multi-GB
        # download with progress reported through on_status, and from_pretrained below
        # is then a pure cache hit instead of a silent download of its own.
        _ = self.model_id
        log.info("Loading Chatterbox-Turbo on %s", device)
        try:
            self._model = ChatterboxTurboTTS.from_pretrained(device=device)
        except torch.cuda.OutOfMemoryError as exc:
            raise TTSOutOfMemory("while loading the model") from exc
        except Exception as exc:  # weights missing, arch mismatch, etc.
            msg = str(exc)
            if "no kernel image" in msg or "sm_" in msg:
                msg += (
                    " -- this usually means the installed torch build does not support your GPU "
                    "architecture (RTX 50-series needs torch >= 2.7 with CUDA 12.8 wheels)."
                )
            raise ModelLoadError(f"Failed to load Chatterbox-Turbo: {msg}") from exc
        self._sr = int(getattr(self._model, "sr", 24000))
        log.info("Model ready (sr=%d, id=%s)", self._sr, self.model_id)
        if self._reference is not None:
            self._prepare(self._reference)

    def unload(self) -> None:
        if self._model is None:
            return
        log.info("Unloading TTS model to free VRAM")
        self._model = None
        try:
            import gc

            import torch

            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:  # pragma: no cover
            pass

    # -- reference voice -------------------------------------------------------------------

    def set_reference(self, clip: Path) -> None:
        clip = Path(clip)
        if not clip.is_file():
            raise ReferenceClipError(f"Reference clip not found: {clip}")
        self._reference = clip
        if self._model is not None:
            self._prepare(clip)

    def _prepare(self, clip: Path) -> None:
        assert self._model is not None
        import torch

        self._status("Encoding reference voice…", None)
        log.info("Encoding reference voice from %s", clip.name)
        try:
            with torch.inference_mode():
                self._model.prepare_conditionals(str(clip))  # type: ignore[attr-defined]
        except AssertionError as exc:
            raise ReferenceClipError(
                f"Chatterbox rejected the reference clip ({exc}). It must be longer than 5 s."
            ) from exc
        except torch.cuda.OutOfMemoryError as exc:
            raise TTSOutOfMemory("while encoding the reference clip") from exc

    # -- synthesis -----------------------------------------------------------------------

    def synthesize(self, text: str, seed: int) -> FloatArray:
        if self._model is None:
            self.load()
        assert self._model is not None
        if self._reference is None:
            raise ReferenceClipError("No reference clip set. Pick a voice clip first.")
        import torch

        s = self.settings
        try:
            torch.manual_seed(seed)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(seed)
            with torch.inference_mode():
                wav = self._model.generate(  # type: ignore[attr-defined]
                    text,
                    temperature=s.temperature,
                    top_p=s.top_p,
                    top_k=s.top_k,
                    repetition_penalty=s.repetition_penalty,
                )
        except torch.cuda.OutOfMemoryError as exc:
            torch.cuda.empty_cache()
            raise TTSOutOfMemory(f"chunk of {len(text)} characters") from exc
        except RuntimeError as exc:
            if "out of memory" in str(exc).lower():
                torch.cuda.empty_cache()
                raise TTSOutOfMemory(f"chunk of {len(text)} characters") from exc
            raise TTSError(f"Chatterbox failed on chunk: {exc}") from exc
        arr = wav.detach().float().cpu().numpy()
        if arr.ndim == 2:
            arr = arr[0]
        return np.asarray(arr, dtype=np.float32)


class FakeBackend:
    """Deterministic stand-in: produces a soft tone whose length scales with text length."""

    def __init__(self, sr: int = 24000, seconds_per_char: float = 0.055) -> None:
        self.on_status: StatusFn | None = None
        self._sr = sr
        self._spc = seconds_per_char
        self._loaded = False
        self._reference: Path | None = None
        self.calls: list[tuple[str, int]] = []

    @property
    def sr(self) -> int:
        return self._sr

    @property
    def model_id(self) -> str:
        return "fake-tts@1"

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def load(self) -> None:
        self._loaded = True

    def unload(self) -> None:
        self._loaded = False

    def set_reference(self, clip: Path) -> None:
        self._reference = Path(clip)

    def synthesize(self, text: str, seed: int) -> FloatArray:
        self.calls.append((text, seed))
        n = max(int(len(text) * self._spc * self._sr), self._sr // 10)
        t = np.arange(n, dtype=np.float32) / self._sr
        rng = np.random.default_rng(seed)
        freq = 150.0 + float(rng.integers(0, 60))
        env = np.minimum(1.0, np.minimum(t / 0.02, (t[-1] - t) / 0.02 + 1e-6))
        tone = 0.2 * np.sin(2 * math.pi * freq * t) * env
        pad = np.zeros(int(0.15 * self._sr), dtype=np.float32)  # silence for trim tests
        return np.concatenate([pad, tone.astype(np.float32), pad])
