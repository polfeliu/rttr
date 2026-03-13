"""Session data and debug logging for the RTTR pytest plugin.

This module is the single home for all session-level data collection,
aggregation, and persistence:

- **DebugLogger** — per-session / per-test directory tree with UI dumps
  and screenshots.
- **SessionLogHandler** — log capture with video timestamps.
- **session_data.json** — unified JSON artifact with test results,
  tracebacks, captured output, failure dumps, frames, and synced logs.
- **viewer.html** — standalone browser-based session replay.

Folder layout (managed by ``DebugLogger``)::

    debug_sessions/
        NNN_session_YYYY-MM-DD_HH-MM-SS/
            session_info.md
            session_data.json
            viewer.html
            session_recording.mp4
            session_recording_markers.jsonl
            NNN_test_<test_name>/
                NNN_<label>/
                    state.txt
                    screen.png
            failure_frames/
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from PIL import ImageGrab

from rttr.base import UIA_ERRORS, BaseModel

if TYPE_CHECKING:
    from pywinauto.base_wrapper import BaseWrapper

# ---------------------------------------------------------------------------
# Debug session root (relative to the working directory where pytest runs).
# ---------------------------------------------------------------------------

DEBUG_SESSIONS_ROOT = Path("debug_sessions")


# ---------------------------------------------------------------------------
# Helpers for DebugLogger
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
        # Determine the next session number
        sessions_root.mkdir(parents=True, exist_ok=True)
        existing = sorted(sessions_root.iterdir())
        next_idx = len(existing) + 1

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        folder_name = f"{next_idx:03d}_session_{timestamp}"
        self.session_dir = sessions_root / folder_name
        self.session_dir.mkdir(parents=True, exist_ok=True)

        self._test_counter = 0
        self._test_dir: Path | None = None
        self._test_dir_created = False
        self._state_counter = 0
        self._session_start: float = time.monotonic()
        self._session_start_dt: str = timestamp
        self._current_test_name: str = ""
        self._current_test_start: float = 0.0
        self._results: list[dict[str, Any]] = []

        # Write initial session header
        self._write_session_info()

    # ------------------------------------------------------------------
    def _write_session_info(self) -> None:
        """Write (or overwrite) the session_info.md with current progress."""
        passed = [r for r in self._results if r["status"] == "PASSED"]
        failed = [r for r in self._results if r["status"] == "FAILED"]
        skipped = [r for r in self._results if r["status"] == "SKIPPED"]
        total_elapsed = time.monotonic() - self._session_start

        lines = [
            "# Test Session Report",
            "",
            f"**Started:** {self._session_start_dt}  ",
            f"**Total elapsed:** {total_elapsed:.1f}s  ",
            f"**Tests run:** {len(self._results)}  ",
            f"**Passed:** {len(passed)} | **Failed:** {len(failed)} | **Skipped:** {len(skipped)}",
            "",
        ]

        if self._results:
            lines += [
                "## Results",
                "",
                "| # | Test | Status | Duration |",
                "|---|------|--------|----------|",
            ]
            for r in self._results:
                icon = {"PASSED": "✅", "FAILED": "❌", "SKIPPED": "⏭️"}.get(r["status"], "?")
                dur = f"{r['duration']:.2f}s"
                lines.append(f"| {r['index']} | `{r['name']}` | {icon} {r['status']} | {dur} |")
            lines.append("")

        if failed:
            lines += ["## Failed Tests", ""]
            for r in failed:
                lines.append(f"- `{r['name']}` ({r['duration']:.2f}s)")
            lines.append("")

        (self.session_dir / "session_info.md").write_text("\n".join(lines), encoding="utf-8")

    @property
    def test_dir(self) -> Path | None:
        """Public accessor for the current test's debug directory."""
        return self._test_dir

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
        self._results.append({
            "index": self._test_counter,
            "name": self._current_test_name,
            "status": status,
            "duration": duration,
        })
        self._write_session_info()

    # ------------------------------------------------------------------
    def capture(self, label: str, element: BaseWrapper | None = None) -> Path:
        """Capture a state snapshot: UI tree dump + screenshot.

        Args:
            label: Human-readable description of this state (e.g. "drive_connected").
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


# ---------------------------------------------------------------------------
# State & hook imports (deferred to avoid circular dep with _state.py)
# ---------------------------------------------------------------------------

from rttr import _state  # noqa: E402
from rttr.hooks import phase_report_key  # noqa: E402

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Viewer template path — bundled inside the rttr package.
# ---------------------------------------------------------------------------

_VIEWER_TEMPLATE: Path = Path(__file__).resolve().parent / "session_viewer.html"


# ---------------------------------------------------------------------------
# Log capture (moved from logging_handler.py)
# ---------------------------------------------------------------------------


class SessionLogHandler(logging.Handler):
    """Captures log records with video timestamps for the session viewer."""

    def emit(self, record: logging.LogRecord) -> None:
        """Append a timestamped log entry to shared state.

        Args:
            record: The log record emitted by the Python logging framework.
        """
        elapsed = _state.recorder._start_time
        video_ts = round(time.monotonic() - elapsed, 2) if elapsed else 0.0
        _state.session_log_entries.append({
            "ts": video_ts,
            "level": record.levelname,
            "name": record.name,
            "msg": self.format(record),
        })


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


def capture_output(nodeid: str, rep: pytest.CollectReport) -> None:
    """Capture per-test output (tracebacks, stdout/stderr sections).

    Args:
        nodeid: The pytest node ID of the test.
        rep: The phase report (setup / call / teardown).
    """
    output = _state.test_outputs.setdefault(nodeid, {})
    if rep.failed and rep.longreprtext:
        prev = output.get("traceback", "")
        output["traceback"] = (prev + "\n" + rep.longreprtext).strip()
    for title, content in rep.sections:
        if content.strip():
            prev = output.get("captured_output", "")
            output["captured_output"] = (prev + f"\n--- {title} ---\n{content}").strip()


def generate(session: pytest.Session) -> None:
    """Generate ``session_data.json`` and a standalone viewer after the session.

    This is the single place where all session metadata, test results,
    tracebacks, video timing, failure dumps/frames, and synced log entries
    are aggregated.  The separate ``test_execution_metadata.json`` has been
    folded into this file's ``"metadata"`` key.

    Args:
        session: The pytest Session object.
    """
    if _state.debug_logger is None:
        return

    session_dir = _state.debug_logger.session_dir

    try:
        _generate_session_data(session, session_dir)
    except Exception:  # noqa: BLE001 — best-effort, never block pytest exit
        logger.warning("[SESSION DATA] Could not generate session_data.json.")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _generate_session_data(session: pytest.Session, session_dir: Path) -> None:
    """Generate session_data.json aggregating markers, tracebacks, dumps, frame paths, and metadata.

    Args:
        session: The pytest Session object (for metadata extraction).
        session_dir: Path to the debug session directory.
    """
    markers_path = session_dir / "session_recording_markers.jsonl"

    # Parse JSONL markers for video timing
    starts: dict[str, float] = {}
    ends: dict[str, float] = {}
    results: dict[str, str] = {}
    ordered_tests: list[str] = []

    if markers_path.exists():
        for line in markers_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            nodeid = entry.get("test", "")
            ts = float(entry.get("video_ts", 0.0))
            event = entry.get("event", "")
            if event == "test_start":
                starts[nodeid] = ts
                ordered_tests.append(nodeid)
            elif event == "test_end":
                ends[nodeid] = ts
                results[nodeid] = entry.get("result", "unknown")

    # Build test directory map (NNN_test_<slug> → dir) for failure dumps
    test_dir_map: dict[str, Path] = {}
    for d in sorted(session_dir.iterdir()):
        if d.is_dir() and "_test_" in d.name:
            test_dir_map[d.name] = d

    frames_base = session_dir / "failure_frames"

    tests: list[dict[str, object]] = []
    for nodeid in ordered_tests:
        func_name = nodeid.split("::")[-1] if "::" in nodeid else nodeid

        # Find matching test directory for failure_dump.txt
        failure_dump: str | None = None
        for dir_name, dir_path in test_dir_map.items():
            dir_slug = dir_name.split("_test_", 1)[-1] if "_test_" in dir_name else ""
            name_slug = re.sub(r"[^a-z0-9]+", "_", func_name.lower()).strip("_")[:60]
            if dir_slug == name_slug:
                dump_file = dir_path / "failure_dump.txt"
                if dump_file.exists():
                    failure_dump = dump_file.read_text(encoding="utf-8", errors="replace")
                break

        # Find extracted frames
        frame_paths: list[str] = []
        if frames_base.exists():
            frame_slug = re.sub(r"[^a-zA-Z0-9_]", "_", nodeid)[:120]
            frame_dir = frames_base / frame_slug
            if frame_dir.exists():
                frame_paths = sorted(
                    str(f.relative_to(session_dir)) for f in frame_dir.glob("frame_*.png")
                )

        output_data = _state.test_outputs.get(nodeid, {})

        tests.append({
            "nodeid": nodeid,
            "result": results.get(nodeid, "unknown"),
            "video_start_s": starts.get(nodeid, 0.0),
            "video_end_s": ends.get(nodeid, 0.0),
            "duration_s": round(ends.get(nodeid, 0.0) - starts.get(nodeid, 0.0), 1),
            "traceback": output_data.get("traceback"),
            "captured_output": output_data.get("captured_output"),
            "failure_dump": failure_dump,
            "frames": frame_paths,
        })

    # Mark collected tests that were not run (so the viewer can show them grayed out)
    ran_nodeids = {t["nodeid"] for t in tests}
    tests.extend(
        {
            "nodeid": nid,
            "result": "not_run",
            "video_start_s": 0.0,
            "video_end_s": 0.0,
            "duration_s": 0.0,
            "traceback": None,
            "captured_output": None,
            "failure_dump": None,
            "frames": [],
        }
        for nid in _state.collected_nodeids
        if nid not in ran_nodeids
    )

    data: dict[str, object] = {
        "session_dir": session_dir.name,
        "metadata": {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "seed": str(getattr(session.config, "_randomly_seed", "unknown")),
            "failed_tests": [
                item.nodeid
                for item in session.items
                if any(
                    rep.failed
                    for rep in item.stash.get(phase_report_key, {}).values()
                )
            ],
        },
        "total": sum(1 for t in tests if t["result"] != "not_run"),
        "passed": sum(1 for t in tests if t["result"] == "passed"),
        "failed": sum(1 for t in tests if t["result"] == "failed"),
        "skipped": sum(1 for t in tests if t["result"] == "skipped"),
        "not_run": sum(1 for t in tests if t["result"] == "not_run"),
        "collected_tests": _state.collected_nodeids,
        "debug_logger_results": (
            _state.debug_logger._results if _state.debug_logger is not None else []
        ),
        "logs": _state.session_log_entries,
        "tests": tests,
    }

    (session_dir / "session_data.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )
    logger.info(
        "[SESSION DATA] Generated %s (%d tests).",
        session_dir / "session_data.json",
        len(tests),
    )

    _generate_standalone_viewer(session_dir, data)


def _generate_standalone_viewer(session_dir: Path, data: dict[str, object]) -> None:
    """Generate a standalone viewer.html with embedded session data.

    Args:
        session_dir: Path to the debug session directory.
        data: The session data dictionary to embed.
    """
    if not _VIEWER_TEMPLATE.exists():
        logger.warning("Viewer template not found at %s", _VIEWER_TEMPLATE)
        return
    template = _VIEWER_TEMPLATE.read_text(encoding="utf-8")
    data_json = json.dumps(data)
    standalone = template.replace(
        "const SESSION_DATA = null;",
        f"const SESSION_DATA = {data_json};",
    )
    (session_dir / "viewer.html").write_text(standalone, encoding="utf-8")
    logger.info("[SESSION DATA] Generated standalone viewer at %s", session_dir / "viewer.html")
