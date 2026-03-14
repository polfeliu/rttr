"""Session outputs — coordinates recording, logging, and report generation.

This package manages all artifacts produced during a test session:

- **Video recording** — ``video_recorder.SessionRecorder``
- **Chapter embedding** — ``video_chapters.embed_test_chapters``
- **Session report** — ``session_report.generate`` (session_data.json + viewer.html)
- **Log capture** — ``_logger.SessionLogHandler``
- **Debug logger** — ``debug_logger.DebugLogger`` (session dir + per-test dirs)
- **Test outputs** — ``test_outputs.failure_frames`` (frame extraction)

The ``__init__`` module re-exports the public API and provides lifecycle
functions used by the RTTR pytest plugin hooks.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from rttr.session_outputs import _state
from rttr.session_outputs._logger import SessionLogHandler, attach_log_handler
from rttr.session_outputs._types import LogEntry, TestOutput, TestResult
from rttr.session_outputs.debug_logger import DebugLogger, _format_tree, _slugify
from rttr.session_outputs.session_report import capture_output, generate
from rttr.session_outputs.test_outputs.failure_frames import (
    extract_failure_frames,
    extract_frames_at,
)
from rttr.session_outputs.video_chapters import embed_test_chapters
from rttr.session_outputs.video_recorder import SessionRecorder

if TYPE_CHECKING:
    import pytest

logger = logging.getLogger(__name__)

__all__ = [
    "DebugLogger",
    "LogEntry",
    "SessionLogHandler",
    "SessionRecorder",
    "TestOutput",
    "TestResult",
    "_format_tree",
    "_slugify",
    "attach_log_handler",
    "capture_output",
    "embed_test_chapters",
    "extract_failure_frames",
    "extract_frames_at",
    "generate",
    "mark_test_end",
    "mark_test_start",
    "start_recording",
    "stop_recording",
]


# ---------------------------------------------------------------------------
# Recording lifecycle (replaces the old ``recording.py`` module)
# ---------------------------------------------------------------------------


def start_recording(session: pytest.Session) -> None:
    """Start the screen recorder if recording is enabled.

    Must be called after ``_state.session.init_debug_logger()`` so the session
    directory is available.

    Args:
        session: The pytest Session object.
    """
    if _state.session.debug_logger is None:
        return
    if not session.config.getoption("--no-record", default=False):
        _state.session.recorder.start(_state.session.debug_logger.session_dir)


def stop_recording(session: pytest.Session) -> None:
    """Stop the recorder and embed chapters / extract failure frames.

    Args:
        session: The pytest Session object.
    """
    had_failures = session.testsfailed > 0
    _state.session.recorder.stop(had_failures=had_failures)

    if _state.session.debug_logger is None:
        return

    session_dir = _state.session.debug_logger.session_dir
    video = session_dir / "session_recording.mp4"
    markers = session_dir / "session_recording_markers.jsonl"

    if video.exists() and markers.exists():
        try:
            embed_test_chapters(video, markers)
        except Exception:  # noqa: BLE001 — best-effort, never block pytest exit
            logger.warning("[VIDEO] Chapter embedding failed (best-effort).")

    if had_failures:
        frames_dir = session_dir / "failure_frames"
        try:
            extracted = extract_failure_frames(video, markers, frames_dir)
            if extracted:
                logger.info(
                    "[VIDEO] Extracted frames for %d failed test(s) into %s.",
                    len(extracted),
                    frames_dir,
                )
        except Exception:  # noqa: BLE001 — best-effort, never block pytest exit
            logger.warning("[VIDEO] Frame extraction failed (best-effort).")


def mark_test_start(nodeid: str) -> None:
    """Record a test-start marker in the video sidecar.

    Args:
        nodeid: The pytest node ID of the test.
    """
    _state.session.recorder.mark_test_start(nodeid)


def mark_test_end(nodeid: str, outcome: str) -> None:
    """Record a test-end marker in the video sidecar.

    Args:
        nodeid: The pytest node ID of the test.
        outcome: The outcome string.
    """
    _state.session.recorder.mark_test_end(nodeid, outcome)
