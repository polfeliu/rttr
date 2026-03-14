"""Post-session chapter embedding into the session recording MP4.

Reads the JSONL sidecar to build an ffmpeg FFMETADATA file, then remuxes
the video with ``-codec copy`` (no re-encoding) to add chapter markers.
"""

from __future__ import annotations

import contextlib
import json
import logging
import subprocess
import sys
from pathlib import Path

from rttr.session_outputs.video_recorder import ffmpeg_available

logger = logging.getLogger(__name__)


def embed_test_chapters(video_path: Path, markers_path: Path) -> bool:
    """Remux the session MP4 with chapter markers for each test.

    Args:
        video_path: Path to the session recording MP4.
        markers_path: Path to the JSONL sidecar file.

    Returns:
        True if chapters were embedded, False otherwise.
    """
    if not video_path.exists() or not markers_path.exists():
        return False
    if not ffmpeg_available():
        logger.warning("ffmpeg not available — cannot embed chapters.")
        return False

    chapters = _build_chapters(markers_path)
    if not chapters:
        logger.info("No test markers found — skipping chapter embedding.")
        return False

    metadata_path = video_path.with_suffix(".ffmetadata")
    try:
        _write_ffmetadata(metadata_path, chapters)
        return _remux_with_chapters(video_path, metadata_path)
    finally:
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
                end_ms = start_ms + 500
            result = results.get(nodeid, "unknown")
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

        tmp_output.replace(video_path)
        logger.info("Embedded chapters into %s.", video_path)
        return True

    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Chapter embedding failed: %s", exc)
        with contextlib.suppress(OSError):
            tmp_output.unlink(missing_ok=True)
        return False
