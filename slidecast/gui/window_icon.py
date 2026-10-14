"""Crisp window icon for the native (pywebview) window on Windows.

NiceGUI sets the icon itself, but loads the .ico without asking for a size, so Windows takes a
single frame and stretches it into the title bar and the taskbar. Here the two slots get a frame
each at the window's own DPI: the small one (title bar, 20 px at 125%) and the big one (taskbar,
Alt+Tab, 40 px at 125%), each picked from the .ico's own renderings.

``apply_window_icon`` runs in the window process: it is handed to ``webview.start(func=...)``, so it
has to stay a module-level function (it is pickled by reference into that process).
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from slidecast.core.logging_setup import get_logger

log = get_logger("gui")

WM_SETICON = 0x0080
ICON_SMALL, ICON_BIG = 0, 1
SM_CXICON, SM_CXSMICON = 11, 49
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
USER_DEFAULT_DPI = 96


def icon_sizes(dpi: int) -> tuple[int, int]:
    """(small, big) icon edge in pixels for a window at ``dpi``."""
    user32 = ctypes.windll.user32
    try:
        metric = user32.GetSystemMetricsForDpi  # Windows 10 1607+
        return metric(SM_CXSMICON, dpi), metric(SM_CXICON, dpi)
    except AttributeError:
        scale = dpi / USER_DEFAULT_DPI
        return round(16 * scale), round(32 * scale)


def set_icon(hwnd: int, icon_path: str) -> bool:
    """Load both icon slots at the window's DPI and hand them to the window."""
    user32 = ctypes.windll.user32
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.LoadImageW.argtypes = [
        wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]  # fmt: skip
    user32.SendMessageW.restype = wintypes.LPARAM
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    try:
        dpi = user32.GetDpiForWindow(hwnd) or USER_DEFAULT_DPI
    except AttributeError:
        dpi = USER_DEFAULT_DPI
    small, big = icon_sizes(dpi)
    ok = True
    for slot, size in ((ICON_SMALL, small), (ICON_BIG, big)):
        # The handles stay alive for the life of the window: destroying them early makes the
        # taskbar fall back to the default icon.
        hicon = user32.LoadImageW(None, icon_path, IMAGE_ICON, size, size, LR_LOADFROMFILE)
        if hicon:
            user32.SendMessageW(hwnd, WM_SETICON, slot, hicon)
        else:
            ok = False
    return ok


def apply_window_icon(icon_path: str) -> None:
    """``webview.start`` hook: wait for the window to appear, then set its icon."""
    if sys.platform != "win32":
        return
    import webview

    if not webview.windows:
        return
    window = webview.windows[0]
    window.events.shown.wait(60)
    # Typed as None in pywebview; at runtime it is the WinForms form once the window is shown.
    native = getattr(window, "native", None)
    if native is None:
        return
    try:
        set_icon(int(native.Handle.ToInt64()), icon_path)
    except Exception as exc:  # cosmetic: never take the window down over an icon
        log.warning("Could not set the window icon: %s", exc)
