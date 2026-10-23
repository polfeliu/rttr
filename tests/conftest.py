"""Shared fixtures for RTTR unit and integration tests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from rttr import _state

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


# ---------------------------------------------------------------------------
# Patch heavy optional deps that are unavailable in pure-unit-test environments
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_state() -> Generator[None]:
    """Reset shared ``_state`` singletons between tests.

    This prevents test-to-test pollution of the recorder, debug_logger,
    collected_nodeids, etc.
    """
    old_dl = _state.session.debug_logger
    old_outputs = _state.session.test_outputs.copy()
    old_nodeids = _state.session.collected_nodeids.copy()
    old_logs = _state.session.session_log_entries.copy()

    yield

    _state.session.debug_logger = old_dl
    _state.session.test_outputs.clear()
    _state.session.test_outputs.update(old_outputs)
    _state.session.collected_nodeids.clear()
    _state.session.collected_nodeids.extend(old_nodeids)
    _state.session.session_log_entries.clear()
    _state.session.session_log_entries.extend(old_logs)


@pytest.fixture
def tmp_session_dir(tmp_path: Path) -> Path:
    """Provide a temporary directory simulating a debug session folder."""
    d = tmp_path / "001_session_2026-03-13_12-00-00"
    d.mkdir()
    return d


@pytest.fixture
def mock_recorder() -> MagicMock:
    """Return a mock SessionRecorder patched into ``_state``."""
    rec = MagicMock()
    rec._start_time = 100.0
    rec.is_recording = True
    with patch("rttr.session_outputs._state.session.recorder", rec):
        yield rec
