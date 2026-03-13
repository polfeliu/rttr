"""Pytest hooks for the RTTR plugin: CLI options, collection, timeouts, phase reports.

These are "simple" hooks that belong to a single concern and can be
re-exported directly from ``rttr.__init__``.
"""

from __future__ import annotations

import pytest
from pytest import CollectReport, StashKey

from rttr import _state

# ---------------------------------------------------------------------------
# Stash key used to expose per-phase reports to fixtures and other hooks
# ---------------------------------------------------------------------------

phase_report_key = StashKey[dict[str, CollectReport]]()

# ---------------------------------------------------------------------------
# CLI options
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add custom CLI options for test configuration.

    Args:
        parser: The pytest argument parser.
    """
    parser.addoption(
        "--no-record",
        action="store_true",
        default=False,
        help="Disable screen recording (ffmpeg) for this session.",
    )


# ---------------------------------------------------------------------------
# pytest-randomly header
# ---------------------------------------------------------------------------


def pytest_report_header(config: pytest.Config) -> str:
    """Report the randomly seed in the test header for reproducibility.

    Args:
        config: The pytest Config object.

    Returns:
        A header line showing the seed value.
    """
    seed: object = getattr(config, "_randomly_seed", None)
    if seed is not None:
        return f"pytest-randomly seed: {seed}"
    return ""


# ---------------------------------------------------------------------------
# Default timeout & collection
# ---------------------------------------------------------------------------

_DEFAULT_TEST_TIMEOUT_S: int = 300
"""Default wall-clock timeout applied to tests that do not carry an explicit
``@pytest.mark.timeout`` marker."""


def pytest_collection_modifyitems(
    config: pytest.Config,  # noqa: ARG001
    items: list[pytest.Item],
) -> None:
    """Record collected tests and apply default timeout to unmarked tests.

    Args:
        config: The pytest Config object (unused).
        items: The collected test items.
    """
    _state.collected_nodeids.clear()
    _state.collected_nodeids.extend(item.nodeid for item in items)

    for item in items:
        if not item.get_closest_marker("timeout"):
            item.add_marker(pytest.mark.timeout(_DEFAULT_TEST_TIMEOUT_S))


# ---------------------------------------------------------------------------
# Phase report stashing
# ---------------------------------------------------------------------------


def store_phase_report(item: pytest.Item, rep: CollectReport) -> None:
    """Stash a phase report on the test item for later consumption by fixtures.

    Args:
        item: The pytest Item.
        rep: The CollectReport for this phase.
    """
    item.stash.setdefault(phase_report_key, {})[rep.when] = rep
