"""Generate ``session_data.json`` and a standalone ``viewer.html``.

Aggregates test results, tracebacks, captured output, failure dumps,
extracted frames, and synced log entries into a single JSON artifact.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from rttr.session_outputs import _state
from rttr.session_outputs._types import TestOutput

if TYPE_CHECKING:
    import pytest

logger = logging.getLogger(__name__)

# Viewer template path — bundled inside the rttr package.
_VIEWER_TEMPLATE: Path = Path(__file__).resolve().parent.parent / "session_viewer.html"


def capture_output(nodeid: str, rep: pytest.CollectReport) -> None:
    """Capture per-test output (tracebacks, stdout/stderr sections).

    Args:
        nodeid: The pytest node ID of the test.
        rep: The phase report (setup / call / teardown).
    """
    if nodeid not in _state.session.test_outputs:
        _state.session.test_outputs[nodeid] = TestOutput()
    output = _state.session.test_outputs[nodeid]
    if rep.failed and rep.longreprtext:
        output.traceback = (output.traceback + "\n" + rep.longreprtext).strip()
    for _title, content in rep.sections:
        if content.strip():
            output.captured_output = (
                output.captured_output + f"\n--- {_title} ---\n{content}"
            ).strip()


def generate(session: pytest.Session, exitstatus: int) -> None:
    """Generate ``session_data.json`` and a standalone viewer after the session.

    Args:
        session: The pytest Session object.
        exitstatus: The pytest exit status code.
    """
    from rttr.hooks import phase_report_key  # noqa: PLC0415 — deferred to break circular import

    if _state.session.debug_logger is None:
        return

    session_dir = _state.session.debug_logger.session_dir

    try:
        _generate_session_data(session, session_dir, phase_report_key, exitstatus)
    except Exception:  # noqa: BLE001 — best-effort, never block pytest exit
        logger.warning("[SESSION DATA] Could not generate session_data.json.")


def _generate_session_data(
    session: pytest.Session,
    session_dir: Path,
    phase_report_key: pytest.StashKey[dict[str, pytest.CollectReport]],
    exitstatus: int,
) -> None:
    """Generate session_data.json aggregating all session artifacts.

    Args:
        session: The pytest Session object.
        session_dir: Path to the debug session directory.
        phase_report_key: Stash key for per-phase reports.
        exitstatus: The pytest exit status code.
    """
    markers_path = session_dir / "session_recording_markers.jsonl"

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

    test_dir_map: dict[str, Path] = {}
    for d in sorted(session_dir.iterdir()):
        if d.is_dir() and "_test_" in d.name:
            test_dir_map[d.name] = d

    frames_base = session_dir / "failure_frames"

    tests: list[dict[str, object]] = []
    for nodeid in ordered_tests:
        func_name = nodeid.split("::")[-1] if "::" in nodeid else nodeid

        frame_paths: list[str] = []
        if frames_base.exists():
            frame_slug = re.sub(r"[^a-zA-Z0-9_]", "_", nodeid)[:120]
            frame_dir = frames_base / frame_slug
            if frame_dir.exists():
                frame_paths = sorted(
                    str(f.relative_to(session_dir)) for f in frame_dir.glob("frame_*.png")
                )

        output_data = _state.session.test_outputs.get(nodeid, TestOutput())

        # Fall back to file-based failure_dump for backward compat.
        failure_dump = output_data.failure_dump or None
        if failure_dump is None:
            for dir_name, dir_path in test_dir_map.items():
                dir_slug = dir_name.split("_test_", 1)[-1] if "_test_" in dir_name else ""
                name_slug = re.sub(r"[^a-z0-9]+", "_", func_name.lower()).strip("_")[:60]
                if dir_slug == name_slug:
                    dump_file = dir_path / "failure_dump.txt"
                    if dump_file.exists():
                        failure_dump = dump_file.read_text(encoding="utf-8", errors="replace")
                    break

        tests.append({
            "nodeid": nodeid,
            "result": results.get(nodeid, "unknown"),
            "video_start_s": starts.get(nodeid, 0.0),
            "video_end_s": ends.get(nodeid, 0.0),
            "duration_s": round(ends.get(nodeid, 0.0) - starts.get(nodeid, 0.0), 1),
            "traceback": output_data.traceback or None,
            "captured_output": output_data.captured_output or None,
            "failure_dump": failure_dump,
            "state_snapshot": output_data.state_snapshot or None,
            "frames": frame_paths,
        })

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
            "state_snapshot": None,
            "frames": [],
        }
        for nid in _state.session.collected_nodeids
        if nid not in ran_nodeids
    )

    data: dict[str, object] = {
        "session_dir": session_dir.name,
        "metadata": {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "seed": str(getattr(session.config, "_randomly_seed", "unknown")),
            "exit_status": exitstatus,
            "collect_only": _state.session.collect_only,
            "failed_tests": [
                item.nodeid
                for item in session.items
                if any(rep.failed for rep in item.stash.get(phase_report_key, {}).values())
            ],
        },
        "total": sum(1 for t in tests if t["result"] != "not_run"),
        "passed": sum(1 for t in tests if t["result"] == "passed"),
        "failed": sum(1 for t in tests if t["result"] == "failed"),
        "skipped": sum(1 for t in tests if t["result"] == "skipped"),
        "not_run": sum(1 for t in tests if t["result"] == "not_run"),
        "collected_tests": _state.session.collected_nodeids,
        "debug_logger_results": (
            [dataclasses.asdict(r) for r in _state.session.debug_logger._results]
            if _state.session.debug_logger is not None
            else []
        ),
        "logs": [dataclasses.asdict(e) for e in _state.session.session_log_entries],
        "tests": tests,
    }

    (session_dir / "session_data.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info(
        "[SESSION DATA] Generated %s (%d tests).",
        session_dir / "session_data.json",
        len(tests),
    )

    if not _state.session.collect_only:
        _generate_standalone_viewer(session_dir, data)

    # Clean up intermediate markers file — its data is now in session_data.json.
    if markers_path.exists():
        markers_path.unlink(missing_ok=True)


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
