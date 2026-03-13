"""Emergency stop hotkey (F10) for the RTTR pytest plugin.

Pressing F10 at any time during a test run will interrupt the main thread,
causing pytest to abort after the current test.
"""

from __future__ import annotations

import _thread
import logging

import keyboard

logger = logging.getLogger(__name__)


def _emergency_stop() -> None:
    """Callback invoked when F10 is pressed."""
    logger.critical("[EMERGENCY STOP] F10 pressed! Stopping after current test.")
    _thread.interrupt_main()


def register() -> None:
    """Bind the F10 hotkey to ``_emergency_stop``.

    Silently logs a warning if the hotkey cannot be bound (e.g. running
    without a desktop session).
    """
    try:
        keyboard.add_hotkey("f10", _emergency_stop)
    except (OSError, ValueError) as exc:
        logger.warning("Could not bind F10 safety net: %s", exc)
