"""Test-level output management — per-test debug directories with UI dumps and screenshots."""

from __future__ import annotations

from rttr.session_outputs.test_outputs.failure_frames import (
    extract_failure_frames,
    extract_frames_at,
)

__all__ = [
    "extract_failure_frames",
    "extract_frames_at",
]
