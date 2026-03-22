"""MCP tools — test key server functions against the live dummy app.

These tests import the MCP server module's internal helpers directly
rather than going through the full FastMCP protocol, since the tool
functions are normal Python callables.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pythoncom
from pywinauto import Desktop

from rttr.mcp.server import (
    _ELEMENT_REGISTRY,
    _RUNTIME_TO_SLOT,
    _element_to_dict,
    _register_element,
    _resolve_element,
)

if TYPE_CHECKING:
    import subprocess

    from rttr.base import BaseModel


@pytest.fixture(autouse=True)
def _clear_mcp_registry() -> None:
    """Clear the MCP element registry between tests."""
    _ELEMENT_REGISTRY.clear()
    _RUNTIME_TO_SLOT.clear()


class TestElementRegistry:
    """Verify the element registry used by MCP tools."""

    def test_register_and_resolve(
        self,
        dummy_app_process: subprocess.Popen[bytes],
    ) -> None:
        pythoncom.CoInitialize()
        pid = dummy_app_process.pid
        windows = [w for w in Desktop(backend="uia").windows() if w.process_id() == pid]
        assert len(windows) >= 1
        main_win = windows[0]

        slot = _register_element(main_win)
        assert isinstance(slot, int)
        assert slot > 0

        resolved = _resolve_element(slot)
        assert resolved is main_win

    def test_resolve_unknown_slot_raises(self) -> None:
        with pytest.raises(RuntimeError, match="not found"):
            _resolve_element(999999)

    def test_register_deduplicates(
        self,
        dummy_app_process: subprocess.Popen[bytes],
    ) -> None:
        pythoncom.CoInitialize()
        pid = dummy_app_process.pid
        windows = [w for w in Desktop(backend="uia").windows() if w.process_id() == pid]
        main_win = windows[0]

        slot1 = _register_element(main_win)
        slot2 = _register_element(main_win)
        assert slot1 == slot2


class TestElementSerialization:
    """Verify ``_element_to_dict`` on real elements."""

    def test_element_to_dict_main_window(
        self,
        dummy_app_process: subprocess.Popen[bytes],
    ) -> None:
        pythoncom.CoInitialize()
        pid = dummy_app_process.pid
        windows = [w for w in Desktop(backend="uia").windows() if w.process_id() == pid]
        main_win = windows[0]

        d = _element_to_dict(main_win)
        assert "handle" in d
        assert "title" in d
        assert "RTTR Dummy App" in d["title"]
        assert "rect" in d

    def test_element_to_dict_child(self, app_model: BaseModel) -> None:
        pythoncom.CoInitialize()
        btn = app_model.find_element(title="Click Me", control_type="Button").wrapper_object()
        d = _element_to_dict(btn)
        assert d["title"] == "Click Me"
        assert d["control_type"] == "Button"
