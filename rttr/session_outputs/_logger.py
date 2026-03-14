"""Log capture with video timestamps for the session viewer."""

from __future__ import annotations

import logging
import time

from rttr.session_outputs import _state
from rttr.session_outputs._types import LogEntry

logger = logging.getLogger(__name__)


class SessionLogHandler(logging.Handler):
    """Captures log records with video timestamps for the session viewer."""

    def emit(self, record: logging.LogRecord) -> None:
        """Append a timestamped log entry to shared state.

        Args:
            record: The log record emitted by the Python logging framework.
        """
        elapsed = _state.session.recorder._start_time
        video_ts = round(time.monotonic() - elapsed, 2) if elapsed else 0.0
        _state.session.session_log_entries.append(
            LogEntry(
                ts=video_ts,
                level=record.levelname,
                name=record.name,
                msg=self.format(record),
            )
        )


def attach_log_handler() -> None:
    """Attach the ``SessionLogHandler`` to the root logger.

    Ensures ``DEBUG`` records propagate so the handler captures everything.
    """
    root = logging.getLogger()
    if root.level > logging.DEBUG:
        root.setLevel(logging.DEBUG)
    handler = SessionLogHandler()
    handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    handler.setLevel(logging.DEBUG)
    root.addHandler(handler)
