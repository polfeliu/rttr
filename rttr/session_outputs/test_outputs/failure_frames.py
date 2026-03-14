"""Extract PNG frames around failed tests from the session recording.

Reads the JSONL sidecar to find start/end timestamps of failed tests,
then uses ffmpeg to extract every frame in that range into per-test
subdirectories.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from pathlib import Path

from rttr.session_outputs.video_recorder import ffmpeg_available

logger = logging.getLogger(__name__)


def extract_failure_frames(
    video_path: Path,
    markers_path: Path,
    output_dir: Path,
    *,
    before_s: float = 2.0,
    after_s: float = 1.0,
) -> dict[str, list[Path]]:
    """Extract PNG frames around each failed test from the session recording.

    Args:
        video_path: Path to the session recording MP4.
        markers_path: Path to the JSONL sidecar file.
        output_dir: Root directory for extracted frames.
        before_s: Seconds before test_start to include.
        after_s: Seconds after test_end to include.

    Returns:
        Mapping of test nodeid to list of extracted frame PNG paths.
    """
    if not video_path.exists() or not markers_path.exists():
        return {}
    if not ffmpeg_available():
        logger.warning("ffmpeg not available — cannot extract failure frames.")
        return {}

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
                "Extracted %d frames for %s (%.1fs–%.1fs).",
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

    Args:
        video_path: Path to the session recording MP4.
        output_dir: Directory where frame PNGs are written.
        timestamp_s: Video timestamp in seconds to center the extraction on.
        window_s: Total window to extract (half before, half after).

    Returns:
        List of extracted frame PNG paths sorted chronologically.
    """
    if not video_path.exists() or not ffmpeg_available():
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
