"""Screen recording lifecycle for the RTTR pytest plugin.

Manages starting/stopping the ffmpeg-based ``SessionRecorder``, emitting
per-test markers, embedding chapter metadata, and extracting failure frames.
"""

from __future__ import annotations

import logging

import pytest

from rttr.video_recorder import embed_test_chapters, extract_failure_frames
from rttr import _state

logger = logging.getLogger(__name__)


def start(session: pytest.Session) -> None:
    """Start the screen recorder if recording is enabled.

    Must be called after ``_state.init_debug_logger()`` so the session
    directory is available.

    Args:
        session: The pytest Session object (used for CLI options).
    """
    if _state.debug_logger is None:
        return
    if not session.config.getoption("--no-record", default=False):
        _state.recorder.start(_state.debug_logger.session_dir)


def stop(session: pytest.Session) -> None:
    """Stop the recorder and embed chapters / extract failure frames.

    Args:
        session: The pytest Session object (used for failure count).
    """
    had_failures = session.testsfailed > 0
    _state.recorder.stop(had_failures=had_failures)

    if _state.debug_logger is None:
        return

    session_dir = _state.debug_logger.session_dir
    video = session_dir / "session_recording.mp4"
    markers = session_dir / "session_recording_markers.jsonl"

    # Embed chapter markers (one per test) into the MP4 for easy navigation.
    if video.exists() and markers.exists():
        try:
            embed_test_chapters(video, markers)
        except Exception:  # noqa: BLE001 — best-effort, never block pytest exit
            logger.warning("[VIDEO] Chapter embedding failed (best-effort).")

    # Extract video frames around each failed test for agent analysis.
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
    _state.recorder.mark_test_start(nodeid)


def mark_test_end(nodeid: str, outcome: str) -> None:
    """Record a test-end marker in the video sidecar.

    Args:
        nodeid: The pytest node ID of the test.
        outcome: The outcome string (``"passed"``, ``"failed"``, etc.).
    """
    _state.recorder.mark_test_end(nodeid, outcome)
