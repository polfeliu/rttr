"""RTTR — Release The T-Rex.

A pytest plugin that provides screen recording, session data generation,
emergency-stop hotkeys, and debug logging for MotionLab integration tests.

This ``__init__`` module acts as the orchestrator: it re-exports simple
hooks from sub-modules and defines composite hooks that delegate to
multiple sub-modules in the correct order.

Usage — add to ``conftest.py``::

    pytest_plugins = ["rttr"]
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from rttr import _state, emergency_stop, recording, session_data
from rttr.hooks import (
    phase_report_key,
    pytest_addoption,
    pytest_collection_modifyitems,
    pytest_report_header,
    store_phase_report,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from pytest import CollectReport

    from rttr.session_data import DebugLogger

# Public API surface — everything else is internal plugin plumbing.
__all__ = [
    "phase_report_key",
]

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _finalise_debug_result(item: pytest.Item) -> None:
    """Record a test's PASSED/FAILED/SKIPPED status in the debug logger.

    Called once the teardown phase report is available, so all three phases
    (setup, call, teardown) have been stashed.  On success or skip the
    per-test debug directory is discarded to save disk space.

    Args:
        item: The pytest Item whose phases are being finalised.
    """
    dl = _state.debug_logger
    if dl is None:
        return

    report = item.stash.get(phase_report_key, {})
    failed_phases = [phase for phase, rep in report.items() if rep.failed]
    skipped_phases = [phase for phase, rep in report.items() if rep.skipped]

    if failed_phases:
        dl.record_result("FAILED")
    elif skipped_phases:
        dl.record_result("SKIPPED")
        dl.discard_test_dir()
    else:
        dl.record_result("PASSED")
        _logger.info(
            "[RTTR] Test passed — discarding debug artifacts for %s.",
            item.nodeid,
        )
        dl.discard_test_dir()


# ---------------------------------------------------------------------------
# Composite hooks (orchestrate multiple sub-modules)
# ---------------------------------------------------------------------------


def pytest_sessionstart(session: pytest.Session) -> None:
    """Create debug logger, start recorder, attach log handler, bind hotkeys.

    Args:
        session: The pytest Session object.
    """
    _state.init_debug_logger()
    recording.start(session)
    session_data.attach_log_handler()
    emergency_stop.register()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:  # noqa: ARG001
    """Stop recording, save metadata, generate session data.

    Args:
        session: The pytest Session object.
        exitstatus: The exit status code (unused directly; failure info
            is read from ``session.testsfailed``).
    """
    recording.stop(session)
    session_data.generate(session)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(
    item: pytest.Item,
    call: pytest.CallInfo[None],  # noqa: ARG001
) -> Generator[None, CollectReport, CollectReport]:
    """Expose test results to fixtures, emit video markers, capture output.

    Args:
        item: The pytest Item being reported on.
        call: The CallInfo for this phase (unused).

    Returns:
        The CollectReport representing the test phase outcome.

    Yields:
        Control back to pytest to generate the report.
    """
    rep: CollectReport = yield

    # 1. Stash the phase report so fixtures can inspect pass/fail.
    store_phase_report(item, rep)

    # 2. Emit a video marker when the "call" phase finishes.
    if rep.when == "call":
        recording.mark_test_end(item.nodeid, rep.outcome)

    # 3. Capture per-test output for session_data.json.
    session_data.capture_output(item.nodeid, rep)

    # 4. After teardown completes, finalise the debug logger for this test.
    #    This records PASSED/FAILED/SKIPPED and discards artifacts on success.
    if rep.when == "teardown":
        _finalise_debug_result(item)

    return rep


def pytest_runtest_logstart(
    nodeid: str,
    location: tuple[str, int | None, str],  # noqa: ARG001
) -> None:
    """Mark the start of each test in the video recording sidecar.

    Also initialises the debug-logger test directory so that fixtures and
    other hooks can write per-test artifacts.

    Args:
        nodeid: The node ID of the test about to start.
        location: The fspath/lineno/domain tuple (unused).
    """
    recording.mark_test_start(nodeid)

    # Create a per-test debug directory.  The nodeid is something like
    # ``motionlab_integration_tests/test_scope.py::test_start_stop``;
    # extract just the test function name for a cleaner folder name.
    test_name = nodeid.rsplit("::", maxsplit=1)[-1]
    if _state.debug_logger is not None:
        _state.debug_logger.new_test(test_name)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def debug_logger() -> DebugLogger:
    """Return the session-wide DebugLogger (created in ``pytest_sessionstart``).

    Returns:
        The instantiated DebugLogger.
    """
    assert _state.debug_logger is not None, "DebugLogger was not created in pytest_sessionstart"
    return _state.debug_logger
