"""Tests for ``rttr.hooks`` — pytest hooks: CLI, collection, phase reports."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from rttr import _state
from rttr.hooks import (
    _DEFAULT_TEST_TIMEOUT_S,
    phase_report_key,
    pytest_addoption,
    pytest_collection_modifyitems,
    pytest_report_header,
    store_phase_report,
)


class TestPhaseReportKey:
    """Verify the StashKey for phase reports."""

    def test_is_stash_key(self) -> None:
        assert isinstance(phase_report_key, pytest.StashKey)


class TestStorePhaseReport:
    """Verify ``store_phase_report`` stashes reports on items."""

    def test_stashes_report(self) -> None:
        item = MagicMock(spec=pytest.Item)
        item.stash = pytest.Stash()

        rep = MagicMock()
        rep.when = "call"
        rep.failed = True
        rep.skipped = False

        store_phase_report(item, rep)

        stashed = item.stash.get(phase_report_key, {})
        assert "call" in stashed
        assert stashed["call"] is rep

    def test_stashes_multiple_phases(self) -> None:
        item = MagicMock(spec=pytest.Item)
        item.stash = pytest.Stash()

        for phase in ("setup", "call", "teardown"):
            rep = MagicMock()
            rep.when = phase
            store_phase_report(item, rep)

        stashed = item.stash.get(phase_report_key, {})
        assert set(stashed.keys()) == {"setup", "call", "teardown"}


class TestPytestAddoption:
    """Verify ``pytest_addoption`` registers ``--no-record``."""

    def test_registers_no_record(self) -> None:
        parser = MagicMock()
        pytest_addoption(parser)
        parser.addoption.assert_called_once()
        args, kwargs = parser.addoption.call_args
        assert "--no-record" in args
        assert kwargs.get("action") == "store_true"


class TestPytestReportHeader:
    """Verify ``pytest_report_header`` returns the seed."""

    def test_returns_seed_when_present(self) -> None:
        config = MagicMock()
        config._randomly_seed = 42
        result = pytest_report_header(config)
        assert "42" in result

    def test_returns_empty_when_no_seed(self) -> None:
        config = MagicMock(spec=[])  # no _randomly_seed attribute
        result = pytest_report_header(config)
        assert result == ""


class TestPytestCollectionModifyItems:
    """Verify ``pytest_collection_modifyitems`` records nodeids and applies timeout."""

    def test_records_collected_nodeids(self) -> None:
        items = [
            MagicMock(nodeid="tests/test_a.py::test_one"),
            MagicMock(nodeid="tests/test_b.py::test_two"),
        ]
        # Mark items as having no timeout marker
        for item in items:
            item.get_closest_marker.return_value = None

        config = MagicMock()
        _state.session.collected_nodeids.clear()

        pytest_collection_modifyitems(config, items)

        assert _state.session.collected_nodeids == [
            "tests/test_a.py::test_one",
            "tests/test_b.py::test_two",
        ]

    def test_applies_default_timeout_to_unmarked_tests(self) -> None:
        item = MagicMock(nodeid="test.py::test_x")
        item.get_closest_marker.return_value = None

        pytest_collection_modifyitems(MagicMock(), [item])

        item.add_marker.assert_called_once()
        marker = item.add_marker.call_args[0][0]
        assert marker.args == (_DEFAULT_TEST_TIMEOUT_S,)

    def test_does_not_override_existing_timeout(self) -> None:
        item = MagicMock(nodeid="test.py::test_x")
        item.get_closest_marker.return_value = MagicMock()  # has timeout marker

        pytest_collection_modifyitems(MagicMock(), [item])

        item.add_marker.assert_not_called()
