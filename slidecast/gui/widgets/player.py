"""Audio preview player: one ``Audio`` object in the page, driven from Python.

Playback happens where the user is - in the webview or browser - so the page fetches the WAV over
the media route and reports its state back (playing / stopped) as a custom event.
"""

from __future__ import annotations

import json
from pathlib import Path

from nicegui import ui
from nicegui.events import GenericEventArguments

from slidecast.gui import media
from slidecast.gui.signals import Signal

# Swapping the source of a playing element fires "pause" for the old one; `switching` keeps that
# from being reported as a stop of the new source.
_SCRIPT = """
<script>
window.scPlayer = window.scPlayer || (() => {
  const audio = new Audio();
  let switching = false;
  const send = (state) => { if (window.emitEvent) emitEvent('sc_audio', state); };
  audio.addEventListener('playing', () => { switching = false; send('playing'); });
  audio.addEventListener('pause', () => { if (!switching) send('stopped'); });
  audio.addEventListener('ended', () => send('stopped'));
  audio.addEventListener('error', () => {
    if (audio.getAttribute('src')) { switching = false; send('error'); }
  });
  return {
    play(url) {
      switching = true;
      audio.src = url;
      audio.play().catch(() => { switching = false; send('error'); });
    },
    stop() {
      switching = false;
      audio.pause();
      audio.removeAttribute('src');
      audio.load();
      send('stopped');
    },
  };
})();
</script>
"""


class Player:
    def __init__(self) -> None:
        self.current: Path | None = None
        self._playing = False
        self.changed = Signal()  # playback state or source changed
        self._client = ui.context.client
        ui.add_body_html(_SCRIPT)
        ui.on("sc_audio", self._on_event)

    def play(self, path: Path) -> None:
        self.current = Path(path)
        self._playing = False
        self._client.run_javascript(f"scPlayer.play({json.dumps(media.audio_url(self.current))})")
        self.changed.emit()

    def stop(self) -> None:
        if self.current is None and not self._playing:
            return
        self.current = None
        self._playing = False
        self._client.run_javascript("scPlayer.stop()")
        self.changed.emit()

    def toggle(self, path: Path) -> None:
        if self.is_playing() and self.current == Path(path):
            self.stop()
        else:
            self.play(path)

    def is_playing(self) -> bool:
        return self._playing and self.current is not None

    def _on_event(self, e: GenericEventArguments) -> None:
        state = e.args if isinstance(e.args, str) else str(e.args)
        if state == "error":
            ui.notify("Could not play this audio file.", type="warning")
        if state == "playing":
            self._playing = True
        else:
            self._playing = False
            self.current = None
        self.changed.emit()
