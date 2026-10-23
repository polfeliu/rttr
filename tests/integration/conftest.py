"""Fixtures for RTTR integration tests.

Launches the dummy PyQt5 app as a subprocess and provides ``BaseModel``
wrappers for the tests.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pywinauto import Application

from rttr.base import BaseModel

if TYPE_CHECKING:
    from collections.abc import Generator

    from pywinauto.application import WindowSpecification

_DUMMY_APP = str(Path(__file__).with_name("dummy_app.py"))
_APP_TITLE = "RTTR Dummy App"
_STARTUP_TIMEOUT = 10  # seconds


# ---------------------------------------------------------------------------
# App process fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def dummy_app_process() -> Generator[subprocess.Popen[bytes]]:
    """Launch the dummy Qt app and yield the ``Popen`` handle.

    Kills the process on teardown.
    """
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, _DUMMY_APP],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait until the window is visible via Win32 enumeration
    deadline = time.time() + _STARTUP_TIMEOUT
    while time.time() < deadline:
        handles = BaseModel._find_window_handles(proc.pid, title=_APP_TITLE)
        if handles:
            break
        time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail(f"Dummy app did not show window within {_STARTUP_TIMEOUT}s")

    yield proc

    proc.kill()
    proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# pywinauto connection fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app_window(dummy_app_process: subprocess.Popen[bytes]) -> WindowSpecification:
    """Connect to the dummy app's main window via pywinauto (UIA backend)."""
    app = Application(backend="uia").connect(
        process=dummy_app_process.pid,
        title=_APP_TITLE,
    )
    return app.window(title=_APP_TITLE)


@pytest.fixture
def app_model(app_window: WindowSpecification) -> BaseModel:
    """Return a ``BaseModel`` wrapping the dummy app's main window."""
    return BaseModel(app_window, name="DummyApp")
