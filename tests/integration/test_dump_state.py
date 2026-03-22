"""dump_state / UINode — test tree capture on the real dummy app."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rttr.base import BaseModel


class TestDumpState:
    """Verify ``dump_state()`` returns meaningful data from a live UI."""

    def test_dump_state_returns_string(self, app_model: BaseModel) -> None:
        state = app_model.dump_state()
        assert isinstance(state, str)
        assert len(state) > 0

    def test_dump_state_contains_known_elements(self, app_model: BaseModel) -> None:
        state = app_model.dump_state()
        assert "Click Me" in state
        assert "Counter:" in state

    def test_dump_state_contains_control_types(self, app_model: BaseModel) -> None:
        state = app_model.dump_state()
        assert "Button" in state

    def test_dump_state_contains_auto_ids(self, app_model: BaseModel) -> None:
        state = app_model.dump_state()
        # Verify the Qt class names appear in the dump
        assert "QPushButton" in state
        assert "QLabel" in state

    def test_dump_state_has_rect(self, app_model: BaseModel) -> None:
        state = app_model.dump_state()
        # Should contain at least one rect= string
        assert "rect=(" in state

    def test_dump_state_has_indentation(self, app_model: BaseModel) -> None:
        state = app_model.dump_state()
        lines = state.strip().splitlines()
        # Should have nested elements (indented lines)
        indented = [line for line in lines if line.startswith("  ")]
        assert len(indented) > 0, "Expected indented child elements in dump"
