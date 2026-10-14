"""Content-hash caching helpers.

The single most important design constraint: re-running a deck after editing three slides must
only re-synthesize those three.  The audio cache key for a slide is a hash over everything that
influences its audio: cleaned narration text, TTS params, chunk-level post params, the reference
clip's content hash, the model identity, and the text-prep version.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TypeVar

from slidecast.core.errors import StorageError
from slidecast.core.logging_setup import get_logger

log = get_logger("cache")
_T = TypeVar("_T")


def hash_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def slide_audio_key(
    cleaned_text: str,
    chunks: Sequence[str],
    tts_params: Mapping[str, object],
    post_params: Mapping[str, object],
    reference_hash: str,
    model_id: str,
    textprep_version: str,
) -> str:
    """Stable 40-hex-char key for one slide's narration audio.

    The chunk list is hashed as well as the cleaned text because it *is* what is synthesized:
    a change in chunking (``chunk_chars``, a textprep tweak) produces different audio for the
    same cleaned text, and a key that only saw the text would silently reuse it.
    """
    payload = {
        "text": cleaned_text,
        "chunks": list(chunks),
        "tts": dict(tts_params),
        "post": dict(post_params),
        "ref": reference_hash,
        "model": model_id,
        "textprep": textprep_version,
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()[:40]


def render_key(deck_hash: str, width: int, height: int, background: str) -> str:
    return hashlib.sha256(f"{deck_hash}|{width}x{height}|{background}".encode()).hexdigest()[:24]


# Windows error codes for "someone else has this file open right now": access denied and
# sharing violation. OneDrive, antivirus and the search indexer all hold brief locks on files
# the moment they are written, which makes an immediate rename fail even though nothing is wrong.
_TRANSIENT_WINERRORS = frozenset({5, 32, 33})
# ~5 s of retries in total; observed OneDrive locks clear well inside a second.
_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.2, 2.0)


def _is_transient_lock(exc: OSError) -> bool:
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        return winerror in _TRANSIENT_WINERRORS
    return isinstance(exc, PermissionError)


def retry_locked_io(operation: Callable[[], _T], what: str) -> _T:
    """Run a filesystem operation, retrying while another process holds the file open.

    Projects living in OneDrive/Dropbox folders (or behind aggressive antivirus) routinely hit
    transient locks; without this a 90-minute run could die on its last slide.
    """
    last: OSError | None = None
    for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
        try:
            return operation()
        except OSError as exc:
            if not _is_transient_lock(exc):
                raise
            last = exc
            if delay is None:
                break
            if attempt == 0:
                log.debug("%s is locked by another process; retrying", what)
            time.sleep(delay)
    raise StorageError(
        f"Could not {what}: the file stayed locked by another program for "
        f"{sum(_RETRY_DELAYS):.0f} seconds ({last}). This usually means OneDrive, Dropbox or "
        "antivirus is syncing/scanning the project folder. Pause syncing for this folder, exclude "
        "it from your antivirus, or keep the project somewhere local."
    ) from last


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write to a temp file in the same directory and rename, so readers never see partials."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        retry_locked_io(lambda: os.replace(tmp, path), f"write {path.name}")
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def atomic_replace(tmp_path: Path, final_path: Path) -> None:
    """Rename a fully written temp file into place, waiting out transient file locks."""
    final_path = Path(final_path)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    retry_locked_io(lambda: os.replace(tmp_path, final_path), f"save {final_path.name}")


class AudioCache:
    """Per-slide narration audio keyed by content hash. Each entry is <key>.wav + <key>.json."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def wav_path(self, key: str) -> Path:
        return self.directory / f"{key}.wav"

    def meta_path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def temp_wav_path(self, key: str) -> Path:
        return self.directory / f".{key}.partial.wav"

    def has(self, key: str) -> bool:
        """True only for a *usable* entry: non-trivial wav plus parseable metadata.

        Anything else - a wav truncated to a stub, meta that is not JSON or not a dict, or meta
        without a duration - counts as a miss and is regenerated rather than crashing read_meta
        or exporting broken audio.
        """
        wav, meta = self.wav_path(key), self.meta_path(key)
        try:
            if not (wav.is_file() and meta.is_file()) or wav.stat().st_size <= 44:
                return False
        except OSError:
            return False
        try:
            with meta.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return False
        return isinstance(data, dict) and isinstance(data.get("duration"), (int, float))

    def read_meta(self, key: str) -> dict[str, Any]:
        with self.meta_path(key).open("r", encoding="utf-8") as f:
            data: dict[str, Any] = json.load(f)
        return data

    def commit(self, key: str, meta: Mapping[str, Any]) -> Path:
        """Promote the partial wav to its final name and write metadata (wav first, then json).

        `has()` requires both files, so a crash between the two renames leaves the entry
        invisible and it will simply be regenerated.
        """
        tmp = self.temp_wav_path(key)
        if not tmp.is_file():
            raise FileNotFoundError(tmp)
        atomic_replace(tmp, self.wav_path(key))
        atomic_write_json(self.meta_path(key), dict(meta))
        return self.wav_path(key)

    def discard_partial(self, key: str) -> None:
        """Remove a half-written file. Runs inside error handlers, so it never raises."""
        tmp = self.temp_wav_path(key)
        if not tmp.exists():
            return
        try:
            retry_locked_io(tmp.unlink, f"remove {tmp.name}")
        except (StorageError, OSError) as exc:
            # A leftover .partial is harmless: has() ignores it and cleanup_partials()
            # sweeps it on the next load.
            log.warning("Could not remove partial file %s: %s", tmp.name, exc)

    def cleanup_partials(self) -> int:
        n = 0
        for p in self.directory.glob(".*.partial.wav"):
            try:
                retry_locked_io(p.unlink, f"remove {p.name}")
            except (StorageError, OSError) as exc:
                log.warning("Could not remove stale partial %s: %s", p.name, exc)
                continue
            n += 1
        return n

    def size_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.directory.glob("*.wav"))
