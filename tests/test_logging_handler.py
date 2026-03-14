"""Tests for ``SessionLogHandler`` and ``attach_log_handler()`` (in ``rttr.session_data``)."""

from __future__ import annotations

import logging
import time

from rttr.session_outputs import _state
from rttr.session_outputs._logger import SessionLogHandler, attach_log_handler


class TestSessionLogHandler:
    """Verify ``SessionLogHandler.emit`` captures log records."""

    def test_appends_to_session_log_entries(self) -> None:
        _state.session.session_log_entries.clear()
        _state.session.recorder._start_time = time.monotonic() - 5.0

        handler = SessionLogHandler()
        handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))

        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Hello %s",
            args=("world",),
            exc_info=None,
        )

        handler.emit(record)

        assert len(_state.session.session_log_entries) == 1
        entry = _state.session.session_log_entries[0]
        assert entry.level == "INFO"
        assert entry.name == "test.logger"
        assert "Hello world" in entry.msg
        assert isinstance(entry.ts, float)
        assert entry.ts > 0

    def test_video_ts_zero_when_no_start_time(self) -> None:
        _state.session.session_log_entries.clear()
        old_start = _state.session.recorder._start_time
        _state.session.recorder._start_time = 0.0

        handler = SessionLogHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))

        record = logging.LogRecord(
            name="x",
            level=logging.DEBUG,
            pathname="",
            lineno=0,
            msg="test",
            args=(),
            exc_info=None,
        )

        handler.emit(record)

        assert _state.session.session_log_entries[-1].ts == 0.0

        _state.session.recorder._start_time = old_start


class TestAttachLogHandler:
    """Verify ``attach_log_handler()`` adds the handler to the root logger."""

    def test_adds_handler_to_root(self) -> None:
        root = logging.getLogger()
        initial_count = len(root.handlers)

        attach_log_handler()

        assert len(root.handlers) == initial_count + 1
        # The last handler should be a SessionLogHandler.
        assert isinstance(root.handlers[-1], SessionLogHandler)

        # Cleanup: remove the handler we just added.
        root.handlers.pop()

    def test_sets_root_to_debug(self) -> None:
        root = logging.getLogger()
        root.setLevel(logging.WARNING)

        attach_log_handler()

        assert root.level <= logging.DEBUG

        # Cleanup.
        root.handlers.pop()
        root.setLevel(logging.WARNING)
