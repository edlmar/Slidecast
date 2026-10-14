"""JSON persistence: recent projects, last-used folders, last-used project settings.

Lives in the per-user config folder (``%APPDATA%\\slidecast`` on Windows, ``~/.config/slidecast``
elsewhere), so it survives reinstalls and portable-folder moves alike.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from slidecast.core.config import ProjectSettings
from slidecast.core.logging_setup import get_logger

log = get_logger("gui")

MAX_RECENT = 8


def config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "slidecast"


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config_dir() / "gui.json"
        self._lock = threading.Lock()
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("Could not save GUI settings to %s: %s", self.path, exc)

    def _set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._save()

    # -- recent projects ------------------------------------------------------------------

    def recent_projects(self) -> list[Path]:
        raw = self._data.get("recent_projects", [])
        return [Path(str(p)) for p in raw if p] if isinstance(raw, list) else []

    def add_recent_project(self, path: Path) -> None:
        items = [p for p in self.recent_projects() if p != path]
        items.insert(0, path)
        self._set("recent_projects", [str(p) for p in items[:MAX_RECENT]])

    def last_dir(self, key: str) -> str:
        dirs = self._data.get("dirs", {})
        return str(dirs.get(key, "")) if isinstance(dirs, dict) else ""

    def set_last_dir(self, key: str, path: Path) -> None:
        dirs = dict(self._data.get("dirs", {}) or {})
        dirs[key] = str(path.parent if path.is_file() else path)
        self._set("dirs", dirs)

    # -- default project settings ---------------------------------------------------------

    def default_settings(self) -> ProjectSettings:
        raw = self._data.get("default_project_settings")
        if raw:
            try:
                return ProjectSettings.model_validate(raw)
            except (ValueError, TypeError):
                pass
        return ProjectSettings()

    def save_default_settings(self, settings: ProjectSettings) -> None:
        self._set("default_project_settings", json.loads(settings.model_dump_json()))
