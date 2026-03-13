"""Tests for ``rttr._state`` — shared singleton state."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from rttr import _state
from rttr.session_data import DebugLogger
from rttr.video_recorder import SessionRecorder


class TestRecorderSingleton:
    """Verify the module-level recorder singleton."""

    def test_recorder_is_session_recorder(self) -> None:
        assert isinstance(_state.recorder, SessionRecorder)

    def test_recorder_not_recording_initially(self) -> None:
        assert not _state.recorder.is_recording


class TestInitDebugLogger:
    """Verify ``init_debug_logger`` creates and reuses a ``DebugLogger``."""

    def test_creates_debug_logger(self, tmp_path: Path) -> None:
        _state.debug_logger = None
        with patch("rttr.session_data.DEBUG_SESSIONS_ROOT", tmp_path):
            dl = _state.init_debug_logger()
        assert isinstance(dl, DebugLogger)
        assert _state.debug_logger is dl

    def test_returns_existing_if_already_set(self, tmp_path: Path) -> None:
        _state.debug_logger = None
        with patch("rttr.session_data.DEBUG_SESSIONS_ROOT", tmp_path):
            first = _state.init_debug_logger()
            second = _state.init_debug_logger()
        assert first is second


class TestCollections:
    """Verify shared mutable containers."""

    def test_test_outputs_is_dict(self) -> None:
        assert isinstance(_state.test_outputs, dict)

    def test_collected_nodeids_is_list(self) -> None:
        assert isinstance(_state.collected_nodeids, list)

    def test_session_log_entries_is_list(self) -> None:
        assert isinstance(_state.session_log_entries, list)
