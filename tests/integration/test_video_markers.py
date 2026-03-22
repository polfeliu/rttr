"""Video markers — verify that test markers are written during real execution."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from rttr.session_outputs._state import SessionState
from rttr.session_outputs.video_recorder import SessionRecorder

if TYPE_CHECKING:
    from pathlib import Path


class TestVideoMarkers:
    """Test the JSONL marker file written by ``SessionRecorder``."""

    def test_markers_written_on_test_start(self, tmp_path: Path) -> None:
        state = SessionState()
        state.recorder = SessionRecorder()
        state.recorder._start_time = 1000.0
        state.recorder._markers_path = tmp_path / "markers.jsonl"
        state.recorder._is_recording = True

        state.recorder.mark_test_start("tests/test_foo.py::test_bar")

        text = state.recorder._markers_path.read_text(encoding="utf-8")
        entry = json.loads(text.strip())
        assert entry["event"] == "test_start"
        assert entry["test"] == "tests/test_foo.py::test_bar"
        assert "video_ts" in entry

    def test_markers_written_on_test_end(self, tmp_path: Path) -> None:
        state = SessionState()
        state.recorder = SessionRecorder()
        state.recorder._start_time = 1000.0
        state.recorder._markers_path = tmp_path / "markers.jsonl"
        state.recorder._is_recording = True

        state.recorder.mark_test_end("tests/test_foo.py::test_bar", "passed")

        text = state.recorder._markers_path.read_text(encoding="utf-8")
        entry = json.loads(text.strip())
        assert entry["event"] == "test_end"
        assert entry["result"] == "passed"

    def test_marker_timestamps_increase(self, tmp_path: Path) -> None:
        state = SessionState()
        state.recorder = SessionRecorder()
        state.recorder._start_time = 1000.0
        state.recorder._markers_path = tmp_path / "markers.jsonl"
        state.recorder._is_recording = True

        state.recorder.mark_test_start("test_a")
        state.recorder.mark_test_end("test_a", "passed")

        lines = state.recorder._markers_path.read_text(encoding="utf-8").strip().splitlines()
        entries = [json.loads(line) for line in lines]
        assert len(entries) == 2
        assert float(entries[1]["video_ts"]) >= float(entries[0]["video_ts"])

    def test_markers_not_written_when_not_recording(self, tmp_path: Path) -> None:
        state = SessionState()
        state.recorder = SessionRecorder()
        state.recorder._markers_path = tmp_path / "markers.jsonl"
        # Not recording — _is_recording defaults to False

        state.recorder.mark_test_start("test_a")

        assert not state.recorder._markers_path.exists()
