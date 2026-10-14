"""Render slides to PNG: LibreOffice (pptx -> pdf) then PyMuPDF (pdf -> letterboxed PNG).

Renders are cached under ``<project>/renders/<render_key>/`` and reused while the deck hash and
resolution are unchanged.  Cross-dissolve frames for transitions are also produced here.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from slidecast.core import proc
from slidecast.core.cache import atomic_write_json, render_key, retry_locked_io
from slidecast.core.config import RenderSettings
from slidecast.core.errors import Cancelled, LibreOfficeNotFound, RenderError
from slidecast.core.logging_setup import get_logger

log = get_logger("render")

ProgressFn = Callable[[int, int, str], None]
CancelFn = Callable[[], bool]

_WINDOWS_SOFFICE_CANDIDATES = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
)
_UNIX_SOFFICE_CANDIDATES = (
    "/usr/bin/soffice",
    "/usr/lib/libreoffice/program/soffice",
    "/opt/libreoffice/program/soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
)


def find_soffice(explicit: Path | None = None) -> Path:
    """Locate the LibreOffice binary or raise LibreOfficeNotFound with the searched paths."""
    searched: list[str] = []
    if explicit:
        searched.append(str(explicit))
        if Path(explicit).is_file():
            return Path(explicit)
    env = os.environ.get("SLIDECAST_SOFFICE")
    if env:
        searched.append(f"$SLIDECAST_SOFFICE={env}")
        if Path(env).is_file():
            return Path(env)
    for name in ("soffice", "soffice.exe", "libreoffice"):
        found = shutil.which(name)
        searched.append(f"PATH:{name}")
        if found:
            return Path(found)
    candidates = (
        _WINDOWS_SOFFICE_CANDIDATES if sys.platform == "win32" else _UNIX_SOFFICE_CANDIDATES
    )
    for c in candidates:
        searched.append(c)
        if Path(c).is_file():
            return Path(c)
    raise LibreOfficeNotFound(searched)


# Export hidden slides too so PDF page N == slide N; hidden handling happens downstream.
_PDF_FILTER = 'pdf:impress_pdf_Export:{"ExportHiddenSlides":{"type":"boolean","value":"true"}}'


def convert_to_pdf(
    deck: Path, out_dir: Path, soffice: Path, timeout: float = 900.0, cancel: CancelFn | None = None
) -> Path:
    """Run headless LibreOffice to produce ``out_dir/<deck stem>.pdf``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    expected = out_dir / f"{deck.stem}.pdf"
    if expected.exists():
        expected.unlink()
    # A private profile dir avoids clashes with a running LibreOffice / concurrent conversions.
    profile = Path(tempfile.mkdtemp(prefix="slidecast-lo-"))
    profile_url = profile.resolve().as_uri()
    cmd = [
        str(soffice),
        f"-env:UserInstallation={profile_url}",
        "--headless",
        "--norestore",
        "--nologo",
        "--nodefault",
        "--convert-to",
        _PDF_FILTER,
        "--outdir",
        str(out_dir),
        str(deck),
    ]
    log.info("Converting %s to PDF with LibreOffice", deck.name)
    log.debug("soffice command: %s", " ".join(cmd))
    try:
        result = proc.run(cmd, timeout=timeout, cancel=cancel, merge_stderr=True)
    except OSError as exc:
        raise RenderError(f"Could not start LibreOffice: {exc}") from exc
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    output = result.stdout
    if result.cancelled:
        raise Cancelled("Render cancelled")
    if result.timed_out:
        raise RenderError("LibreOffice timed out converting the deck")
    if result.returncode != 0:
        raise RenderError(f"LibreOffice exited with {result.returncode}: {output.strip()[-2000:]}")
    if not expected.is_file():
        # LibreOffice writes <stem>.pdf but sometimes changes case/extension handling.
        pdfs = sorted(out_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
        if pdfs:
            expected = pdfs[0]
        else:
            raise RenderError(
                "LibreOffice reported success but produced no PDF. Output was: "
                f"{output.strip()[-1000:]}"
            )
    return expected


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    v = hex_color.lstrip("#")
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def letterbox(image: Image.Image, width: int, height: int, background: str) -> Image.Image:
    """Fit ``image`` inside width x height preserving aspect, centred on a solid background."""
    src_w, src_h = image.size
    scale = min(width / src_w, height / src_h)
    new_w = max(round(src_w * scale), 1)
    new_h = max(round(src_h * scale), 1)
    resized = (
        image
        if (new_w, new_h) == (src_w, src_h)
        else image.resize((new_w, new_h), Image.Resampling.LANCZOS)
    )
    canvas = Image.new("RGB", (width, height), _hex_to_rgb(background))
    canvas.paste(resized, ((width - new_w) // 2, (height - new_h) // 2))
    return canvas


@contextlib.contextmanager
def _quiet_mupdf() -> Iterator[None]:
    """Send MuPDF's own messages to the log instead of straight to stderr.

    MuPDF prints from C, so its notes about a PDF bypass Python logging entirely and look like
    application errors. They are diagnostics about the file, not failures of the render.
    """
    import pymupdf

    tools = pymupdf.TOOLS
    restore: list[tuple[Any, Any]] = []
    for name in ("mupdf_display_errors", "mupdf_display_warnings"):
        setter = getattr(tools, name, None)
        if setter is None:
            continue
        restore.append((setter, setter(False)))
    try:
        yield
    finally:
        for setter, previous in restore:
            with contextlib.suppress(Exception):
                setter(previous)
        collect = getattr(tools, "mupdf_warnings", None)
        if collect is not None:
            with contextlib.suppress(Exception):
                messages = str(collect(reset=True) or "").strip()
                if messages:
                    for line in dict.fromkeys(messages.splitlines()):
                        if line.strip():
                            log.debug("MuPDF: %s", line.strip())


def rasterize_pdf(
    pdf: Path,
    out_dir: Path,
    settings: RenderSettings,
    progress: ProgressFn | None = None,
    cancel: CancelFn | None = None,
) -> list[Path]:
    """Rasterise each PDF page to ``out_dir/slide_NNN.png`` at exactly the target size."""
    import pymupdf

    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    with _quiet_mupdf(), pymupdf.open(str(pdf)) as doc:  # type: ignore[no-untyped-call]
        total = doc.page_count
        for i, page in enumerate(doc):
            if cancel and cancel():
                raise Cancelled("Render cancelled")
            rect = page.rect
            zoom = min(settings.width / rect.width, settings.height / rect.height)
            # Render slightly above target for a cleaner downscale.
            mat = pymupdf.Matrix(zoom * 1.25, zoom * 1.25)  # type: ignore[no-untyped-call]
            # annots=False: a slide render is a still picture, and PDF annotations here are the
            # interactive bits LibreOffice writes for embedded media (Screen annotations), which
            # MuPDF cannot rasterise. Verified pixel-identical to annots=True on a deck with an
            # embedded video, minus the "cannot create appearance stream" errors it emits.
            pix = page.get_pixmap(matrix=mat, alpha=False, annots=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            framed = letterbox(img, settings.width, settings.height, settings.background)
            target = out_dir / f"slide_{i + 1:03d}.png"
            framed.save(target, format="PNG", optimize=False, compress_level=3)
            paths.append(target)
            if progress:
                progress(i + 1, total, f"Rendered slide {i + 1}/{total}")
    return paths


def preview_thumbnails(
    deck: Path,
    out_dir: Path,
    soffice: Path,
    expected_slides: int,
    is_cancelled: CancelFn | None = None,
) -> list[Path]:
    """Low-res (~384 px wide) PNG per slide of the raw deck, for the table before a render.

    Unlike :func:`rasterize_pdf` there is no letterboxing or target resolution - these are
    cheap stand-ins cached by deck mtime+size in ``out_dir/meta.json``, so a saved deck
    invalidates them but a reopen does not reconvert.
    """
    import pymupdf

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stat = Path(deck).stat()
    meta_path = out_dir / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        meta = None
    if (
        isinstance(meta, dict)
        and meta.get("deck_mtime") == stat.st_mtime
        and meta.get("deck_size") == stat.st_size
        and meta.get("count") == expected_slides
    ):
        cached = [out_dir / f"slide_{i + 1:03d}.png" for i in range(expected_slides)]
        if expected_slides and all(p.is_file() for p in cached):
            return cached
    pdf_dir = out_dir / "_pdf"  # convert inside out_dir: the PDF is scratch, not a deliverable
    if pdf_dir.exists():
        shutil.rmtree(pdf_dir, ignore_errors=True)
    pdf = convert_to_pdf(deck, pdf_dir, soffice, cancel=is_cancelled)
    # Drop stale previews first: a re-saved deck may have fewer slides than the last preview.
    for stale in out_dir.glob("slide_*.png"):
        stale.unlink()
    paths: list[Path] = []
    try:
        with _quiet_mupdf(), pymupdf.open(str(pdf)) as doc:  # type: ignore[no-untyped-call]
            for i, page in enumerate(doc):
                if is_cancelled and is_cancelled():
                    raise Cancelled("Preview cancelled")
                zoom = 384.0 / max(page.rect.width, 1.0)
                mat = pymupdf.Matrix(zoom, zoom)  # type: ignore[no-untyped-call]
                pix = page.get_pixmap(matrix=mat, alpha=False, annots=False)
                target = out_dir / f"slide_{i + 1:03d}.png"
                pix.save(str(target))
                paths.append(target)
    finally:
        shutil.rmtree(pdf_dir, ignore_errors=True)
    atomic_write_json(
        meta_path,
        {"deck_mtime": stat.st_mtime, "deck_size": stat.st_size, "count": len(paths)},
    )
    return paths


@dataclass
class RenderResult:
    directory: Path
    pngs: list[Path]
    from_cache: bool
    key: str


def render_deck(
    deck: Path,
    deck_hash: str,
    cache_root: Path,
    settings: RenderSettings,
    expected_slides: int | None = None,
    progress: ProgressFn | None = None,
    cancel: CancelFn | None = None,
) -> RenderResult:
    """Render (or reuse) all slides of a deck as PNGs."""
    key = render_key(deck_hash, settings.width, settings.height, settings.background)
    out_dir = cache_root / key
    marker = out_dir / "done.json"
    if marker.is_file():
        try:
            info = json.loads(marker.read_text(encoding="utf-8"))
            pngs = [out_dir / n for n in info["pngs"]]
            if all(p.is_file() for p in pngs) and (
                expected_slides is None or len(pngs) == expected_slides
            ):
                log.info("Using cached renders (%d slides) from %s", len(pngs), out_dir)
                return RenderResult(out_dir, pngs, True, key)
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            log.warning("Ignoring unreadable render marker %s: %s", marker, exc)

    soffice = find_soffice(settings.soffice_path)
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    work = out_dir.with_name(out_dir.name + ".tmp")
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    if progress:
        progress(0, 1, "Converting deck to PDF (LibreOffice)")
    pdf = convert_to_pdf(deck, work, soffice, cancel=cancel)
    pngs = rasterize_pdf(pdf, work, settings, progress, cancel)
    if expected_slides is not None and len(pngs) != expected_slides:
        shutil.rmtree(work, ignore_errors=True)
        raise RenderError(
            f"LibreOffice exported {len(pngs)} pages but the deck has {expected_slides} slides. "
            "Check for hidden-slide or custom-show settings in the deck."
        )
    atomic_write_json(
        work / "done.json",
        {
            "deck": str(deck),
            "deck_visual_sha256": deck_hash,
            "width": settings.width,
            "height": settings.height,
            "pngs": [p.name for p in pngs],
        },
    )
    retry_locked_io(lambda: os.replace(work, out_dir), f"publish renders to {out_dir.name}")
    return RenderResult(out_dir, [out_dir / p.name for p in pngs], False, key)


def blend_frames(img_a: Path, img_b: Path, out_dir: Path, prefix: str, n: int) -> list[Path]:
    """Write ``n`` cross-dissolve frames between two images (cached by filename)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    targets = [out_dir / f"{prefix}_{k:02d}.png" for k in range(n)]
    if all(t.is_file() for t in targets):
        return targets
    a = np.asarray(Image.open(img_a).convert("RGB"), dtype=np.float32)
    b = np.asarray(Image.open(img_b).convert("RGB"), dtype=np.float32)
    if a.shape != b.shape:
        raise RenderError(f"Cannot dissolve images of different sizes: {img_a} vs {img_b}")
    for k, target in enumerate(targets):
        alpha = (k + 1) / (n + 1)
        mixed = (a * (1.0 - alpha) + b * alpha).round().clip(0, 255).astype(np.uint8)
        Image.fromarray(mixed).save(target, format="PNG", compress_level=3)
    return targets
