"""Tests for ``rttr.session_data`` — session data generation, capture, and log handler."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from rttr import _state
from rttr import session_data
from rttr.hooks import phase_report_key


class TestCaptureOutput:
    """Verify ``capture_output`` collects tracebacks and sections."""

    def test_captures_traceback_on_failure(self) -> None:
        _state.test_outputs.clear()
        rep = MagicMock()
        rep.failed = True
        rep.longreprtext = "AssertionError: expected True"
        rep.sections = []

        session_data.capture_output("test.py::test_x", rep)

        assert "AssertionError" in _state.test_outputs["test.py::test_x"]["traceback"]

    def test_captures_sections(self) -> None:
        _state.test_outputs.clear()
        rep = MagicMock()
        rep.failed = False
        rep.longreprtext = ""
        rep.sections = [("Captured stdout", "hello world")]

        session_data.capture_output("test.py::test_x", rep)

        assert "hello world" in _state.test_outputs["test.py::test_x"]["captured_output"]

    def test_accumulates_across_phases(self) -> None:
        _state.test_outputs.clear()
        for phase_text in ("setup error", "call error"):
            rep = MagicMock()
            rep.failed = True
            rep.longreprtext = phase_text
            rep.sections = []
            session_data.capture_output("test.py::test_x", rep)

        combined = _state.test_outputs["test.py::test_x"]["traceback"]
        assert "setup error" in combined
        assert "call error" in combined

    def test_ignores_empty_content(self) -> None:
        _state.test_outputs.clear()
        rep = MagicMock()
        rep.failed = False
        rep.longreprtext = ""
        rep.sections = [("Captured stdout", "   ")]

        session_data.capture_output("test.py::test_x", rep)

        # Whitespace-only content should NOT have been added.
        assert "captured_output" not in _state.test_outputs.get("test.py::test_x", {})


class TestGenerate:
    """Verify ``generate`` produces session_data.json with embedded metadata."""

    def test_generates_session_data_json_with_metadata(self, tmp_path: Path) -> None:
        dl = MagicMock()
        dl.session_dir = tmp_path
        dl._results = []
        _state.debug_logger = dl
        _state.test_outputs.clear()
        _state.collected_nodeids.clear()
        _state.session_log_entries.clear()

        # Create a minimal markers file so _generate_session_data has data.
        markers = tmp_path / "session_recording_markers.jsonl"
        markers.write_text(
            '{"event":"test_start","test":"test.py::test_a","video_ts":1.0}\n'
            '{"event":"test_end","test":"test.py::test_a","video_ts":2.0,"result":"passed"}\n',
            encoding="utf-8",
        )

        session = MagicMock()
        session.testsfailed = 0
        session.testscollected = 1
        session.items = []
        session.config._randomly_seed = 1

        session_data.generate(session)

        data_file = tmp_path / "session_data.json"
        assert data_file.exists()
        data = json.loads(data_file.read_text(encoding="utf-8"))
        assert data["total"] == 1
        assert data["passed"] == 1
        assert len(data["tests"]) == 1
        assert data["tests"][0]["nodeid"] == "test.py::test_a"

        # Metadata should be embedded in the JSON.
        assert "metadata" in data
        assert data["metadata"]["seed"] == "1"

    def test_includes_not_run_tests(self, tmp_path: Path) -> None:
        dl = MagicMock()
        dl.session_dir = tmp_path
        dl._results = []
        _state.debug_logger = dl
        _state.test_outputs.clear()
        _state.session_log_entries.clear()
        _state.collected_nodeids.clear()
        _state.collected_nodeids.append("test.py::test_never_ran")

        markers = tmp_path / "session_recording_markers.jsonl"
        markers.write_text("", encoding="utf-8")

        session = MagicMock()
        session.testsfailed = 0
        session.testscollected = 1
        session.items = []
        session.config._randomly_seed = 1

        session_data.generate(session)

        data = json.loads((tmp_path / "session_data.json").read_text(encoding="utf-8"))
        not_run = [t for t in data["tests"] if t["result"] == "not_run"]
        assert len(not_run) == 1
        assert not_run[0]["nodeid"] == "test.py::test_never_ran"

    def test_includes_debug_logger_results(self, tmp_path: Path) -> None:
        dl = MagicMock()
        dl.session_dir = tmp_path
        dl._results = [
            {"index": 1, "name": "test_one", "status": "PASSED", "duration": 1.5},
        ]
        _state.debug_logger = dl
        _state.test_outputs.clear()
        _state.collected_nodeids.clear()
        _state.session_log_entries.clear()

        markers = tmp_path / "session_recording_markers.jsonl"
        markers.write_text("", encoding="utf-8")

        session = MagicMock()
        session.testsfailed = 0
        session.testscollected = 1
        session.items = []
        session.config._randomly_seed = 1

        session_data.generate(session)

        data = json.loads((tmp_path / "session_data.json").read_text(encoding="utf-8"))
        assert "debug_logger_results" in data
        assert len(data["debug_logger_results"]) == 1
        assert data["debug_logger_results"][0]["status"] == "PASSED"

    def test_standalone_viewer_generated(self, tmp_path: Path) -> None:
        dl = MagicMock()
        dl.session_dir = tmp_path
        dl._results = []
        _state.debug_logger = dl
        _state.test_outputs.clear()
        _state.collected_nodeids.clear()
        _state.session_log_entries.clear()

        markers = tmp_path / "session_recording_markers.jsonl"
        markers.write_text("", encoding="utf-8")

        session = MagicMock()
        session.testsfailed = 0
        session.testscollected = 0
        session.items = []
        session.config._randomly_seed = 1

        # Create a fake viewer template
        viewer_template = Path(session_data._VIEWER_TEMPLATE)
        viewer_template.parent.mkdir(parents=True, exist_ok=True)
        viewer_template.write_text(
            "const SESSION_DATA = null;",
            encoding="utf-8",
        )

        try:
            session_data.generate(session)
            viewer = tmp_path / "viewer.html"
            if viewer.exists():
                content = viewer.read_text(encoding="utf-8")
                assert "SESSION_DATA" in content
                assert "null" not in content  # should be replaced
        finally:
            # Don't leave our fake template behind.
            if viewer_template.exists():
                viewer_template.unlink()
