"""Tests for ``rttr.video_recorder`` — SessionRecorder and helpers."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from rttr.session_outputs.test_outputs.failure_frames import (
    _extract_frames,
    _parse_failed_test_ranges,
    _test_slug,
    extract_failure_frames,
    extract_frames_at,
)
from rttr.session_outputs.video_chapters import (
    _build_chapters,
    _remux_with_chapters,
    _write_ffmetadata,
    embed_test_chapters,
)
from rttr.session_outputs.video_recorder import (
    SessionRecorder,
    _kill_stale_ffmpeg,
    _primary_monitor_size,
    ffmpeg_available,
)


class TestSessionRecorderInit:
    """Verify initial state of a fresh SessionRecorder."""

    def test_not_recording_initially(self) -> None:
        rec = SessionRecorder()
        assert not rec.is_recording

    def test_pid_none_initially(self) -> None:
        rec = SessionRecorder()
        assert rec.pid is None

    def test_video_path_none_initially(self) -> None:
        rec = SessionRecorder()
        assert rec.video_path is None

    def test_elapsed_zero_initially(self) -> None:
        rec = SessionRecorder()
        assert rec.elapsed == 0.0


class TestSessionRecorderStartNoFfmpeg:
    """Verify graceful fallback when ffmpeg is not available."""

    def test_returns_false_when_ffmpeg_missing(self, tmp_path: Path) -> None:
        rec = SessionRecorder()
        with patch("rttr.session_outputs.video_recorder.ffmpeg_available", return_value=False):
            result = rec.start(tmp_path)
        assert result is False
        assert not rec.is_recording


class TestSessionRecorderMarkers:
    """Verify JSONL marker writing."""

    def test_write_marker_creates_jsonl_line(self, tmp_path: Path) -> None:
        rec = SessionRecorder()
        rec._markers_path = tmp_path / "markers.jsonl"
        rec._start_time = 100.0  # pretend we started 100s ago (monotonic)

        rec._write_marker("test_start", "test.py::test_one")

        content = rec._markers_path.read_text(encoding="utf-8").strip()
        entry = json.loads(content)
        assert entry["event"] == "test_start"
        assert entry["test"] == "test.py::test_one"
        assert "video_ts" in entry
        assert "wall_clock" in entry

    def test_write_marker_includes_result(self, tmp_path: Path) -> None:
        rec = SessionRecorder()
        rec._markers_path = tmp_path / "markers.jsonl"
        rec._start_time = 100.0

        rec._write_marker("test_end", "test.py::test_one", result="failed")

        entry = json.loads(rec._markers_path.read_text(encoding="utf-8").strip())
        assert entry["result"] == "failed"

    def test_write_marker_noop_without_markers_path(self) -> None:
        rec = SessionRecorder()
        rec._markers_path = None
        rec._start_time = 100.0
        # Should not raise.
        rec._write_marker("test_start", "test.py::test_one")

    def test_write_marker_noop_without_start_time(self, tmp_path: Path) -> None:
        rec = SessionRecorder()
        rec._markers_path = tmp_path / "markers.jsonl"
        rec._start_time = 0.0
        rec._write_marker("test_start", "test.py::test_one")
        assert not rec._markers_path.exists()

    def test_mark_test_start_delegates(self) -> None:
        rec = SessionRecorder()
        with patch.object(rec, "_write_marker") as mock:
            rec.mark_test_start("test.py::test_x")
            mock.assert_called_once_with("test_start", "test.py::test_x")

    def test_mark_test_end_delegates(self) -> None:
        rec = SessionRecorder()
        with patch.object(rec, "_write_marker") as mock:
            rec.mark_test_end("test.py::test_x", "passed")
            mock.assert_called_once_with("test_end", "test.py::test_x", result="passed")


class TestSessionRecorderStop:
    """Verify stop/kill logic with mocked process."""

    def test_stop_noop_without_process(self) -> None:
        rec = SessionRecorder()
        rec.stop()  # should not raise

    def test_stop_sends_q_to_stdin(self) -> None:
        rec = SessionRecorder()
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.pid = 12345
        rec._process = proc

        rec.stop()

        proc.stdin.write.assert_called_with(b"q")
        proc.wait.assert_called()

    def test_kill_force_kills_process(self) -> None:
        rec = SessionRecorder()
        proc = MagicMock()
        rec._process = proc

        rec.kill()

        proc.kill.assert_called_once()
        assert rec._process is None


class TestSessionRecorderCleanup:
    """Verify file cleanup."""

    def test_cleanup_removes_video_and_markers(self, tmp_path: Path) -> None:
        rec = SessionRecorder()
        video = tmp_path / "session_recording.mp4"
        markers = tmp_path / "session_recording_markers.jsonl"
        video.write_bytes(b"fake")
        markers.write_text("{}", encoding="utf-8")

        rec._video_path = video
        rec._markers_path = markers

        rec._cleanup_files()

        assert not video.exists()
        assert not markers.exists()


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


class TestFfmpegAvailable:
    """Verify ``ffmpeg_available`` detects ffmpeg."""

    def test_returns_true_when_found(self) -> None:
        with patch(
            "rttr.session_outputs.video_recorder.shutil.which", return_value="/usr/bin/ffmpeg"
        ):
            assert ffmpeg_available() is True

    def test_returns_false_when_missing(self) -> None:
        with patch("rttr.session_outputs.video_recorder.shutil.which", return_value=None):
            assert ffmpeg_available() is False


class TestPrimaryMonitorSize:
    """Verify ``_primary_monitor_size`` returns sensible values."""

    def test_returns_tuple(self) -> None:
        result = _primary_monitor_size()
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_fallback_on_non_windows(self) -> None:
        with patch("rttr.session_outputs.video_recorder.sys.platform", "linux"):
            assert _primary_monitor_size() == (0, 0)


class TestTestSlug:
    """Verify ``_test_slug`` sanitises node IDs."""

    def test_basic(self) -> None:
        assert _test_slug("test.py::test_one") == "test_py__test_one"

    def test_truncation(self) -> None:
        long = "a" * 200
        assert len(_test_slug(long)) == 120


class TestBuildChapters:
    """Verify ``_build_chapters`` parses JSONL markers into chapters."""

    def test_builds_chapters(self, tmp_path: Path) -> None:
        markers = tmp_path / "markers.jsonl"
        markers.write_text(
            '{"event":"test_start","test":"t::a","video_ts":1.0}\n'
            '{"event":"test_end","test":"t::a","video_ts":3.0,"result":"passed"}\n'
            '{"event":"test_start","test":"t::b","video_ts":4.0}\n'
            '{"event":"test_end","test":"t::b","video_ts":5.0,"result":"failed"}\n',
            encoding="utf-8",
        )
        chapters = _build_chapters(markers)
        assert len(chapters) == 2
        # sorted by start_ms
        assert chapters[0][0] == 1000
        assert chapters[0][1] == 3000
        assert "\u2713" in chapters[0][2]  # passed → checkmark
        assert chapters[1][0] == 4000
        assert "\u2717" in chapters[1][2]  # failed → cross

    def test_empty_markers(self, tmp_path: Path) -> None:
        markers = tmp_path / "markers.jsonl"
        markers.write_text("", encoding="utf-8")
        assert _build_chapters(markers) == []

    def test_minimum_chapter_duration(self, tmp_path: Path) -> None:
        markers = tmp_path / "markers.jsonl"
        markers.write_text(
            '{"event":"test_start","test":"t::x","video_ts":5.0}\n'
            '{"event":"test_end","test":"t::x","video_ts":5.0,"result":"passed"}\n',
            encoding="utf-8",
        )
        chapters = _build_chapters(markers)
        assert len(chapters) == 1
        start_ms, end_ms, _title, _result = chapters[0]
        assert end_ms > start_ms  # at least 500ms min

    def test_skips_malformed_json(self, tmp_path: Path) -> None:
        markers = tmp_path / "markers.jsonl"
        markers.write_text(
            "not-json\n"
            '{"event":"test_start","test":"t::a","video_ts":1.0}\n'
            '{"event":"test_end","test":"t::a","video_ts":2.0,"result":"passed"}\n',
            encoding="utf-8",
        )
        chapters = _build_chapters(markers)
        assert len(chapters) == 1

    def test_skips_blank_lines(self, tmp_path: Path) -> None:
        markers = tmp_path / "markers.jsonl"
        markers.write_text(
            "\n"
            '{"event":"test_start","test":"t::a","video_ts":1.0}\n'
            "\n"
            '{"event":"test_end","test":"t::a","video_ts":2.0,"result":"passed"}\n'
            "\n",
            encoding="utf-8",
        )
        chapters = _build_chapters(markers)
        assert len(chapters) == 1


class TestWriteFfmetadata:
    """Verify ``_write_ffmetadata`` writes a valid FFMETADATA1 file."""

    def test_writes_metadata(self, tmp_path: Path) -> None:
        meta = tmp_path / "test.ffmetadata"
        chapters = [(0, 1000, "test_one", "passed"), (1000, 2000, "test_two", "failed")]
        _write_ffmetadata(meta, chapters)

        content = meta.read_text(encoding="utf-8")
        assert ";FFMETADATA1" in content
        assert "[CHAPTER]" in content
        assert "TIMEBASE=1/1000" in content
        assert "START=0" in content
        assert "END=1000" in content
        assert "title=test_one" in content


class TestParseFailedTestRanges:
    """Verify ``_parse_failed_test_ranges`` returns only failed test time ranges."""

    def test_returns_failed_only(self, tmp_path: Path) -> None:
        markers = tmp_path / "markers.jsonl"
        markers.write_text(
            '{"event":"test_start","test":"t::pass","video_ts":1.0}\n'
            '{"event":"test_end","test":"t::pass","video_ts":2.0,"result":"passed"}\n'
            '{"event":"test_start","test":"t::fail","video_ts":3.0}\n'
            '{"event":"test_end","test":"t::fail","video_ts":4.0,"result":"failed"}\n',
            encoding="utf-8",
        )
        result = _parse_failed_test_ranges(markers)
        assert "t::fail" in result
        assert "t::pass" not in result
        assert result["t::fail"] == (3.0, 4.0)

    def test_empty_file(self, tmp_path: Path) -> None:
        markers = tmp_path / "markers.jsonl"
        markers.write_text("", encoding="utf-8")
        assert _parse_failed_test_ranges(markers) == {}


class TestEmbedTestChapters:
    """Verify ``embed_test_chapters`` orchestrates chapter embedding."""

    def test_returns_false_when_files_missing(self, tmp_path: Path) -> None:
        video = tmp_path / "video.mp4"
        markers = tmp_path / "markers.jsonl"
        assert embed_test_chapters(video, markers) is False

    def test_returns_false_when_no_ffmpeg(self, tmp_path: Path) -> None:
        video = tmp_path / "video.mp4"
        markers = tmp_path / "markers.jsonl"
        video.write_bytes(b"data")
        markers.write_text(
            '{"event":"test_start","test":"t::a","video_ts":1.0}\n'
            '{"event":"test_end","test":"t::a","video_ts":2.0,"result":"passed"}\n',
            encoding="utf-8",
        )
        with patch("rttr.session_outputs.video_chapters.ffmpeg_available", return_value=False):
            assert embed_test_chapters(video, markers) is False

    def test_returns_false_when_no_chapters(self, tmp_path: Path) -> None:
        video = tmp_path / "video.mp4"
        markers = tmp_path / "markers.jsonl"
        video.write_bytes(b"data")
        markers.write_text("", encoding="utf-8")
        with patch("rttr.session_outputs.video_chapters.ffmpeg_available", return_value=True):
            assert embed_test_chapters(video, markers) is False

    def test_calls_remux_on_success(self, tmp_path: Path) -> None:
        video = tmp_path / "video.mp4"
        markers = tmp_path / "markers.jsonl"
        video.write_bytes(b"data")
        markers.write_text(
            '{"event":"test_start","test":"t::a","video_ts":1.0}\n'
            '{"event":"test_end","test":"t::a","video_ts":2.0,"result":"passed"}\n',
            encoding="utf-8",
        )
        with (
            patch("rttr.session_outputs.video_chapters.ffmpeg_available", return_value=True),
            patch(
                "rttr.session_outputs.video_chapters._remux_with_chapters", return_value=True
            ) as mock_remux,
        ):
            result = embed_test_chapters(video, markers)
        assert result is True
        mock_remux.assert_called_once()


class TestRemuxWithChapters:
    """Verify ``_remux_with_chapters`` subprocess orchestration."""

    def test_success_replaces_video(self, tmp_path: Path) -> None:
        video = tmp_path / "video.mp4"
        video.write_bytes(b"original")
        meta = tmp_path / "meta.ffmetadata"
        meta.write_text(";FFMETADATA1\n", encoding="utf-8")

        mock_result = MagicMock()
        mock_result.returncode = 0

        tmp_output = video.with_name("video_chapters.mp4")

        with patch("rttr.session_outputs.video_chapters.subprocess.run", return_value=mock_result):
            # Pre-create the temp output file to simulate ffmpeg producing it.
            tmp_output.write_bytes(b"chapters_embedded")
            result = _remux_with_chapters(video, meta)

        assert result is True
        # The tmp_output should have replaced the original.
        assert video.read_bytes() == b"chapters_embedded"

    def test_failure_returns_false(self, tmp_path: Path) -> None:
        video = tmp_path / "video.mp4"
        video.write_bytes(b"data")
        meta = tmp_path / "meta.ffmetadata"
        meta.write_text(";FFMETADATA1\n", encoding="utf-8")

        mock_result = MagicMock()
        mock_result.returncode = 1

        with patch("rttr.session_outputs.video_chapters.subprocess.run", return_value=mock_result):
            result = _remux_with_chapters(video, meta)

        assert result is False

    def test_oserror_returns_false(self, tmp_path: Path) -> None:
        video = tmp_path / "video.mp4"
        video.write_bytes(b"data")
        meta = tmp_path / "meta.ffmetadata"
        meta.write_text(";FFMETADATA1\n", encoding="utf-8")

        with patch(
            "rttr.session_outputs.video_chapters.subprocess.run",
            side_effect=OSError("no ffmpeg"),
        ):
            result = _remux_with_chapters(video, meta)

        assert result is False

    def test_timeout_returns_false(self, tmp_path: Path) -> None:
        video = tmp_path / "video.mp4"
        video.write_bytes(b"data")
        meta = tmp_path / "meta.ffmetadata"
        meta.write_text(";FFMETADATA1\n", encoding="utf-8")

        with patch(
            "rttr.session_outputs.video_chapters.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=30),
        ):
            result = _remux_with_chapters(video, meta)

        assert result is False


class TestEmbedTestChaptersCleanup:
    """Verify metadata file cleanup after embed_test_chapters."""

    def test_cleans_up_metadata_file(self, tmp_path: Path) -> None:
        video = tmp_path / "video.mp4"
        markers = tmp_path / "markers.jsonl"
        video.write_bytes(b"data")
        markers.write_text(
            '{"event":"test_start","test":"t::a","video_ts":1.0}\n'
            '{"event":"test_end","test":"t::a","video_ts":2.0,"result":"passed"}\n',
            encoding="utf-8",
        )
        metadata_path = video.with_suffix(".ffmetadata")

        with (
            patch("rttr.session_outputs.video_chapters.ffmpeg_available", return_value=True),
            patch("rttr.session_outputs.video_chapters._remux_with_chapters", return_value=True),
        ):
            embed_test_chapters(video, markers)

        # Metadata should be cleaned up in the finally block.
        assert not metadata_path.exists()


class TestExtractFailureFrames:
    """Verify ``extract_failure_frames`` orchestration."""

    def test_returns_empty_when_files_missing(self, tmp_path: Path) -> None:
        video = tmp_path / "video.mp4"
        markers = tmp_path / "markers.jsonl"
        assert extract_failure_frames(video, markers, tmp_path / "out") == {}

    def test_returns_empty_when_no_ffmpeg(self, tmp_path: Path) -> None:
        video = tmp_path / "video.mp4"
        markers = tmp_path / "markers.jsonl"
        video.write_bytes(b"data")
        markers.write_text("{}", encoding="utf-8")
        with patch(
            "rttr.session_outputs.test_outputs.failure_frames.ffmpeg_available", return_value=False
        ):
            assert extract_failure_frames(video, markers, tmp_path / "out") == {}

    def test_returns_empty_when_no_failures(self, tmp_path: Path) -> None:
        video = tmp_path / "video.mp4"
        markers = tmp_path / "markers.jsonl"
        video.write_bytes(b"data")
        markers.write_text(
            '{"event":"test_start","test":"t::a","video_ts":1.0}\n'
            '{"event":"test_end","test":"t::a","video_ts":2.0,"result":"passed"}\n',
            encoding="utf-8",
        )
        with patch(
            "rttr.session_outputs.test_outputs.failure_frames.ffmpeg_available", return_value=True
        ):
            assert extract_failure_frames(video, markers, tmp_path / "out") == {}

    def test_extracts_for_failed_tests(self, tmp_path: Path) -> None:
        video = tmp_path / "video.mp4"
        markers = tmp_path / "markers.jsonl"
        video.write_bytes(b"data")
        markers.write_text(
            '{"event":"test_start","test":"t::fail","video_ts":1.0}\n'
            '{"event":"test_end","test":"t::fail","video_ts":2.0,"result":"failed"}\n',
            encoding="utf-8",
        )
        fake_frames = [tmp_path / "frame_0001.png"]
        with (
            patch(
                "rttr.session_outputs.test_outputs.failure_frames.ffmpeg_available",
                return_value=True,
            ),
            patch(
                "rttr.session_outputs.test_outputs.failure_frames._extract_frames",
                return_value=fake_frames,
            ) as mock_extract,
        ):
            result = extract_failure_frames(video, markers, tmp_path / "out")
        assert "t::fail" in result
        mock_extract.assert_called_once()


class TestExtractFramesAt:
    """Verify ``extract_frames_at`` convenience function."""

    def test_returns_empty_when_no_video(self, tmp_path: Path) -> None:
        video = tmp_path / "nonexistent.mp4"
        assert extract_frames_at(video, tmp_path / "out", 5.0) == []

    def test_delegates_to_extract_frames(self, tmp_path: Path) -> None:
        video = tmp_path / "video.mp4"
        video.write_bytes(b"data")
        fake = [tmp_path / "f1.png"]
        with (
            patch(
                "rttr.session_outputs.test_outputs.failure_frames.ffmpeg_available",
                return_value=True,
            ),
            patch(
                "rttr.session_outputs.test_outputs.failure_frames._extract_frames",
                return_value=fake,
            ) as mock,
        ):
            result = extract_frames_at(video, tmp_path / "out", 10.0, window_s=4.0)
        assert result == fake
        mock.assert_called_once()
        # seek should be max(0, 10 - 2) = 8.0
        args = mock.call_args
        assert args[0][2] == 8.0  # seek_s
        assert args[0][3] == 4.0  # duration_s


class TestExtractFrames:
    """Verify ``_extract_frames`` subprocess invocation."""

    def test_calls_ffmpeg(self, tmp_path: Path) -> None:
        video = tmp_path / "video.mp4"
        frame_dir = tmp_path / "frames"
        frame_dir.mkdir()

        with patch("rttr.session_outputs.test_outputs.failure_frames.subprocess.run") as mock_run:
            _extract_frames(video, frame_dir, 1.0, 3.0)

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "ffmpeg"
        assert "-ss" in cmd
        assert "-t" in cmd

    def test_returns_empty_on_error(self, tmp_path: Path) -> None:
        video = tmp_path / "video.mp4"
        frame_dir = tmp_path / "frames"
        frame_dir.mkdir()

        with patch(
            "rttr.session_outputs.test_outputs.failure_frames.subprocess.run",
            side_effect=OSError("nope"),
        ):
            result = _extract_frames(video, frame_dir, 1.0, 3.0)
        assert result == []

    def test_returns_empty_on_timeout(self, tmp_path: Path) -> None:
        video = tmp_path / "video.mp4"
        frame_dir = tmp_path / "frames"
        frame_dir.mkdir()

        with patch(
            "rttr.session_outputs.test_outputs.failure_frames.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=30),
        ):
            result = _extract_frames(video, frame_dir, 1.0, 3.0)
        assert result == []

    def test_returns_sorted_frames(self, tmp_path: Path) -> None:
        video = tmp_path / "video.mp4"
        frame_dir = tmp_path / "frames"
        frame_dir.mkdir()
        # Pre-create frames to simulate ffmpeg output.
        (frame_dir / "frame_0002.png").write_bytes(b"")
        (frame_dir / "frame_0001.png").write_bytes(b"")

        with patch("rttr.session_outputs.test_outputs.failure_frames.subprocess.run"):
            result = _extract_frames(video, frame_dir, 0.0, 5.0)

        assert len(result) == 2
        assert result[0].name == "frame_0001.png"
        assert result[1].name == "frame_0002.png"


class TestKillStaleFfmpeg:
    """Verify ``_kill_stale_ffmpeg`` helper."""

    def test_calls_taskkill_on_windows(self) -> None:
        with (
            patch("rttr.session_outputs.video_recorder.sys.platform", "win32"),
            patch("rttr.session_outputs.video_recorder.subprocess.run") as mock_run,
        ):
            _kill_stale_ffmpeg()

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "taskkill" in cmd

    def test_noop_on_linux(self) -> None:
        with (
            patch("rttr.session_outputs.video_recorder.sys.platform", "linux"),
            patch("rttr.session_outputs.video_recorder.subprocess.run") as mock_run,
        ):
            _kill_stale_ffmpeg()

        mock_run.assert_not_called()

    def test_handles_timeout(self) -> None:
        with (
            patch("rttr.session_outputs.video_recorder.sys.platform", "win32"),
            patch(
                "rttr.session_outputs.video_recorder.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="taskkill", timeout=10),
            ),
        ):
            _kill_stale_ffmpeg()  # should not raise


class TestSessionRecorderStopExtended:
    """Extended stop scenarios — timeout, OSError, delete_on_success."""

    def test_stop_kills_on_timeout(self) -> None:
        rec = SessionRecorder()
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.pid = 12345
        # First wait() raises timeout, second wait() (after kill) succeeds.
        proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="ffmpeg", timeout=15),
            None,
        ]
        rec._process = proc

        rec.stop()

        proc.kill.assert_called_once()
        assert rec._process is None

    def test_stop_handles_oserror(self) -> None:
        rec = SessionRecorder()
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.write.side_effect = OSError("broken pipe")
        proc.pid = 12345
        rec._process = proc

        rec.stop()

        proc.kill.assert_called_once()
        assert rec._process is None

    def test_stop_cleans_up_on_success(self, tmp_path: Path) -> None:
        rec = SessionRecorder()
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.pid = 12345
        rec._process = proc

        video = tmp_path / "session_recording.mp4"
        markers = tmp_path / "session_recording_markers.jsonl"
        video.write_bytes(b"data")
        markers.write_text("{}", encoding="utf-8")
        rec._video_path = video
        rec._markers_path = markers

        rec.stop(delete_on_success=True, had_failures=False)

        assert not video.exists()
        assert not markers.exists()

    def test_stop_keeps_files_on_failure(self, tmp_path: Path) -> None:
        rec = SessionRecorder()
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.pid = 12345
        rec._process = proc

        video = tmp_path / "session_recording.mp4"
        markers = tmp_path / "session_recording_markers.jsonl"
        video.write_bytes(b"data")
        markers.write_text("{}", encoding="utf-8")
        rec._video_path = video
        rec._markers_path = markers

        rec.stop(delete_on_success=True, had_failures=True)

        assert video.exists()
        assert markers.exists()

    def test_write_marker_handles_oserror(self, tmp_path: Path) -> None:
        rec = SessionRecorder()
        rec._markers_path = tmp_path / "readonly" / "markers.jsonl"
        rec._start_time = 100.0
        # Don't create the directory — write will fail.
        rec._write_marker("test_start", "test.py::test_one")
        # Should not raise.

    def test_start_with_downscale(self, tmp_path: Path) -> None:
        rec = SessionRecorder(downscale_720p=True)
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 99999

        with (
            patch("rttr.session_outputs.video_recorder.ffmpeg_available", return_value=True),
            patch("rttr.session_outputs.video_recorder._kill_stale_ffmpeg"),
            patch(
                "rttr.session_outputs.video_recorder.subprocess.Popen", return_value=proc
            ) as mock_popen,
            patch("rttr.session_outputs.video_recorder.time.sleep"),
        ):
            result = rec.start(tmp_path)

        assert result is True
        cmd = mock_popen.call_args[0][0]
        assert "-vf" in cmd
        assert "scale=-2:720" in cmd

    def test_start_with_monitor_size(self, tmp_path: Path) -> None:
        rec = SessionRecorder()
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 99999

        with (
            patch("rttr.session_outputs.video_recorder.ffmpeg_available", return_value=True),
            patch("rttr.session_outputs.video_recorder._kill_stale_ffmpeg"),
            patch(
                "rttr.session_outputs.video_recorder._primary_monitor_size",
                return_value=(1920, 1080),
            ),
            patch(
                "rttr.session_outputs.video_recorder.subprocess.Popen", return_value=proc
            ) as mock_popen,
            patch("rttr.session_outputs.video_recorder.time.sleep"),
        ):
            result = rec.start(tmp_path)

        assert result is True
        cmd = mock_popen.call_args[0][0]
        assert "-video_size" in cmd
        assert "1920x1080" in cmd

    def test_elapsed_returns_positive_when_recording(self) -> None:
        rec = SessionRecorder()
        rec._start_time = time.monotonic() - 5.0
        assert rec.elapsed >= 4.0

    def test_cleanup_handles_missing_files(self, tmp_path: Path) -> None:
        rec = SessionRecorder()
        rec._video_path = tmp_path / "nonexistent.mp4"
        rec._markers_path = tmp_path / "nonexistent.jsonl"
        rec._cleanup_files()  # should not raise

    def test_cleanup_handles_unlink_oserror(self, tmp_path: Path) -> None:
        rec = SessionRecorder()
        video = tmp_path / "session_recording.mp4"
        video.write_bytes(b"data")
        rec._video_path = video
        rec._markers_path = None

        with patch.object(Path, "unlink", side_effect=OSError("perm")):
            rec._cleanup_files()  # should not raise

    def test_primary_monitor_size_error_fallback(self) -> None:
        with (
            patch("rttr.session_outputs.video_recorder.sys.platform", "win32"),
            patch("rttr.session_outputs.video_recorder.ctypes") as mock_ctypes,
        ):
            mock_ctypes.windll.user32.GetSystemMetrics.side_effect = OSError("no api")
            assert _primary_monitor_size() == (0, 0)


class TestSessionRecorderStart:
    """Verify ``start()`` with mocked subprocess."""

    def test_start_succeeds_with_mock_popen(self, tmp_path: Path) -> None:
        rec = SessionRecorder()
        proc = MagicMock()
        proc.poll.return_value = None  # process still running
        proc.pid = 99999

        with (
            patch("rttr.session_outputs.video_recorder.ffmpeg_available", return_value=True),
            patch("rttr.session_outputs.video_recorder._kill_stale_ffmpeg"),
            patch("rttr.session_outputs.video_recorder.subprocess.Popen", return_value=proc),
            patch("rttr.session_outputs.video_recorder.time.sleep"),  # skip the 1.5s delay
        ):
            result = rec.start(tmp_path)

        assert result is True
        assert rec.is_recording
        assert rec.video_path == tmp_path / "session_recording.mp4"

    def test_start_returns_false_when_ffmpeg_exits_immediately(self, tmp_path: Path) -> None:
        rec = SessionRecorder()
        proc = MagicMock()
        proc.poll.return_value = 1  # exited immediately
        proc.returncode = 1

        with (
            patch("rttr.session_outputs.video_recorder.ffmpeg_available", return_value=True),
            patch("rttr.session_outputs.video_recorder._kill_stale_ffmpeg"),
            patch("rttr.session_outputs.video_recorder.subprocess.Popen", return_value=proc),
            patch("rttr.session_outputs.video_recorder.time.sleep"),
        ):
            result = rec.start(tmp_path)

        assert result is False
        assert not rec.is_recording

    def test_start_returns_false_on_file_not_found(self, tmp_path: Path) -> None:
        rec = SessionRecorder()

        with (
            patch("rttr.session_outputs.video_recorder.ffmpeg_available", return_value=True),
            patch("rttr.session_outputs.video_recorder._kill_stale_ffmpeg"),
            patch(
                "rttr.session_outputs.video_recorder.subprocess.Popen",
                side_effect=FileNotFoundError,
            ),
            patch("rttr.session_outputs.video_recorder.time.sleep"),
        ):
            result = rec.start(tmp_path)

        assert result is False

    def test_start_returns_false_on_os_error(self, tmp_path: Path) -> None:
        rec = SessionRecorder()

        with (
            patch("rttr.session_outputs.video_recorder.ffmpeg_available", return_value=True),
            patch("rttr.session_outputs.video_recorder._kill_stale_ffmpeg"),
            patch(
                "rttr.session_outputs.video_recorder.subprocess.Popen",
                side_effect=OSError("access denied"),
            ),
            patch("rttr.session_outputs.video_recorder.time.sleep"),
        ):
            result = rec.start(tmp_path)

        assert result is False
