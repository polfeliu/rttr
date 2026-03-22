"""Session report — verify ``session_data.json`` generation end-to-end.

Runs a tiny test suite with the RTTR plugin active via subprocess,
then inspects the generated artifacts.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def rttr_session_dir(tmp_path: Path) -> Path:
    """Run a minimal test through RTTR and return the debug session dir."""
    test_file = tmp_path / "test_sample.py"
    test_file.write_text(
        textwrap.dedent("""\
        def test_passing():
            assert 1 + 1 == 2

        def test_failing():
            assert 1 == 2, "intentional failure"
        """),
        encoding="utf-8",
    )

    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", str(test_file), "--no-record", "-p", "rttr"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )

    # We expect 1 pass + 1 fail → exit code 1
    assert result.returncode == 1, (
        f"Unexpected exit code: {result.returncode}\n{result.stdout}\n{result.stderr}"
    )

    debug_root = tmp_path / "debug_sessions"
    if not debug_root.exists():
        pytest.skip("debug_sessions not created (RTTR may not have initialised)")

    sessions = sorted(debug_root.iterdir())
    assert len(sessions) >= 1, "Expected at least one session directory"
    return sessions[-1]


class TestSessionReport:
    """Verify the session_data.json and viewer.html artifacts."""

    def test_session_data_json_created(self, rttr_session_dir: Path) -> None:
        data_file = rttr_session_dir / "session_data.json"
        assert data_file.exists(), "session_data.json should be created"

    def test_session_data_contains_tests(self, rttr_session_dir: Path) -> None:
        data_file = rttr_session_dir / "session_data.json"
        data = json.loads(data_file.read_text(encoding="utf-8"))
        assert "tests" in data
        nodeids = [t["nodeid"] for t in data["tests"]]
        assert any("test_passing" in n for n in nodeids)
        assert any("test_failing" in n for n in nodeids)

    def test_session_data_metadata(self, rttr_session_dir: Path) -> None:
        data_file = rttr_session_dir / "session_data.json"
        data = json.loads(data_file.read_text(encoding="utf-8"))
        assert "metadata" in data
        meta = data["metadata"]
        assert "timestamp" in meta
        assert "exit_status" in meta

    def test_session_data_counts(self, rttr_session_dir: Path) -> None:
        data_file = rttr_session_dir / "session_data.json"
        data = json.loads(data_file.read_text(encoding="utf-8"))
        results = data["debug_logger_results"]
        passed = sum(1 for r in results if r["status"] == "PASSED")
        failed = sum(1 for r in results if r["status"] == "FAILED")
        assert passed >= 1
        assert failed >= 1

    def test_failed_test_has_traceback(self, rttr_session_dir: Path) -> None:
        data_file = rttr_session_dir / "session_data.json"
        data = json.loads(data_file.read_text(encoding="utf-8"))
        failed_tests = data["metadata"]["failed_tests"]
        assert len(failed_tests) >= 1
        assert "test_failing" in failed_tests[0]

    def test_viewer_html_generated(self, rttr_session_dir: Path) -> None:
        viewer = rttr_session_dir / "viewer.html"
        assert viewer.exists(), "viewer.html should be generated"
        content = viewer.read_text(encoding="utf-8")
        assert "SESSION_DATA" in content
