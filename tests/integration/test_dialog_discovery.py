"""Dialog discovery — test window/dialog lookup methods."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
from pywinauto.keyboard import send_keys

from rttr.base import BaseModel

if TYPE_CHECKING:
    import subprocess


class TestDialogDiscovery:
    """Verify dialog discovery methods against the real dummy app."""

    def _open_dialog_via_menu(self, app_model: BaseModel) -> None:
        """Open the dialog through File > Open Dialog... menu."""
        app_model.element.set_focus()
        time.sleep(0.3)
        file_menu = app_model.element.child_window(title="File", control_type="MenuItem")
        file_menu.click_input()
        time.sleep(0.5)
        # After clicking File, the popup menu appears as a child of the menu bar
        open_item = app_model.element.child_window(title="Open Dialog...", control_type="MenuItem")
        open_item.click_input()
        time.sleep(0.5)

    def test_get_top_level_dialog_by_title(
        self,
        app_model: BaseModel,
    ) -> None:
        self._open_dialog_via_menu(app_model)
        try:
            dlg_spec = app_model.get_top_level_dialog("Test Dialog", timeout=5)
            assert dlg_spec.exists(timeout=2)
            assert dlg_spec.window_text() == "Test Dialog"
        finally:
            send_keys("{ESC}")
            time.sleep(0.3)

    def test_get_top_level_dialog_timeout_on_missing(self, app_model: BaseModel) -> None:
        with pytest.raises(RuntimeError, match="not found"):
            app_model.get_top_level_dialog("NonExistent Dialog", timeout=1)

    def test_dialog_ok_closes(self, app_model: BaseModel) -> None:
        self._open_dialog_via_menu(app_model)
        dlg_spec = app_model.get_top_level_dialog("Test Dialog", timeout=5)
        dlg_model = BaseModel(dlg_spec, name="TestDialog")

        # Click OK
        ok_btn = dlg_model.find_element(title="OK", control_type="Button")
        dlg_model.click(ok_btn)
        time.sleep(0.5)

        # Dialog should be gone
        with pytest.raises(RuntimeError, match="not found"):
            app_model.get_top_level_dialog("Test Dialog", timeout=1)

    def test_dialog_cancel_closes(self, app_model: BaseModel) -> None:
        self._open_dialog_via_menu(app_model)
        dlg_spec = app_model.get_top_level_dialog("Test Dialog", timeout=5)
        dlg_model = BaseModel(dlg_spec, name="TestDialog")

        # Click Cancel
        cancel_btn = dlg_model.find_element(title="Cancel", control_type="Button")
        dlg_model.click(cancel_btn)
        time.sleep(0.5)

        with pytest.raises(RuntimeError, match="not found"):
            app_model.get_top_level_dialog("Test Dialog", timeout=1)

    def test_menu_opens_dialog(self, app_model: BaseModel) -> None:
        self._open_dialog_via_menu(app_model)
        try:
            dlg_spec = app_model.get_top_level_dialog("Test Dialog", timeout=5)
            assert dlg_spec.exists(timeout=2)
        finally:
            send_keys("{ESC}")
            time.sleep(0.5)

        # After dialog closes, status should be back to Ready
        lbl = app_model.find_element(title_re="Status:.*", control_type="Text")
        assert "Ready" in lbl.window_text()

    def test_find_window_handles(
        self,
        dummy_app_process: subprocess.Popen[bytes],
    ) -> None:
        handles = BaseModel._find_window_handles(dummy_app_process.pid, title="RTTR Dummy App")
        assert len(handles) >= 1

    def test_get_active_dialog(self, app_model: BaseModel) -> None:
        self._open_dialog_via_menu(app_model)
        try:
            active = app_model.get_active_dialog()
            # Should return something different from the main window
            # (the dialog is now foreground)
            assert active.exists(timeout=2)
        finally:
            send_keys("{ESC}")
            time.sleep(0.3)
