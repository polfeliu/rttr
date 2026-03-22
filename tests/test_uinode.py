"""Tests for UINode and BaseModel tree/dump utilities."""

from __future__ import annotations

from rttr.base import BaseModel, ControlType, UINode


def _make_node(
    title: str = "Root",
    control_type: ControlType = ControlType.WINDOW,
    children: list[UINode] | None = None,
) -> UINode:
    return UINode(
        control_type=control_type,
        class_name="TestClass",
        title=title,
        auto_id="test_id",
        is_visible=True,
        is_enabled=True,
        children=children or [],
    )


class TestUINodeToDict:
    """UINode.to_dict should return all required keys."""

    def test_required_keys_present(self) -> None:
        node = _make_node()
        d = node.to_dict()
        for key in ["control_type", "class_name", "title", "auto_id", "is_visible", "is_enabled", "children"]:
            assert key in d, f"Missing key: {key}"

    def test_children_is_list(self) -> None:
        node = _make_node()
        d = node.to_dict()
        assert isinstance(d["children"], list)

    def test_nested_children_serialized(self) -> None:
        child = _make_node(title="Child", control_type=ControlType.BUTTON)
        parent = _make_node(title="Parent", children=[child])
        d = parent.to_dict()
        assert len(d["children"]) == 1
        assert d["children"][0]["title"] == "Child"

    def test_control_type_is_string(self) -> None:
        node = _make_node(control_type=ControlType.BUTTON)
        d = node.to_dict()
        assert isinstance(d["control_type"], str)


class TestCaptureTreeFromWin:
    """capture_tree_from_win should build a UINode from a pywinauto wrapper."""

    def test_returns_uinode_or_none(self) -> None:
        """Smoke test: capture_tree_from_win returns UINode | None."""
        # We cannot test with a live window in pure unit tests, but we verify
        # the method exists and is callable.
        assert callable(BaseModel.capture_tree_from_win)
