"""Real-time screen overlays for POM assertion results.

Draws a brief green (PASS) or red (FAIL) border around the asserted
element's screen rectangle when ``BaseModel._assert()`` fires.

The border is 50% transparent, click-through, and auto-dismisses after
a short duration so it never blocks UIA or user input.

Requires ``pywin32`` (already a pywinauto dependency).
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import threading
import time as _time

import win32con
import win32gui

logger = logging.getLogger(__name__)

_GREEN = 0x0000FF00  # COLORREF (BGR)
_RED = 0x000000FF
_BG_KEY = 0x00010101  # near-black used as transparency key
_OVERLAY_DURATION_S = 0.6


def _create_overlay_window(
    left: int,
    top: int,
    right: int,
    bottom: int,
    colour_ref: int,
    duration: float = _OVERLAY_DURATION_S,
) -> None:
    """Create a transparent, topmost, click-through border overlay.

    Runs on a background daemon thread — never blocks the test.
    """
    width = right - left
    height = bottom - top
    if width < 10 or height < 10:
        return

    def _run() -> None:
        try:
            class_name = f"RTTROverlay_{threading.current_thread().ident}"
            wc = win32gui.WNDCLASS()
            wc.lpszClassName = class_name  # type: ignore[misc]
            # Solid brush in the key colour — becomes fully transparent
            bg_brush = win32gui.CreateSolidBrush(_BG_KEY)
            wc.hbrBackground = bg_brush  # type: ignore[assignment,misc]
            wc.lpfnWndProc = {  # type: ignore[misc]
                win32con.WM_DESTROY: lambda _hwnd, _msg, _wp, _lp: 0,
            }
            with contextlib.suppress(Exception):
                win32gui.RegisterClass(wc)

            ex_style = (
                win32con.WS_EX_LAYERED
                | win32con.WS_EX_TRANSPARENT
                | win32con.WS_EX_TOPMOST
                | win32con.WS_EX_TOOLWINDOW
                | win32con.WS_EX_NOACTIVATE
            )

            hwnd = win32gui.CreateWindowEx(
                ex_style,
                class_name,
                "",
                win32con.WS_POPUP | win32con.WS_VISIBLE,
                left,
                top,
                width,
                height,
                0,
                0,
                0,
                None,
            )

            # Color-key transparency: _BG_KEY becomes invisible, border remains
            win32gui.SetLayeredWindowAttributes(hwnd, _BG_KEY, 0, win32con.LWA_COLORKEY)

            # Fill entire window with the key color first (makes interior transparent)
            hdc = win32gui.GetDC(hwnd)
            key_brush = win32gui.CreateSolidBrush(_BG_KEY)
            win32gui.FillRect(hdc, (0, 0, width, height), key_brush)  # type: ignore[arg-type]
            win32gui.DeleteObject(key_brush)

            # Draw dashed border on top (8px on, 6px off, 3px wide)
            gdi32 = ctypes.windll.gdi32
            style = 7 | win32con.PS_GEOMETRIC | win32con.PS_ENDCAP_FLAT  # PS_USERSTYLE
            logbrush = (ctypes.c_uint * 3)(0, colour_ref, 0)  # BS_SOLID, color, no hatch
            dashes = (ctypes.c_uint32 * 2)(8, 6)
            pen = gdi32.ExtCreatePen(style, 3, ctypes.byref(logbrush), 2, ctypes.byref(dashes))
            old_pen = win32gui.SelectObject(hdc, pen)
            brush = win32gui.GetStockObject(win32con.HOLLOW_BRUSH)
            old_brush = win32gui.SelectObject(hdc, brush)
            win32gui.Rectangle(hdc, 0, 0, width, height)
            win32gui.SelectObject(hdc, old_pen)
            win32gui.SelectObject(hdc, old_brush)
            win32gui.DeleteObject(pen)
            win32gui.ReleaseDC(hwnd, hdc)

            _time.sleep(duration)

            with contextlib.suppress(Exception):
                win32gui.DestroyWindow(hwnd)
            with contextlib.suppress(Exception):
                win32gui.UnregisterClass(class_name, 0)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 — overlay is best-effort
            logger.debug("[OVERLAY] Failed to create assertion overlay", exc_info=True)

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def show_assertion(
    *,
    passed: bool,
    rect: tuple[int, int, int, int] | None,
) -> None:
    """Show a brief border overlay on-screen at the given rect.

    Args:
        passed: True for PASS (green), False for FAIL (red).
        rect: (left, top, right, bottom) screen coordinates.
            If ``None``, no overlay is shown.
    """
    if rect is None:
        return
    left, top, right, bottom = rect
    colour = _GREEN if passed else _RED
    _create_overlay_window(left, top, right, bottom, colour)
