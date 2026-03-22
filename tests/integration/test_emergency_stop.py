"""Emergency stop — verify F10 hotkey registration works in a live session."""

from __future__ import annotations

from unittest.mock import patch

from rttr.emergency_stop import _emergency_stop, register


class TestEmergencyStop:
    """Verify the F10 emergency stop registration."""

    def test_register_binds_f10(self) -> None:
        with patch("rttr.emergency_stop.keyboard") as mock_kb:
            register()
            mock_kb.add_hotkey.assert_called_once()
            args = mock_kb.add_hotkey.call_args
            assert args[0][0] == "f10"

    def test_register_handles_os_error(self) -> None:
        with patch("rttr.emergency_stop.keyboard") as mock_kb:
            mock_kb.add_hotkey.side_effect = OSError("no desktop")
            # Should NOT raise
            register()

    def test_emergency_stop_interrupts_main_thread(self) -> None:
        with patch("rttr.emergency_stop._thread") as mock_thread:
            _emergency_stop()
            mock_thread.interrupt_main.assert_called_once()
