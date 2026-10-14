"""Serve local audio and slide thumbnails to the page.

The page runs in a webview (or a browser), so a WAV on disk has to reach it over HTTP. Only files
the app itself registered are served, under an opaque token - the routes never take a path.
"""

from __future__ import annotations

import hashlib
import io
import threading
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse, Response
from nicegui import app

from slidecast.core.logging_setup import get_logger

log = get_logger("gui")

THUMB_W, THUMB_H = 256, 144  # 2x the displayed size, so HiDPI screens stay sharp
_MAX_CACHED_THUMBS = 2000

_files: dict[str, Path] = {}
_thumbs: dict[tuple[str, int], bytes] = {}
_lock = threading.Lock()


def _register(path: Path) -> tuple[str, int]:
    p = Path(path).resolve()
    token = hashlib.sha1(str(p).encode("utf-8")).hexdigest()[:24]
    with _lock:
        _files[token] = p
    try:
        version = p.stat().st_mtime_ns
    except OSError:
        version = 0
    return token, version


def audio_url(path: Path) -> str:
    token, version = _register(path)
    return f"/slidecast/media/{token}?v={version}"


def thumb_url(path: Path) -> str:
    token, version = _register(path)
    return f"/slidecast/thumb/{token}?v={version}"


def _lookup(token: str) -> Path:
    with _lock:
        path = _files.get(token)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404)
    return path


def _scaled_png(path: Path) -> bytes:
    from PIL import Image

    with Image.open(path) as img:
        img.thumbnail((THUMB_W, THUMB_H))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG", optimize=False)
        return buf.getvalue()


def register_routes() -> None:
    @app.get("/slidecast/media/{token}")
    def _media(token: str) -> FileResponse:
        return FileResponse(_lookup(token), headers={"Cache-Control": "no-store"})

    @app.get("/slidecast/thumb/{token}")
    def _thumb(token: str) -> Response:
        path = _lookup(token)
        key = (token, path.stat().st_mtime_ns)
        with _lock:
            data = _thumbs.get(key)
        if data is None:
            try:
                data = _scaled_png(path)
            except OSError as exc:
                log.warning("Thumbnail %s unreadable: %s", path.name, exc)
                raise HTTPException(status_code=404) from exc
            with _lock:
                if len(_thumbs) >= _MAX_CACHED_THUMBS:
                    _thumbs.clear()
                _thumbs[key] = data
        # The URL carries the file's mtime, so a cached copy can never be stale.
        return Response(data, media_type="image/png", headers={"Cache-Control": "max-age=31536000"})
