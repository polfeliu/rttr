"""Shared singleton state for the RTTR session outputs.

All cross-module state lives in :class:`SessionState` so that
session_outputs sub-modules can read/write the same objects without
circular imports.  The module-level ``session`` instance is the single
source of truth.
"""

from __future__ import annotations

import atexit
from typing import TYPE_CHECKING

from rttr.session_outputs.video_recorder import SessionRecorder

if TYPE_CHECKING:
    from rttr.session_outputs._types import LogEntry, TestOutput
    from rttr.session_outputs.debug_logger import DebugLogger


class SessionState:
    """Encapsulates all mutable state for a single pytest session."""

    def __init__(self) -> None:
        self.recorder: SessionRecorder = SessionRecorder()
        self.debug_logger: DebugLogger | None = None
        self.test_outputs: dict[str, TestOutput] = {}
        self.collected_nodeids: list[str] = []
        self.session_log_entries: list[LogEntry] = []
        self.collect_only: bool = False

    def reset(self) -> None:
        """Reset mutable collections for a fresh session.

        Does **not** recreate the recorder — call between pytester runs.
        """
        self.debug_logger = None
        self.test_outputs.clear()
        self.collected_nodeids.clear()
        self.session_log_entries.clear()
        self.collect_only = False

    def init_debug_logger(self) -> DebugLogger:
        """Create (or return) the session-wide DebugLogger.

        Returns:
            The instantiated DebugLogger.
        """
        from rttr.session_outputs.debug_logger import DebugLogger as _DebugLogger  # noqa: PLC0415

        if self.debug_logger is None:
            self.debug_logger = _DebugLogger()
        return self.debug_logger


# ---------------------------------------------------------------------------
# Module-level singleton — import ``session`` from this module.
# ---------------------------------------------------------------------------

session = SessionState()
atexit.register(session.recorder.kill)
