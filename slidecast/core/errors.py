"""Exception hierarchy for Slidecast. Every error carries a user-readable message."""

from __future__ import annotations


class SlidecastError(Exception):
    """Base class for all expected failures."""


class DeckError(SlidecastError):
    """The .pptx could not be read."""


class RenderError(SlidecastError):
    """Slide rendering failed."""


class LibreOfficeNotFound(RenderError):
    """LibreOffice (soffice) could not be located."""

    def __init__(self, searched: list[str]) -> None:
        self.searched = searched
        lines = "\n  ".join(searched)
        super().__init__(
            "LibreOffice was not found. Install it from https://www.libreoffice.org/download "
            "(on Windows: winget install TheDocumentFoundation.LibreOffice), or set the "
            f"soffice path in Settings. Searched:\n  {lines}"
        )


class TTSError(SlidecastError):
    """Speech synthesis failed."""


class ModelLoadError(TTSError):
    """The TTS model could not be loaded."""


class TTSOutOfMemory(TTSError):
    """CUDA ran out of memory."""

    def __init__(self, detail: str = "") -> None:
        super().__init__(
            "The GPU ran out of memory while synthesizing. Close other GPU programs, "
            "lower the chunk size in Settings, or switch the device to CPU. "
            + (f"({detail})" if detail else "")
        )


class ReferenceClipError(TTSError):
    """The reference voice clip is unusable."""


class FFmpegError(SlidecastError):
    """ffmpeg is missing or failed."""


class FFmpegNotFound(FFmpegError):
    def __init__(self, searched: list[str]) -> None:
        lines = "\n  ".join(searched)
        super().__init__(
            "ffmpeg was not found. Install it (Windows: winget install Gyan.FFmpeg) or set "
            f"the ffmpeg path in Settings. Searched:\n  {lines}"
        )


class EncoderUnavailable(FFmpegError):
    """No usable encoder for the requested codec."""


class StorageError(SlidecastError):
    """A file could not be written or renamed (locked by another program, disk full, ...)."""


class Cancelled(SlidecastError):
    """The user cancelled the operation."""


class ProjectError(SlidecastError):
    """Project state is missing or inconsistent."""
