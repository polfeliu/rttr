"""Screen recording for test sessions using ffmpeg.

Captures the desktop at low FPS (default 2) during pytest runs.  A JSONL
sidecar file maps test names to video timestamps so that any log entry can
be correlated to a video frame.

Usage — integrated automatically via the RTTR pytest plugin::

    from rttr.session_outputs.video_recorder import SessionRecorder

    recorder = SessionRecorder()
    recorder.start(output_dir)
    recorder.mark_test_start(name)
    recorder.mark_test_end(name, result)
    recorder.stop()

If ffmpeg is not installed, all methods are safe no-ops.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import logging
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
    """Kill any orphaned ffmpeg processes left over from previous sessions."""
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


def ffmpeg_available() -> bool:
    """Check whether ffmpeg is on the system PATH.

    Returns:
        True if ffmpeg is found, False otherwise.
    """
    return shutil.which("ffmpeg") is not None


class SessionRecorder:
    """Manages an ffmpeg screen-recording subprocess for a test session."""

    def __init__(self, fps: int = 2, downscale_720p: bool = False) -> None:
        self._fps = fps
        self._downscale = downscale_720p
        self._process: subprocess.Popen[bytes] | None = None
        self._start_time: float = 0.0
        self._markers_path: Path | None = None
        self._video_path: Path | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, output_dir: Path) -> bool:
        """Start ffmpeg screen recording.

        Args:
            output_dir: Directory where video and sidecar files are written.

        Returns:
            True if recording started successfully, False otherwise.
        """
        if not ffmpeg_available():
            logger.warning("ffmpeg not found on PATH — session recording disabled.")
            return False

        _kill_stale_ffmpeg()

        output_dir.mkdir(parents=True, exist_ok=True)
        self._video_path = output_dir / "session_recording.mp4"
        self._markers_path = output_dir / "session_recording_markers.jsonl"

        cmd: list[str] = [
            "ffmpeg",
            "-y",
            "-f",
            "gdigrab",
            "-framerate",
            str(self._fps),
        ]

        pw, ph = _primary_monitor_size()
        if pw > 0 and ph > 0:
            cmd += ["-offset_x", "0", "-offset_y", "0", "-video_size", f"{pw}x{ph}"]

        cmd += [
            "-i",
            "desktop",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "35",
            "-pix_fmt",
            "yuv420p",
        ]

        if self._downscale:
            cmd += ["-vf", "scale=-2:720"]

        cmd.append(str(self._video_path))

        creation_flags = 0
        if sys.platform == "win32":
            creation_flags = subprocess.CREATE_NO_WINDOW

        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags,
            )
        except FileNotFoundError:
            logger.warning("ffmpeg binary not found — session recording disabled.")
            return False
        except OSError as exc:
            logger.warning("Could not start ffmpeg: %s", exc)
            return False

        time.sleep(1.5)
        if self._process.poll() is not None:
            rc = self._process.returncode
            logger.warning(
                "ffmpeg exited immediately (rc=%d).",
                rc,
            )
            self._process = None
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
            if self._process.stdin:
                self._process.stdin.write(b"q")
                self._process.stdin.flush()
            self._process.wait(timeout=15)
            logger.info("Screen recording stopped (pid=%d).", pid)
        except subprocess.TimeoutExpired:
            logger.warning("ffmpeg did not exit in time — killing (pid=%d).", pid)
            self._process.kill()
            self._process.wait(timeout=5)
        except OSError as exc:
            logger.warning("Error stopping ffmpeg: %s", exc)
            with contextlib.suppress(OSError):
                self._process.kill()
        finally:
            self._process = None

        if delete_on_success and not had_failures:
            self._cleanup_files()

    def kill(self) -> None:
        """Force-kill ffmpeg immediately (e.g. from F10 emergency stop)."""
        if self._process is None:
            return
        with contextlib.suppress(OSError):
            self._process.kill()
        self._process = None

    # ------------------------------------------------------------------
    # Markers
    # ------------------------------------------------------------------

    def mark_test_start(self, nodeid: str) -> None:
        """Record a test-start marker in the sidecar JSONL.

        Args:
            nodeid: Full pytest node ID.
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
