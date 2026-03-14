"""Tests for ``rttr._state`` — shared singleton state."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from rttr.session_outputs import _state
from rttr.session_outputs.debug_logger import DebugLogger
from rttr.session_outputs.video_recorder import SessionRecorder


class TestRecorderSingleton:
    """Verify the module-level recorder singleton."""

    def test_recorder_is_session_recorder(self) -> None:
        assert isinstance(_state.session.recorder, SessionRecorder)

    def test_recorder_not_recording_initially(self) -> None:
        assert not _state.session.recorder.is_recording


class TestInitDebugLogger:
    """Verify ``init_debug_logger`` creates and reuses a ``DebugLogger``."""

    def test_creates_debug_logger(self, tmp_path: Path) -> None:
        _state.session.debug_logger = None
        with patch("rttr.session_outputs.debug_logger.DEBUG_SESSIONS_ROOT", tmp_path):
            dl = _state.session.init_debug_logger()
        assert isinstance(dl, DebugLogger)
        assert _state.session.debug_logger is dl

    def test_returns_existing_if_already_set(self, tmp_path: Path) -> None:
        _state.session.debug_logger = None
        with patch("rttr.session_outputs.debug_logger.DEBUG_SESSIONS_ROOT", tmp_path):
            first = _state.session.init_debug_logger()
            second = _state.session.init_debug_logger()
        assert first is second


class TestCollections:
    """Verify shared mutable containers."""

    def test_test_outputs_is_dict(self) -> None:
        assert isinstance(_state.session.test_outputs, dict)

    def test_collected_nodeids_is_list(self) -> None:
        assert isinstance(_state.session.collected_nodeids, list)

    def test_session_log_entries_is_list(self) -> None:
        assert isinstance(_state.session.session_log_entries, list)


class TestReset:
    """Verify ``SessionState.reset`` clears mutable state."""

    def test_clears_all_collections(self) -> None:
        _state.session.test_outputs["test"] = "value"  # type: ignore[assignment]
        _state.session.collected_nodeids.append("nid")
        _state.session.session_log_entries.append("entry")  # type: ignore[arg-type]
        _state.session.debug_logger = "something"  # type: ignore[assignment]

        _state.session.reset()

        assert _state.session.test_outputs == {}
        assert _state.session.collected_nodeids == []
        assert _state.session.session_log_entries == []
        assert _state.session.debug_logger is None

    def test_preserves_recorder(self) -> None:
        original_recorder = _state.session.recorder
        _state.session.reset()
        assert _state.session.recorder is original_recorder
