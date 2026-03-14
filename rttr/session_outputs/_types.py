"""Typed data containers for session outputs.

These dataclasses replace the untyped dictionaries that were previously
used to pass test results, captured outputs, and log entries around.
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass
class TestOutput:
    """Captured output for a single test (tracebacks, stdout/stderr, dumps)."""

    traceback: str = ""
    captured_output: str = ""
    failure_dump: str = ""
    state_snapshot: str = ""


@dataclasses.dataclass
class TestResult:
    """Result entry recorded by ``DebugLogger`` at the end of each test."""

    __test__ = False  # Prevent pytest from collecting this as a test class.

    index: int
    name: str
    status: str
    duration: float


@dataclasses.dataclass
class LogEntry:
    """A single timestamped log record synced with video playback."""

    ts: float
    level: str
    name: str
    msg: str
