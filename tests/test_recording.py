"""Tests for ``rttr.session_outputs`` — screen recording lifecycle orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from rttr.session_outputs import (
    _state,
    mark_test_end,
    mark_test_start,
    start_recording,
    stop_recording,
)

if TYPE_CHECKING:
    import pytest


class TestStart:
    """Verify ``recording.start`` delegates to the recorder."""

    def test_noop_when_no_debug_logger(self) -> None:
        _state.session.debug_logger = None
        session = MagicMock()
        start_recording(session)
        # No crash — that's the contract.

    def test_starts_recorder_when_recording_enabled(self, tmp_path: pytest.TempPathFactory) -> None:
        dl = MagicMock()
        dl.session_dir = tmp_path
        _state.session.debug_logger = dl

        session = MagicMock()
        session.config.getoption.return_value = False  # --no-record = False

        with patch.object(_state.session.recorder, "start") as mock_start:
            start_recording(session)
            mock_start.assert_called_once_with(tmp_path)

    def test_does_not_start_when_no_record_flag(self) -> None:
        dl = MagicMock()
        _state.session.debug_logger = dl

        session = MagicMock()
        session.config.getoption.return_value = True  # --no-record = True

        with patch.object(_state.session.recorder, "start") as mock_start:
            start_recording(session)
            mock_start.assert_not_called()


class TestStop:
    """Verify ``recording.stop`` stops the recorder and processes artifacts."""

    def test_calls_recorder_stop(self) -> None:
        session = MagicMock()
        session.testsfailed = 0

        with patch.object(_state.session.recorder, "stop") as mock_stop:
            stop_recording(session)
            mock_stop.assert_called_once_with(had_failures=False)

    def test_embeds_chapters_when_files_exist(self, tmp_path: pytest.TempPathFactory) -> None:
        dl = MagicMock()
        dl.session_dir = tmp_path
        _state.session.debug_logger = dl

        # Create the expected files.
        (tmp_path / "session_recording.mp4").write_bytes(b"fake")
        (tmp_path / "session_recording_markers.jsonl").write_text("{}", encoding="utf-8")

        session = MagicMock()
        session.testsfailed = 0

        with (
            patch.object(_state.session.recorder, "stop"),
            patch("rttr.session_outputs.embed_test_chapters") as mock_embed,
            patch("rttr.session_outputs.extract_failure_frames"),
        ):
            stop_recording(session)
            mock_embed.assert_called_once()

    def test_extracts_frames_on_failure(self, tmp_path: pytest.TempPathFactory) -> None:
        dl = MagicMock()
        dl.session_dir = tmp_path
        _state.session.debug_logger = dl

        # Create the expected files.
        (tmp_path / "session_recording.mp4").write_bytes(b"fake")
        (tmp_path / "session_recording_markers.jsonl").write_text("{}", encoding="utf-8")

        session = MagicMock()
        session.testsfailed = 2

        with (
            patch.object(_state.session.recorder, "stop"),
            patch("rttr.session_outputs.embed_test_chapters"),
            patch("rttr.session_outputs.extract_failure_frames") as mock_extract,
        ):
            stop_recording(session)
            mock_extract.assert_called_once()

    def test_swallows_chapter_embedding_exception(self, tmp_path: pytest.TempPathFactory) -> None:
        dl = MagicMock()
        dl.session_dir = tmp_path
        _state.session.debug_logger = dl

        (tmp_path / "session_recording.mp4").write_bytes(b"fake")
        (tmp_path / "session_recording_markers.jsonl").write_text("{}", encoding="utf-8")

        session = MagicMock()
        session.testsfailed = 0

        with (
            patch.object(_state.session.recorder, "stop"),
            patch(
                "rttr.session_outputs.embed_test_chapters",
                side_effect=RuntimeError("boom"),
            ),
        ):
            stop_recording(session)  # should not raise

    def test_swallows_frame_extraction_exception(self, tmp_path: pytest.TempPathFactory) -> None:
        dl = MagicMock()
        dl.session_dir = tmp_path
        _state.session.debug_logger = dl

        (tmp_path / "session_recording.mp4").write_bytes(b"fake")
        (tmp_path / "session_recording_markers.jsonl").write_text("{}", encoding="utf-8")

        session = MagicMock()
        session.testsfailed = 1

        with (
            patch.object(_state.session.recorder, "stop"),
            patch("rttr.session_outputs.embed_test_chapters"),
            patch(
                "rttr.session_outputs.extract_failure_frames",
                side_effect=RuntimeError("boom"),
            ),
        ):
            stop_recording(session)  # should not raise


class TestMarkers:
    """Verify ``mark_test_start`` and ``mark_test_end`` delegate to the recorder."""

    def test_mark_test_start(self) -> None:
        with patch.object(_state.session.recorder, "mark_test_start") as mock:
            mark_test_start("test_a.py::test_one")
            mock.assert_called_once_with("test_a.py::test_one")

    def test_mark_test_end(self) -> None:
        with patch.object(_state.session.recorder, "mark_test_end") as mock:
            mark_test_end("test_a.py::test_one", "passed")
            mock.assert_called_once_with("test_a.py::test_one", "passed")
