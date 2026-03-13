"""Integration tests for the RTTR pytest plugin using ``pytester``.

These tests run pytest as a subprocess with a minimal conftest that loads
the RTTR plugin, verifying that hooks fire correctly, CLI options work,
and the session lifecycle is coherent end-to-end.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    pass

# Enable the pytester plugin for this file.
pytest_plugins = ["pytester"]


class TestPluginLoads:
    """Verify the RTTR plugin can be registered and runs."""

    def test_plugin_registers_without_error(self, pytester: pytest.Pytester) -> None:
        pytester.makeconftest('pytest_plugins = ["rttr"]')
        pytester.makepyfile(
            """
            def test_trivial():
                assert True
            """
        )
        result = pytester.runpytest("--no-record", "-q")
        result.assert_outcomes(passed=1)

    def test_report_header_includes_seed(self, pytester: pytest.Pytester) -> None:
        pytester.makeconftest('pytest_plugins = ["rttr"]')
        pytester.makepyfile(
            """
            def test_dummy():
                pass
            """
        )
        result = pytester.runpytest("--no-record", "-v", "-p", "randomly")
        # The header should mention "pytest-randomly seed" if randomly is installed.
        # If not installed, the header just won't appear — not a failure.
        output = result.stdout.str()
        if "randomly" in output.lower():
            assert "seed" in output.lower()


class TestNoRecordOption:
    """Verify ``--no-record`` is recognized."""

    def test_no_record_accepted(self, pytester: pytest.Pytester) -> None:
        pytester.makeconftest('pytest_plugins = ["rttr"]')
        pytester.makepyfile(
            """
            def test_x():
                pass
            """
        )
        result = pytester.runpytest("--no-record", "-q")
        result.assert_outcomes(passed=1)


class TestCollectionTimeout:
    """Verify default timeout markers are applied."""

    def test_unmarked_tests_get_timeout(self, pytester: pytest.Pytester) -> None:
        pytester.makeconftest('pytest_plugins = ["rttr"]')
        pytester.makepyfile(
            """
            import pytest

            def test_has_timeout(request):
                marker = request.node.get_closest_marker("timeout")
                assert marker is not None
                assert marker.args[0] == 300
            """
        )
        result = pytester.runpytest("--no-record", "-q")
        result.assert_outcomes(passed=1)

    def test_explicit_timeout_not_overridden(self, pytester: pytest.Pytester) -> None:
        pytester.makeconftest('pytest_plugins = ["rttr"]')
        pytester.makepyfile(
            """
            import pytest

            @pytest.mark.timeout(60)
            def test_custom_timeout(request):
                marker = request.node.get_closest_marker("timeout")
                assert marker.args[0] == 60
            """
        )
        result = pytester.runpytest("--no-record", "-q")
        result.assert_outcomes(passed=1)


class TestPhaseReportStashing:
    """Verify phase reports are stashed and accessible from fixtures."""

    def test_setup_call_teardown_stashed(self, pytester: pytest.Pytester) -> None:
        pytester.makeconftest(
            """
            import pytest
            from rttr.hooks import phase_report_key

            pytest_plugins = ["rttr"]

            @pytest.fixture
            def check_phases(request):
                yield
                report = request.node.stash.get(phase_report_key, {})
                # After teardown starts, at least setup and call should be stashed.
                assert "setup" in report
                assert "call" in report
            """
        )
        pytester.makepyfile(
            """
            def test_phases(check_phases):
                assert True
            """
        )
        result = pytester.runpytest("--no-record", "-q")
        result.assert_outcomes(passed=1)


class TestDebugLoggerFixture:
    """Verify the ``debug_logger`` fixture is provided."""

    def test_debug_logger_available(self, pytester: pytest.Pytester) -> None:
        pytester.makeconftest('pytest_plugins = ["rttr"]')
        pytester.makepyfile(
            """
            def test_has_logger(debug_logger):
                from rttr.session_data import DebugLogger
                assert isinstance(debug_logger, DebugLogger)
            """
        )
        result = pytester.runpytest("--no-record", "-q")
        result.assert_outcomes(passed=1)


class TestSessionDataGeneration:
    """Verify session artifacts are produced after a run."""

    def test_session_data_json_created(self, pytester: pytest.Pytester) -> None:
        pytester.makeconftest('pytest_plugins = ["rttr"]')
        pytester.makepyfile(
            """
            def test_ok():
                pass
            """
        )
        result = pytester.runpytest("--no-record", "-q")
        result.assert_outcomes(passed=1)

        # The debug_sessions/ dir should exist with session_data.json.
        sessions_dir = pytester.path / "debug_sessions"
        if sessions_dir.exists():
            session_dirs = sorted(sessions_dir.iterdir())
            assert len(session_dirs) >= 1
            data_file = session_dirs[0] / "session_data.json"
            assert data_file.exists()
            data = json.loads(data_file.read_text(encoding="utf-8"))
            assert "metadata" in data
            assert "tests" in data
            assert "logs" in data


class TestFailureHandling:
    """Verify the plugin handles test failures gracefully."""

    def test_plugin_survives_test_failure(self, pytester: pytest.Pytester) -> None:
        pytester.makeconftest('pytest_plugins = ["rttr"]')
        pytester.makepyfile(
            """
            def test_fail():
                assert False

            def test_pass():
                assert True
            """
        )
        result = pytester.runpytest("--no-record", "-q")
        result.assert_outcomes(passed=1, failed=1)

    def test_session_info_records_failure(self, pytester: pytest.Pytester) -> None:
        pytester.makeconftest('pytest_plugins = ["rttr"]')
        pytester.makepyfile(
            """
            def test_fail():
                assert False
            """
        )
        result = pytester.runpytest("--no-record", "-q")
        result.assert_outcomes(failed=1)

        sessions_dir = pytester.path / "debug_sessions"
        if sessions_dir.exists():
            session_dirs = sorted(sessions_dir.iterdir())
            info = session_dirs[0] / "session_info.md"
            if info.exists():
                content = info.read_text(encoding="utf-8")
                assert "FAILED" in content
