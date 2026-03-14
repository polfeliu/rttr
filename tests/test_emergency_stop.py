"""Tests for ``rttr.emergency_stop`` — F10 hotkey registration."""

from __future__ import annotations

from unittest.mock import patch

from rttr.emergency_stop import _emergency_stop, register


class TestRegister:
    """Verify ``register`` binds the F10 hotkey."""

    def test_binds_f10(self) -> None:
        with patch("rttr.emergency_stop.keyboard") as mock_kb:
            register()
            mock_kb.add_hotkey.assert_called_once_with("f10", _emergency_stop)

    def test_logs_warning_on_os_error(self) -> None:
        with (
            patch("rttr.emergency_stop.keyboard") as mock_kb,
            patch("rttr.emergency_stop.logger") as mock_logger,
        ):
            mock_kb.add_hotkey.side_effect = OSError("no keyboard")
            register()
            mock_logger.warning.assert_called_once()

    def test_logs_warning_on_value_error(self) -> None:
        with (
            patch("rttr.emergency_stop.keyboard") as mock_kb,
            patch("rttr.emergency_stop.logger") as mock_logger,
        ):
            mock_kb.add_hotkey.side_effect = ValueError("bad key")
            register()
            mock_logger.warning.assert_called_once()


class TestEmergencyStop:
    """Verify ``_emergency_stop`` interrupts the main thread."""

    def test_interrupts_main_thread(self) -> None:
        with patch("rttr.emergency_stop._thread.interrupt_main") as mock_interrupt:
            _emergency_stop()
            mock_interrupt.assert_called_once()
