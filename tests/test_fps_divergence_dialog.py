"""Tests for the pure logic in recorder.gui.fps_divergence_dialog (no QApplication needed)."""
from __future__ import annotations

from recorder.gui.fps_divergence_dialog import (DEFAULT_IGNORE_MINUTES,
                                                ignore_seconds_for)


def test_ignore_until_stopped_has_no_time_limit():
    assert ignore_seconds_for(until_stopped=True, minutes=30) is None


def test_ignore_for_minutes_converts_to_seconds():
    assert ignore_seconds_for(until_stopped=False, minutes=10) == 600.0
    assert ignore_seconds_for(until_stopped=False, minutes=1) == 60.0


def test_default_ignore_minutes_is_ten():
    assert DEFAULT_IGNORE_MINUTES == 10
