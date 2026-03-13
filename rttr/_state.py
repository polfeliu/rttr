"""Shared singleton state for the RTTR (Release The T-Rex) pytest plugin.

All cross-module state lives here so that recording, session_data, hooks,
and the orchestrator in ``__init__`` can read/write the same objects without
circular imports.
"""

from __future__ import annotations

import atexit
from typing import TYPE_CHECKING

from rttr.video_recorder import SessionRecorder

if TYPE_CHECKING:
    from rttr.session_data import DebugLogger

# ---------------------------------------------------------------------------
# Screen recorder — started when the debug_logger creates the session dir
# ---------------------------------------------------------------------------

recorder = SessionRecorder()
atexit.register(recorder.kill)

# ---------------------------------------------------------------------------
# Debug logger — one folder per pytest invocation
# ---------------------------------------------------------------------------

debug_logger: DebugLogger | None = None


def init_debug_logger() -> DebugLogger:
    """Create (or return) the session-wide DebugLogger.

    Returns:
        The instantiated DebugLogger.
    """
    from rttr.session_data import DebugLogger as _DebugLogger  # noqa: PLC0415

    global debug_logger  # noqa: PLW0603
    if debug_logger is None:
        debug_logger = _DebugLogger()
    return debug_logger


# ---------------------------------------------------------------------------
# Per-test captured output (tracebacks, stdout, stderr) for session_data.json
# ---------------------------------------------------------------------------

test_outputs: dict[str, dict[str, str]] = {}

# ---------------------------------------------------------------------------
# All collected test nodeids (including those not run)
# ---------------------------------------------------------------------------

collected_nodeids: list[str] = []

# ---------------------------------------------------------------------------
# Timestamped log records for synced playback in the session viewer
# ---------------------------------------------------------------------------

session_log_entries: list[dict[str, object]] = []
