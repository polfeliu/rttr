"""POM basics — test ``BaseModel`` against the real dummy Qt app."""

from __future__ import annotations

import time

import pytest

from rttr.base import BaseModel, ControlType


class TestClick:
    """Verify ``BaseModel.click()`` on real UI elements."""

    def test_click_increments_counter(self, app_model: BaseModel) -> None:
        btn = app_model.find_element(title="Click Me", control_type=ControlType.BUTTON)
        lbl = app_model.find_element(title_re="Counter:.*", control_type=ControlType.TEXT)
        assert lbl.window_text() == "Counter: 0"
        app_model.click(btn)
        assert lbl.window_text() == "Counter: 1"

    def test_click_twice(self, app_model: BaseModel) -> None:
        btn = app_model.find_element(title="Click Me", control_type=ControlType.BUTTON)
        lbl = app_model.find_element(title_re="Counter:.*", control_type=ControlType.TEXT)
        before = int(lbl.window_text().split(":")[1].strip())
        app_model.click(btn)
        app_model.click(btn)
        assert lbl.window_text() == f"Counter: {before + 2}"


class TestFindElement:
    """Verify ``BaseModel.find_element()`` with various selectors."""

    def test_by_combined_selectors(self, app_model: BaseModel) -> None:
        el = app_model.find_element(title="Click Me", control_type="Button")
        assert el.exists(timeout=2)

    def test_by_title(self, app_model: BaseModel) -> None:
        el = app_model.find_element(title="Click Me")
        assert el.exists(timeout=2)

    def test_by_title_re(self, app_model: BaseModel) -> None:
        el = app_model.find_element(title_re="Click.*")
        assert el.exists(timeout=2)

    def test_by_control_type(self, app_model: BaseModel) -> None:
        el = app_model.find_element(control_type=ControlType.BUTTON, title="Click Me")
        assert el.exists(timeout=2)


class TestExists:
    """Verify ``exists()`` and ``wait_until_closed()``."""

    def test_exists_true(self, app_model: BaseModel) -> None:
        assert app_model.exists(timeout=2)

    def test_exists_for_element(self, app_model: BaseModel) -> None:
        el = app_model.find_element(title="Click Me", control_type=ControlType.BUTTON)
        model = BaseModel(el, name="btn_click")
        assert model.exists(timeout=2)


class TestWait:
    """Verify typed wait helpers."""

    def test_wait_visible(self, app_model: BaseModel) -> None:
        el = app_model.find_element(title="Click Me", control_type=ControlType.BUTTON)
        app_model.wait_visible(el, timeout=3)

    def test_wait_exists(self, app_model: BaseModel) -> None:
        el = app_model.find_element(title_re="Counter:.*", control_type=ControlType.TEXT)
        app_model.wait_exists(el, timeout=3)

    def test_wait_enabled(self, app_model: BaseModel) -> None:
        el = app_model.find_element(title="Click Me", control_type=ControlType.BUTTON)
        app_model.wait_enabled(el, timeout=3)


class TestEnabledState:
    """Verify checkbox toggles the enabled state of a button."""

    def test_uncheck_disables_button(self, app_model: BaseModel) -> None:
        chk = app_model.find_element(title="Enable Button", control_type=ControlType.CHECK_BOX)
        btn = app_model.find_element(title="Toggleable", control_type=ControlType.BUTTON)

        # Initially checked → button enabled
        assert btn.is_enabled()

        # Uncheck → button disabled
        app_model.click(chk)
        time.sleep(0.3)
        assert not btn.is_enabled()

    def test_recheck_enables_button(self, app_model: BaseModel) -> None:
        chk = app_model.find_element(title="Enable Button", control_type=ControlType.CHECK_BOX)
        btn = app_model.find_element(title="Toggleable", control_type=ControlType.BUTTON)

        # Uncheck first
        app_model.click(chk)
        time.sleep(0.3)
        assert not btn.is_enabled()

        # Re-check → button enabled again
        app_model.click(chk)
        time.sleep(0.3)
        assert btn.is_enabled()


class TestTextInput:
    """Verify typing into QLineEdit echoes to label."""

    def test_type_text_echoes(self, app_model: BaseModel) -> None:
        txt = app_model.find_element(control_type="Edit")
        lbl = app_model.find_element(title_re="Echo:.*", control_type=ControlType.TEXT)

        # Click to focus, then type
        app_model.click(txt)
        txt.type_keys("hello", with_spaces=True)
        time.sleep(0.3)
        assert "hello" in lbl.window_text()


class TestAssertions:
    """Verify assertion helpers on real elements."""

    def test_assert_exists(self, app_model: BaseModel) -> None:
        app_model.assert_exists()

    def test_assert_not_exists_raises(self, app_model: BaseModel) -> None:
        with pytest.raises(AssertionError):
            app_model.assert_not_exists()

    def test_assert_element_visible(self, app_model: BaseModel) -> None:
        el = app_model.find_element(title="Click Me", control_type=ControlType.BUTTON)
        app_model.assert_element_visible(el)

    def test_assert_element_enabled(self, app_model: BaseModel) -> None:
        el = app_model.find_element(title="Click Me", control_type=ControlType.BUTTON)
        app_model.assert_element_enabled(el)

    def test_assert_element_enabled_fails_when_disabled(self, app_model: BaseModel) -> None:
        chk = app_model.find_element(title="Enable Button", control_type=ControlType.CHECK_BOX)
        btn = app_model.find_element(title="Toggleable", control_type=ControlType.BUTTON)
        app_model.click(chk)
        time.sleep(0.3)
        with pytest.raises(AssertionError):
            app_model.assert_element_enabled(btn)
