"""Palette and stylesheet: Quasar's dark theme recoloured, plus a few ``sc-`` classes."""

from __future__ import annotations

from nicegui import ui

TOKENS = {
    "bg": "#0D0E11",  # near-black window background
    "surface": "#15171B",  # elevated surfaces (cards, table, inputs)
    "surface2": "#1B1E24",  # hover / pressed surfaces
    "border": "#23262D",  # 1 px hairlines
    "text": "#E7E9EE",  # primary text
    "muted": "#8A9099",  # secondary text
    "accent": "#4C8DFF",  # the one accent
    "accent2": "#3A78E8",  # accent pressed
    "green": "#3DBE7A",
    "amber": "#E0A526",
    "red": "#E5534B",
}

STATE_COLORS = {
    "pending": "muted",
    "cached": "green",
    "generating": "amber",
    "done": "green",
    "error": "red",
    "skipped": "muted",
}

_VARS = "\n".join(f"  --sc-{k}: {v};" for k, v in TOKENS.items())

CSS = (
    ":root {\n"
    + _VARS
    + "\n}\n"
    + """
body, .q-page, .nicegui-content {
  background: var(--sc-bg);
  color: var(--sc-text);
}
body {
  font-family: Inter, "Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif;
  font-size: 13px;
  font-feature-settings: "tnum";
}
.nicegui-content { padding: 20px 24px; gap: 12px; }
.sc-view { row-gap: 12px !important; }
/* While the window measures itself: collapsed sections count at full height. */
.sc-measure .q-expansion-item__content { display: block !important; }

/* ---- text roles ---------------------------------------------------------- */
.sc-title { font-size: 20px; font-weight: 600; line-height: 1.3; }
.sc-panel-title { font-size: 15px; font-weight: 600; }
.sc-subtitle { font-size: 14px; color: var(--sc-muted); }
.sc-muted { color: var(--sc-muted); }
.sc-section {
  font-size: 11px; font-weight: 600; letter-spacing: 1px; color: var(--sc-muted);
  text-transform: uppercase;
}
.sc-mono { font-family: "Cascadia Mono", Consolas, monospace; }
.sc-ok { color: var(--sc-green); }
.sc-warn { color: var(--sc-amber); white-space: pre-line; }
.sc-error { color: var(--sc-red); white-space: pre-line; }
.sc-ellipsis { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }

/* ---- surfaces ------------------------------------------------------------ */
.sc-card {
  background: var(--sc-surface);
  border: 1px solid var(--sc-border);
  border-radius: 8px;
  padding: 12px 16px 16px;
}
.sc-dropzone {
  border: 2px dashed var(--sc-border);
  border-radius: 12px;
  min-height: 220px;
  transition: border-color .15s, background .15s;
}
.sc-dropzone.active { border-color: var(--sc-accent); background: rgba(76, 141, 255, .06); }
.sc-divider { border-top: 1px solid var(--sc-border); width: 100%; }

/* ---- inputs -------------------------------------------------------------- */
/* Quasar dashes the outline of read-only fields; here they are path displays filled by a
   Browse button, not disabled inputs, so they keep the ordinary solid hairline. */
.q-field--readonly.q-field--outlined .q-field__control:before {
  border-style: solid;
  border-color: rgba(255, 255, 255, .24);
}
.q-field--outlined .q-field__control { border-radius: 6px; }

/* ---- chrome -------------------------------------------------------------- */
.sc-menubar {
  background: var(--sc-surface) !important;
  border-bottom: 1px solid var(--sc-border);
  padding: 0 8px; min-height: 34px;
}
.sc-sidebar { background: var(--sc-surface) !important; }
.sc-brand { font-size: 18px; font-weight: 600; }
.sc-step {
  border-radius: 8px; padding: 10px 12px; cursor: pointer; height: 64px;
  border: 1px solid transparent; flex-wrap: nowrap;
}
.sc-step:hover { background: var(--sc-surface2); }
.sc-step.active { background: rgba(76, 141, 255, .12); border-color: rgba(76, 141, 255, .35); }
.sc-step-num {
  width: 26px; height: 26px; min-width: 26px; border-radius: 13px;
  border: 1px solid var(--sc-border); display: flex; align-items: center;
  justify-content: center; font-size: 12px; color: var(--sc-muted);
}
.sc-step.active .sc-step-num {
  background: var(--sc-accent); border-color: var(--sc-accent); color: #fff;
}
.sc-step-title { font-weight: 600; }
.sc-step-hint { font-size: 12px; color: var(--sc-muted); }
.sc-footer {
  background: var(--sc-surface) !important; color: var(--sc-text) !important;
  border-top: 1px solid var(--sc-border);
}
.sc-log .q-scrollarea, .sc-log { background: var(--sc-bg); }
.sc-log { font-family: "Cascadia Mono", Consolas, monospace; font-size: 12px; height: 200px; }

/* ---- badges -------------------------------------------------------------- */
.sc-badge {
  border-radius: 999px; padding: 2px 10px; font-size: 12px; white-space: nowrap;
  display: inline-block; text-align: center; min-width: 64px;
}
.sc-badge-muted, .sc-badge-idle, .sc-badge-neutral {
  background: rgba(138,144,153,.14); color: var(--sc-muted);
}
.sc-badge-green, .sc-badge-success, .sc-badge-ready {
  background: rgba(61,190,122,.14); color: var(--sc-green);
}
.sc-badge-amber, .sc-badge-running, .sc-badge-paused, .sc-badge-warning {
  background: rgba(224,165,38,.14); color: var(--sc-amber);
}
.sc-badge-red, .sc-badge-error { background: rgba(229,83,75,.14); color: var(--sc-red); }
.sc-badge-accent { background: rgba(76,141,255,.14); color: var(--sc-accent); }

/* ---- tables -------------------------------------------------------------- */
.sc-table { background: var(--sc-surface); border: 1px solid var(--sc-border); border-radius: 8px; }
.sc-table thead tr th {
  position: sticky; top: 0; z-index: 1; background: var(--sc-surface);
  color: var(--sc-muted); font-weight: 600;
}
.sc-table tbody tr { cursor: default; }
.sc-table tbody tr:hover td { background: var(--sc-surface2); }
.sc-table tbody tr.selected td { background: rgba(76, 141, 255, .14); }
.sc-table tbody tr.excluded td { color: var(--sc-muted); }
.sc-table td { border-bottom-color: var(--sc-border) !important; }
.sc-thumb { width: 128px; height: 72px; object-fit: contain; display: block; border-radius: 4px; }
.sc-thumb-empty { background: var(--sc-surface2); }
.sc-notes-cell {
  max-width: 0; width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sc-override { color: var(--sc-accent); }

/* ---- narration bar ------------------------------------------------------- */
.sc-bar {
  height: 8px; border-radius: 4px; overflow: hidden; background: var(--sc-surface2);
  display: flex; width: 100%;
}
.sc-bar > div { height: 100%; }
.sc-dot::before { content: "\\25CF"; margin-right: 6px; color: var(--dot, currentColor); }
"""
)

# Browser shortcuts that would otherwise reload the page or open a browser dialog; the app binds
# these keys itself (see MainWindow._on_key).
KEY_GUARD = """
<script>
document.addEventListener('keydown', (e) => {
  const k = (e.key || '').toLowerCase();
  if (e.key === 'F5' || (e.ctrlKey && ['o', 'r', 'e', 'p', 'j', 'l', 'q'].includes(k))) {
    e.preventDefault();
  }
}, true);
</script>
"""


def apply_theme() -> None:
    """Call once per page, before building it."""
    ui.colors(
        primary=TOKENS["accent"],
        positive=TOKENS["green"],
        negative=TOKENS["red"],
        warning=TOKENS["amber"],
        dark=TOKENS["surface"],
        dark_page=TOKENS["bg"],
    )
    ui.dark_mode(True)
    ui.add_css(CSS)
    ui.add_head_html(KEY_GUARD)
