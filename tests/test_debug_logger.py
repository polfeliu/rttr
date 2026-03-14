"""Tests for ``rttr.session_outputs.debug_logger`` — DebugLogger and helper functions."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from rttr.session_outputs.debug_logger import DebugLogger, _format_tree, _slugify


class TestSlugify:
    """Verify ``_slugify`` converts text to safe filesystem slugs."""

    def test_basic(self) -> None:
        assert _slugify("Hello World!") == "hello_world"

    def test_preserves_numbers(self) -> None:
        assert _slugify("test_123") == "test_123"

    def test_strips_leading_trailing(self) -> None:
        assert _slugify("---hello---") == "hello"

    def test_truncates_at_60_chars(self) -> None:
        long = "a" * 100
        assert len(_slugify(long)) == 60

    def test_empty_string(self) -> None:
        assert _slugify("   ") == ""

    def test_special_characters(self) -> None:
        assert _slugify("test[param=1]::sub") == "test_param_1_sub"


class TestFormatTree:
    """Verify ``_format_tree`` produces readable output from pywinauto elements."""

    def _make_element(
        self,
        ctrl_type: str = "Button",
        title: str = "OK",
        enabled: bool = True,
        visible: bool = True,
        children: list[MagicMock] | None = None,
    ) -> MagicMock:
        el = MagicMock()
        el.element_info.control_type = ctrl_type
        el.window_text.return_value = title
        el.is_enabled.return_value = enabled
        el.is_visible.return_value = visible
        el.children.return_value = children or []
        return el

    def test_single_element(self) -> None:
        el = self._make_element()
        result = _format_tree(el)
        assert "[Button]" in result
        assert "title='OK'" in result
        assert "enabled=True" in result

    def test_nested_children(self) -> None:
        child = self._make_element(ctrl_type="Text", title="Label")
        parent = self._make_element(ctrl_type="Pane", title="Container", children=[child])
        result = _format_tree(parent)
        assert "[Pane]" in result
        assert "[Text]" in result

    def test_respects_max_depth(self) -> None:
        el = self._make_element()
        result = _format_tree(el, depth=15, max_depth=12)
        assert result == ""


class TestDebugLogger:
    """Verify ``DebugLogger`` session and test lifecycle."""

    def test_creates_session_dir(self, tmp_path: Path) -> None:
        dl = DebugLogger(sessions_root=tmp_path)
        assert dl.session_dir.exists()
        assert dl.session_dir.parent == tmp_path

    def test_session_dir_name_format(self, tmp_path: Path) -> None:
        dl = DebugLogger(sessions_root=tmp_path)
        name = dl.session_dir.name
        assert name.startswith("001_session_")

    def test_new_test_increments_counter(self, tmp_path: Path) -> None:
        dl = DebugLogger(sessions_root=tmp_path)
        dl.new_test("test_alpha")
        assert dl._test_counter == 1
        dl.new_test("test_beta")
        assert dl._test_counter == 2

    def test_new_test_sets_dir_path(self, tmp_path: Path) -> None:
        dl = DebugLogger(sessions_root=tmp_path)
        dl.new_test("test_example")
        assert dl.test_dir is not None
        assert "test_example" in dl.test_dir.name

    def test_test_dir_not_created_until_needed(self, tmp_path: Path) -> None:
        dl = DebugLogger(sessions_root=tmp_path)
        dl.new_test("test_lazy")
        assert dl.test_dir is not None
        assert not dl.test_dir.exists()  # lazy — not created yet

    def test_ensure_test_dir_creates_on_disk(self, tmp_path: Path) -> None:
        dl = DebugLogger(sessions_root=tmp_path)
        dl.new_test("test_eager")
        path = dl._ensure_test_dir()
        assert path.exists()

    def test_record_result_appends(self, tmp_path: Path) -> None:
        dl = DebugLogger(sessions_root=tmp_path)
        dl.new_test("test_one")
        dl.record_result("PASSED")

        assert len(dl._results) == 1
        assert dl._results[0].status == "PASSED"
        assert dl._results[0].name == "test_one"

    def test_discard_test_dir_removes_directory(self, tmp_path: Path) -> None:
        dl = DebugLogger(sessions_root=tmp_path)
        dl.new_test("test_discard")
        dl._ensure_test_dir()
        assert dl.test_dir is not None
        assert dl.test_dir.exists()

        dl.discard_test_dir()
        # After discard, the dir should be gone.
        assert dl._test_dir is None

    def test_discard_test_dir_noop_if_not_created(self, tmp_path: Path) -> None:
        dl = DebugLogger(sessions_root=tmp_path)
        dl.new_test("test_noop")
        dl.discard_test_dir()  # should not raise

    def test_capture_creates_state_dir(self, tmp_path: Path) -> None:
        dl = DebugLogger(sessions_root=tmp_path)
        dl.new_test("test_capture")

        # Patch ImageGrab to avoid needing a real screen.
        mock_img = MagicMock()
        with patch("rttr.session_outputs.debug_logger.ImageGrab.grab", return_value=mock_img):
            state_dir = dl.capture("initial_state")

        assert state_dir.exists()
        assert (state_dir / "state.txt").exists()
        mock_img.save.assert_called_once()

    def test_capture_writes_tree_for_element(self, tmp_path: Path) -> None:
        dl = DebugLogger(sessions_root=tmp_path)
        dl.new_test("test_tree")

        el = MagicMock()
        el.element_info.control_type = "Button"
        el.window_text.return_value = "MyButton"
        el.is_enabled.return_value = True
        el.is_visible.return_value = True
        el.children.return_value = []

        mock_img = MagicMock()
        with patch("rttr.session_outputs.debug_logger.ImageGrab.grab", return_value=mock_img):
            state_dir = dl.capture("with_element", element=el)

        content = (state_dir / "state.txt").read_text(encoding="utf-8")
        assert "MyButton" in content

    def test_capture_without_element(self, tmp_path: Path) -> None:
        dl = DebugLogger(sessions_root=tmp_path)
        dl.new_test("test_no_el")

        mock_img = MagicMock()
        with patch("rttr.session_outputs.debug_logger.ImageGrab.grab", return_value=mock_img):
            state_dir = dl.capture("no_element", element=None)

        content = (state_dir / "state.txt").read_text(encoding="utf-8")
        assert "No element provided" in content

    def test_sequential_sessions_increment_index(self, tmp_path: Path) -> None:
        dl1 = DebugLogger(sessions_root=tmp_path)
        dl2 = DebugLogger(sessions_root=tmp_path)
        assert dl1.session_dir.name.startswith("001_")
        assert dl2.session_dir.name.startswith("002_")
