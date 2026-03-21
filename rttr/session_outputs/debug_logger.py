"""DebugLogger — manages the structured debug session folder tree.

Creates the session directory and manages per-test subdirectories
with UI tree dumps and screenshots.

Folder layout::

    debug_sessions/
        YYYY-MM-DD_HH-MM-SS <suffix>/
            session_data.json
            viewer.html
            session_recording.mp4
            NNN_test_<test_name>/
                NNN_<label>/
                    state.txt
                    screen.png
            failure_frames/
"""

from __future__ import annotations

import contextlib
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import ImageGrab

from rttr.base import UIA_ERRORS, BaseModel
from rttr.session_outputs._types import TestResult

if TYPE_CHECKING:
    from pywinauto.base_wrapper import BaseWrapper

# ---------------------------------------------------------------------------
# Debug session root (relative to the working directory where pytest runs).
# ---------------------------------------------------------------------------

DEBUG_SESSIONS_ROOT = Path("debug_sessions")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    """Convert arbitrary text into a safe filesystem slug.

    Returns:
        A lowercase slug with non-alphanumeric characters replaced by underscores.
    """
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")[:60]


def _format_tree(
    element: BaseWrapper,
    depth: int = 0,
    max_depth: int = 12,
    current_owner: str | None = None,
) -> str:
    """Recursively format a pywinauto element tree as indented text.

    Args:
        element:       pywinauto element to dump.
        depth:         Current recursion depth.
        max_depth:     Maximum depth before truncating.
        current_owner: Name of the POM widget that owns the current subtree,
                       or ``None`` if no container has been matched yet.

    Returns:
        Formatted multi-line string representation of the element subtree.
    """
    if depth > max_depth:
        return ""
    indent = "  " * depth
    try:
        ctrl = element.element_info.control_type
    except UIA_ERRORS:
        ctrl = "?"
    try:
        title = element.window_text().replace("\n", "\\n")
    except UIA_ERRORS:
        title = ""
    try:
        enabled = element.is_enabled()
    except UIA_ERRORS:
        enabled = False
    try:
        visible = element.is_visible()
    except UIA_ERRORS:
        visible = False

    effective_owner = current_owner

    line = f"{indent}[{ctrl}] title='{title}' enabled={enabled} visible={visible}"
    if effective_owner:
        line += f"  → {effective_owner}"
    line += "\n"

    with contextlib.suppress(*UIA_ERRORS):
        for child in element.children():
            line += _format_tree(child, depth + 1, max_depth, effective_owner)

    return line


# ---------------------------------------------------------------------------
# DebugLogger — per-session / per-test artifact manager
# ---------------------------------------------------------------------------


