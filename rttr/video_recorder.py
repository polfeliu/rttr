"""Screen recording for test sessions using ffmpeg.

Captures the desktop at low FPS (default 2) during pytest runs.  A JSONL
sidecar file maps test names to video timestamps so that any log entry can
be correlated to a video frame.

After the session ends, ``extract_failure_frames`` can extract PNG frames
for every failed test, giving agents visual context for diagnosis.

Usage ΓÇö integrated automatically via conftest hooks::

    # conftest.py
    from rttr.video_recorder import SessionRecorder

    recorder = SessionRecorder()
    recorder.start(output_dir)      # in pytest_sessionstart
    recorder.mark_test_start(name)  # in pytest_runtest_logstart
    recorder.mark_test_end(name, result)  # in pytest_runtest_logreport
    recorder.stop()                 # in pytest_sessionfinish

If ffmpeg is not installed, all methods are safe no-ops.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


def _primary_monitor_size() -> tuple[int, int]:
    """Return (width, height) of the primary monitor via Win32 API.

    Falls back to (0, 0) on non-Windows or if the call fails, which tells
    the caller to let ffmpeg capture the full desktop.
    """
    if sys.platform != "win32":
        return (0, 0)
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        user32.SetProcessDPIAware()
        return (int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1)))
    except (AttributeError, OSError, ValueError):
        return (0, 0)


def _kill_stale_ffmpeg() -> None:
    """Kill any orphaned ffmpeg processes left over from previous sessions.

    Uses ``taskkill`` on Windows to force-terminate all ffmpeg.exe processes.
    This prevents resource leaks from sessions that crashed without cleanup.
    Safe to call even when no ffmpeg is running (command simply reports
    "not found" and returns a non-zero exit code, which is ignored).
    """
    if sys.platform != "win32":
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "ffmpeg.exe"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        logger.info("Killed stale ffmpeg processes (if any).")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("Could not kill stale ffmpeg: %s", exc)


class SessionRecorder:
    """Manages an ffmpeg screen-recording subprocess for a test session."""

    def __init__(self, fps: int = 2, downscale_720p: bool = False) -> None:
        self._fps = fps
        self._downscale = downscale_720p
        self._process: subprocess.Popen[bytes] | None = None
        self._start_time: float = 0.0
        self._markers_path: Path | None = None
        self._video_path: Path | None = None
        self._stderr_path: Path | None = None
        self._stderr_fh: object | None = None  # file handle for ffmpeg stderr

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, output_dir: Path) -> bool:
        """Start ffmpeg screen recording.

        Kills any stale ffmpeg processes before launching a new one to
        prevent resource leaks from sessions that crashed without cleanup.

        Args:
            output_dir: Directory where video and sidecar files are written.

        Returns:
            True if recording started successfully, False otherwise.
        """
        if not _ffmpeg_available():
            logger.warning("ffmpeg not found on PATH ΓÇö session recording disabled.")
            return False

        # Kill any orphaned ffmpeg from previous sessions before starting.
        _kill_stale_ffmpeg()

        output_dir.mkdir(parents=True, exist_ok=True)
        self._video_path = output_dir / "session_recording.mp4"
        self._markers_path = output_dir / "session_recording_markers.jsonl"

        cmd: list[str] = [
            "ffmpeg",
            "-y",  # overwrite output
            "-f",
            "gdigrab",  # Windows desktop capture
            "-framerate",
            str(self._fps),
        ]

        # Capture only the primary monitor when possible.
        pw, ph = _primary_monitor_size()
        if pw > 0 and ph > 0:
            cmd += ["-offset_x", "0", "-offset_y", "0", "-video_size", f"{pw}x{ph}"]

        cmd += [
            "-i",
            "desktop",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",  # minimal CPU
            "-crf",
            "35",  # low quality = small file
            "-pix_fmt",
            "yuv420p",
        ]

        if self._downscale:
            cmd += ["-vf", "scale=-2:720"]

        cmd.append(str(self._video_path))

        # Redirect stderr to a file to avoid pipe buffer deadlocks.
        self._stderr_path = output_dir / "ffmpeg_stderr.log"
        self._stderr_fh = self._stderr_path.open("w", encoding="utf-8")

        # On Windows, prevent ffmpeg from opening a console window.
        creation_flags = 0
        if sys.platform == "win32":
            creation_flags = subprocess.CREATE_NO_WINDOW

        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=self._stderr_fh,
                creationflags=creation_flags,
            )
        except FileNotFoundError:
            logger.warning("ffmpeg binary not found ΓÇö session recording disabled.")
            self._close_stderr()
            return False
        except OSError as exc:
            logger.warning("Could not start ffmpeg: %s", exc)
            self._close_stderr()
            return False

        # Give ffmpeg a moment to fail (e.g. codec errors, gdigrab issues).
        time.sleep(1.5)
        if self._process.poll() is not None:
            rc = self._process.returncode
            logger.warning(
                "ffmpeg exited immediately (rc=%d). Check %s for details.",
                rc,
                self._stderr_path,
            )
            self._process = None
            self._close_stderr()
            return False

        self._start_time = time.monotonic()
        logger.info(
            "Screen recording started (pid=%d, fps=%d, output=%s).",
            self._process.pid,
            self._fps,
            self._video_path,
        )
        return True

    def stop(self, *, delete_on_success: bool = False, had_failures: bool = True) -> None:
        """Stop the ffmpeg process gracefully.

        Args:
            delete_on_success: If True and *had_failures* is False, remove
                the recording files to save disk space.
            had_failures: Whether any test failed during the session.
        """
        if self._process is None:
            return

        pid = self._process.pid
        try:
            # Send 'q' to ffmpeg's stdin for graceful shutdown
            if self._process.stdin:
                self._process.stdin.write(b"q")
                self._process.stdin.flush()
            self._process.wait(timeout=15)
            logger.info("Screen recording stopped (pid=%d).", pid)
        except subprocess.TimeoutExpired:
            logger.warning("ffmpeg did not exit in time ΓÇö killing (pid=%d).", pid)
            self._process.kill()
            self._process.wait(timeout=5)
        except OSError as exc:
            logger.warning("Error stopping ffmpeg: %s", exc)
            # Last resort
            with contextlib.suppress(OSError):
                self._process.kill()
        finally:
            self._process = None
            self._close_stderr()

        if delete_on_success and not had_failures:
            self._cleanup_files()

    def kill(self) -> None:
        """Force-kill ffmpeg immediately (e.g. from F10 emergency stop)."""
        if self._process is None:
            return
        with contextlib.suppress(OSError):
            self._process.kill()
        self._process = None
        self._close_stderr()

    # ------------------------------------------------------------------
    # Markers
    # ------------------------------------------------------------------

    def mark_test_start(self, nodeid: str) -> None:
        """Record a test-start marker in the sidecar JSONL.

        Args:
            nodeid: Full pytest node ID (e.g. ``test_scope.py::TestX::test_y``).
        """
        self._write_marker("test_start", nodeid)

    def mark_test_end(self, nodeid: str, result: str) -> None:
        """Record a test-end marker in the sidecar JSONL.

        Args:
            nodeid: Full pytest node ID.
            result: Outcome string (``"passed"``, ``"failed"``, ``"skipped"``).
        """
        self._write_marker("test_end", nodeid, result=result)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_recording(self) -> bool:
        """Whether ffmpeg is currently running."""
        return self._process is not None and self._process.poll() is None

    @property
    def pid(self) -> int | None:
        """PID of the ffmpeg process, or None."""
        return self._process.pid if self._process else None

    @property
    def video_path(self) -> Path | None:
        """Path to the output video file."""
        return self._video_path

    @property
    def elapsed(self) -> float:
        """Seconds since recording started."""
        if self._start_time:
            return time.monotonic() - self._start_time
        return 0.0

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _close_stderr(self) -> None:
        if self._stderr_fh is not None:
            with contextlib.suppress(OSError):
                self._stderr_fh.close()  # type: ignore[union-attr]
            self._stderr_fh = None

    def _write_marker(self, event: str, nodeid: str, result: str | None = None) -> None:
        if self._markers_path is None or self._start_time == 0.0:
            return
        entry: dict[str, str | float] = {
            "event": event,
            "test": nodeid,
            "video_ts": round(time.monotonic() - self._start_time, 1),
            "wall_clock": datetime.now(tz=UTC).isoformat(),
        }
        if result is not None:
            entry["result"] = result
        try:
            with self._markers_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError as exc:
            logger.warning("Could not write recording marker: %s", exc)

    def _cleanup_files(self) -> None:
        for path in (self._video_path, self._markers_path):
            if path and path.exists():
                try:
                    path.unlink()
                    logger.info("Deleted recording artifact: %s", path)
                except OSError as exc:
                    logger.warning("Could not delete %s: %s", path, exc)


def _ffmpeg_available() -> bool:
    """Check whether ffmpeg is on the system PATH.

    Returns:
        True if ffmpeg is found, False otherwise.
    """
    return shutil.which("ffmpeg") is not None


# ------------------------------------------------------------------
# Post-session chapter embedding
# ------------------------------------------------------------------


def embed_test_chapters(video_path: Path, markers_path: Path) -> bool:
    """Remux the session MP4 with chapter markers for each test.

    Reads the JSONL sidecar to build an ffmpeg FFMETADATA file, then
    remuxes the video with ``-codec copy`` (no re-encoding).  The original
    file is replaced in-place.

    Args:
        video_path: Path to the session recording MP4.
        markers_path: Path to the JSONL sidecar file.

    Returns:
        True if chapters were embedded, False otherwise.
    """
    if not video_path.exists() or not markers_path.exists():
        return False
    if not _ffmpeg_available():
        logger.warning("ffmpeg not available ΓÇö cannot embed chapters.")
        return False

    chapters = _build_chapters(markers_path)
    if not chapters:
        logger.info("No test markers found ΓÇö skipping chapter embedding.")
        return False

    metadata_path = video_path.with_suffix(".ffmetadata")
    try:
        _write_ffmetadata(metadata_path, chapters)
        return _remux_with_chapters(video_path, metadata_path)
    finally:
        # Clean up the temporary metadata file.
        with contextlib.suppress(OSError):
            metadata_path.unlink(missing_ok=True)


def _build_chapters(
    markers_path: Path,
) -> list[tuple[int, int, str, str]]:
    """Parse the JSONL sidecar and build chapter entries.

    Returns:
        List of (start_ms, end_ms, title, result) tuples sorted by start time.
    """
    starts: dict[str, float] = {}
    ends: dict[str, float] = {}
    results: dict[str, str] = {}

    for line in markers_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
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
        elif event == "test_end":
            ends[nodeid] = ts
            results[nodeid] = entry.get("result", "unknown")

    chapters: list[tuple[int, int, str, str]] = []
    for nodeid in starts:
        if nodeid in ends:
            start_ms = int(starts[nodeid] * 1000)
            end_ms = int(ends[nodeid] * 1000)
            if end_ms <= start_ms:
                end_ms = start_ms + 500  # minimum 0.5s chapter
            result = results.get(nodeid, "unknown")
            # Use short test name for readability (file::test_func)
            short = nodeid.split("/")[-1] if "/" in nodeid else nodeid
            prefix = "\u2717" if result == "failed" else ("\u2713" if result == "passed" else "?")
            title = f"{prefix} {short}"
            chapters.append((start_ms, end_ms, title, result))

    chapters.sort()
    return chapters


def _write_ffmetadata(
    metadata_path: Path,
    chapters: list[tuple[int, int, str, str]],
) -> None:
    """Write an FFMETADATA1 file with [CHAPTER] entries.

    Args:
        metadata_path: Output path for the metadata file.
        chapters: List of (start_ms, end_ms, title, result) tuples.
    """
    lines = [";FFMETADATA1"]
    for start_ms, end_ms, title, _result in chapters:
        lines.append("")
        lines.append("[CHAPTER]")
        lines.append("TIMEBASE=1/1000")
        lines.append(f"START={start_ms}")
        lines.append(f"END={end_ms}")
        # FFMETADATA requires '=' and '\' to be escaped in values.
        safe_title = title.replace("\\", "\\\\").replace("=", "\\=")
        lines.append(f"title={safe_title}")
    lines.append("")
    metadata_path.write_text("\n".join(lines), encoding="utf-8")


def _remux_with_chapters(video_path: Path, metadata_path: Path) -> bool:
    """Remux the video with chapter metadata (no re-encoding).

    Args:
        video_path: Input/output video path (replaced in-place).
        metadata_path: FFMETADATA1 file with chapter definitions.

    Returns:
        True if remuxing succeeded, False otherwise.
    """
    tmp_output = video_path.with_name(video_path.stem + "_chapters" + video_path.suffix)

    cmd: list[str] = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(metadata_path),
        "-map_metadata",
        "1",
        "-codec",
        "copy",
        str(tmp_output),
    ]

    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NO_WINDOW

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
            creationflags=creation_flags,
        )
        if result.returncode != 0:
            logger.warning("ffmpeg chapter remux failed (rc=%d).", result.returncode)
            with contextlib.suppress(OSError):
                tmp_output.unlink(missing_ok=True)
            return False

        # Replace original with the chaptered version.
        tmp_output.replace(video_path)
        logger.info("Embedded chapters into %s.", video_path)
        return True

    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Chapter embedding failed: %s", exc)
        with contextlib.suppress(OSError):
            tmp_output.unlink(missing_ok=True)
        return False


# ------------------------------------------------------------------
# Post-session frame extraction
# ------------------------------------------------------------------


def extract_failure_frames(
    video_path: Path,
    markers_path: Path,
    output_dir: Path,
    *,
    before_s: float = 2.0,
    after_s: float = 1.0,
) -> dict[str, list[Path]]:
    """Extract PNG frames around each failed test from the session recording.

    Reads the JSONL sidecar to find start/end timestamps of failed tests,
    then uses ffmpeg to extract every frame in that range (plus a buffer)
    into ``output_dir/<test_slug>/frame_NNNN.png``.

    Args:
        video_path: Path to the session recording MP4.
        markers_path: Path to the JSONL sidecar file.
        output_dir: Root directory for extracted frames.
        before_s: Seconds before test_start to include.
        after_s: Seconds after test_end to include.

    Returns:
        Mapping of test nodeid ΓåÆ list of extracted frame PNG paths.
    """
    if not video_path.exists() or not markers_path.exists():
        return {}
    if not _ffmpeg_available():
        logger.warning("ffmpeg not available ΓÇö cannot extract failure frames.")
        return {}

    # Parse markers to find failed test time ranges.
    test_ranges = _parse_failed_test_ranges(markers_path)
    if not test_ranges:
        return {}

    result: dict[str, list[Path]] = {}
    for nodeid, (start_ts, end_ts) in test_ranges.items():
        slug = _test_slug(nodeid)
        frame_dir = output_dir / slug
        frame_dir.mkdir(parents=True, exist_ok=True)

        seek = max(0.0, start_ts - before_s)
        duration = (end_ts + after_s) - seek

        frames = _extract_frames(video_path, frame_dir, seek, duration)
        if frames:
            result[nodeid] = frames
            logger.info(
                "Extracted %d frames for %s (%.1fsΓÇô%.1fs).",
                len(frames),
                nodeid,
                seek,
                seek + duration,
            )

    return result


def extract_frames_at(
    video_path: Path,
    output_dir: Path,
    timestamp_s: float,
    *,
    window_s: float = 2.0,
) -> list[Path]:
    """Extract frames from the video around a specific timestamp.

    Useful for agents to inspect what the GUI looked like at an arbitrary
    moment (e.g. when a log entry was written or an exception was raised).

    Args:
        video_path: Path to the session recording MP4.
        output_dir: Directory where frame PNGs are written.
        timestamp_s: Video timestamp in seconds to center the extraction on.
        window_s: Total window to extract (half before, half after).

    Returns:
        List of extracted frame PNG paths sorted chronologically.
    """
    if not video_path.exists() or not _ffmpeg_available():
        return []
    output_dir.mkdir(parents=True, exist_ok=True)
    seek = max(0.0, timestamp_s - window_s / 2)
    return _extract_frames(video_path, output_dir, seek, window_s)


def _parse_failed_test_ranges(markers_path: Path) -> dict[str, tuple[float, float]]:
    """Read the JSONL sidecar and return {nodeid: (start_ts, end_ts)} for failed tests.

    Returns:
        Mapping of nodeid to (start_ts, end_ts) tuples for failed tests.
    """
    starts: dict[str, float] = {}
    ends: dict[str, float] = {}
    failed: set[str] = set()

    for line in markers_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
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
        elif event == "test_end":
            ends[nodeid] = ts
            if entry.get("result") == "failed":
                failed.add(nodeid)

    return {nid: (starts[nid], ends[nid]) for nid in failed if nid in starts and nid in ends}


def _test_slug(nodeid: str) -> str:
    """Convert a pytest nodeid to a filesystem-safe slug.

    Returns:
        Slugified string safe for use as a directory name.
    """
    slug = re.sub(r"[^a-zA-Z0-9_]", "_", nodeid)
    return slug[:120]


def _extract_frames(
    video_path: Path,
    frame_dir: Path,
    seek_s: float,
    duration_s: float,
) -> list[Path]:
    """Run ffmpeg to extract frames from *video_path* into *frame_dir*.

    Returns:
        Sorted list of PNG paths that were created.
    """
    cmd: list[str] = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{seek_s:.2f}",
        "-i",
        str(video_path),
        "-t",
        f"{duration_s:.2f}",
        "-vsync",
        "vfr",
        str(frame_dir / "frame_%04d.png"),
    ]

    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NO_WINDOW

    try:
        subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Frame extraction failed: %s", exc)
        return []

    frames = sorted(frame_dir.glob("frame_*.png"))
    return frames