class DebugLogger:
    """Manages a structured debug session folder for an entire pytest run."""

    def __init__(self, sessions_root: Path = DEBUG_SESSIONS_ROOT) -> None:
        sessions_root.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.session_dir = sessions_root / timestamp
        if self.session_dir.exists():
            counter = 2
            while (sessions_root / f"{timestamp} ({counter})").exists():
                counter += 1
            self.session_dir = sessions_root / f"{timestamp} ({counter})"
        self.session_dir.mkdir(parents=True, exist_ok=True)

        self._test_counter = 0
        self._test_dir: Path | None = None
        self._test_dir_created = False
        self._state_counter = 0
        self._session_start: float = time.monotonic()
        self._session_start_dt: str = timestamp
        self._current_test_name: str = ""
        self._current_test_start: float = 0.0
        self._results: list[TestResult] = []

    @property
    def test_dir(self) -> Path | None:
        """Public accessor for the current test's debug directory."""
        return self._test_dir

    def apply_suffix(self, suffix: str) -> None:
        """Rename the session directory to include a descriptive suffix.

        Called after test collection so the folder name reflects which
        tests were selected.  E.g. ``2026-03-21_13-50-16 test_control``.

        Args:
            suffix: Short label derived from collected test node IDs.
        """
        new_name = f"{self._session_start_dt} {suffix}"
        new_path = self.session_dir.parent / new_name
        if new_path.exists():
            counter = 2
            while (self.session_dir.parent / f"{new_name} ({counter})").exists():
                counter += 1
            new_path = self.session_dir.parent / f"{new_name} ({counter})"
        self.session_dir.rename(new_path)
        self.session_dir = new_path

    # ------------------------------------------------------------------
    def new_test(self, test_name: str) -> None:
        """Call at the start of each test to initialise its subfolder.

        The directory is created lazily — only when :meth:`capture` or
        :meth:`_ensure_test_dir` is called, avoiding wasted I/O for
        passing tests whose folders would just be discarded.
        """
        self._test_counter += 1
        self._state_counter = 0
        self._current_test_name = test_name
        self._current_test_start = time.monotonic()
        slug = _slugify(test_name)
        folder_name = f"{self._test_counter:03d}_test_{slug}"
        self._test_dir = self.session_dir / folder_name
        self._test_dir_created = False

    def _ensure_test_dir(self) -> Path:
        """Create the current test directory on disk if it doesn't exist yet.

        Returns:
            The path to the (now-existing) test directory.
        """
        if self._test_dir is None:
            self.new_test("unknown_test")
        assert self._test_dir is not None
        if not self._test_dir_created:
            self._test_dir.mkdir(parents=True, exist_ok=True)
            self._test_dir_created = True
        return self._test_dir

    # ------------------------------------------------------------------
    def record_result(self, status: str) -> None:
        """Record the result of the current test.

        Args:
            status: One of ``"PASSED"``, ``"FAILED"``, or ``"SKIPPED"``.
        """
        duration = time.monotonic() - self._current_test_start
        self._results.append(
            TestResult(
                index=self._test_counter,
                name=self._current_test_name,
                status=status,
                duration=duration,
            )
        )

    # ------------------------------------------------------------------
    def capture(self, label: str, element: BaseWrapper | None = None) -> Path:
        """Capture a state snapshot: UI tree dump + screenshot.

        Args:
            label: Human-readable description of this state.
            element: Optional pywinauto element to dump the UI tree from.
                     If ``None``, the tree dump is skipped.

        Returns:
            The path to the created state folder.
        """
        test_dir = self._ensure_test_dir()

        self._state_counter += 1
        slug = _slugify(label)
        state_dir = test_dir / f"{self._state_counter:03d}_{slug}"
        state_dir.mkdir(parents=True, exist_ok=True)

        # 1. UI tree text dump
        if element is not None:
            is_pom = isinstance(element, BaseModel)

            if is_pom:
                try:
                    tree_text = element.dump_state()
                except UIA_ERRORS as e:
                    tree_text = f"[Could not call dump_state on POM: {e}]\n"
            else:
                try:
                    tree_text = _format_tree(element)
                except UIA_ERRORS as e:
                    tree_text = f"[Could not dump tree via _format_tree: {e}]\n"
        else:
            tree_text = "[No element provided]\n"

        (state_dir / "state.txt").write_text(
            f"State: {label}\nCaptured at: {datetime.now().isoformat()}\n\n{tree_text}",
            encoding="utf-8",
        )

        # 2. Full-screen screenshot
        try:
            img = ImageGrab.grab()
            img.save(str(state_dir / "screen.png"))
        except OSError as e:
            (state_dir / "screen_error.txt").write_text(str(e), encoding="utf-8")

        return state_dir

    # ------------------------------------------------------------------
    def discard_test_dir(self) -> None:
        """Discard the current test's debug folder (e.g. if the test passed).

        If the directory was never created (lazy init), this is a no-op.
        """
        if self._test_dir and self._test_dir_created and self._test_dir.exists():
            try:
                shutil.rmtree(self._test_dir)
            except OSError as e:
                print(f"Warning: Could not discard debug folder {self._test_dir}: {e}")
        self._test_dir = None
        self._test_dir_created = False
