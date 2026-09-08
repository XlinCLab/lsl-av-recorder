"""Tests for recorder.video.video_recorder.VideoRecorder throttling logic.

VideoRecorder opens no camera until start(), so its pre-capture logic is
testable directly by constructing one with a VideoCamConfig: the preview
frame-rate limiter, the expected-fps precedence, and the capture-fps deviation
warning (including its throttle). No hardware is touched.
"""
from __future__ import annotations

import time

import numpy as np

from recorder.config import VideoCamConfig
from recorder.video.video_recorder import VideoRecorder


def _recorder(*, fps=30, preview_cb=None, preview_fps=None, status_cb=None):
    """Build a VideoRecorder for a single camera without opening hardware."""
    return VideoRecorder(
        cam_cfg=VideoCamConfig(Enabled=True, Label="Cam", FPS=fps),
        output_path="unused.mp4",
        status_cb=status_cb,
        preview_cb=preview_cb,
        preview_fps=preview_fps,
    )


def _frame():
    return np.zeros((2, 2, 3), dtype=np.uint8)


def _collect_warnings(logs: list[tuple[str, str]]) -> list[str]:
    warnings = [msg for level, msg in logs if level == "WARNING"]
    return warnings


# ---------------------------------------------------------------------------
# Preview interval derivation (__init__)
# ---------------------------------------------------------------------------

def test_preview_interval_derived_from_fps():
    """A positive preview_fps with a preview callback yields the reciprocal as the emit interval."""
    rec = _recorder(preview_cb=lambda f: None, preview_fps=10.0)
    assert rec._preview_interval == 0.1


def test_preview_interval_none_without_callback():
    """No preview callback means no preview interval (nothing to throttle)."""
    rec = _recorder(preview_cb=None, preview_fps=10.0)
    assert rec._preview_interval is None


def test_preview_interval_none_for_nonpositive_fps():
    """A zero or negative preview_fps leaves the interval unset."""
    assert _recorder(preview_cb=lambda f: None, preview_fps=0)._preview_interval is None
    assert _recorder(preview_cb=lambda f: None, preview_fps=-5)._preview_interval is None


# ---------------------------------------------------------------------------
# _expected_fps precedence
# ---------------------------------------------------------------------------

def test_expected_fps_prefers_configured_fps():
    """cam.FPS (what the operator actually configured/expects) wins over
    everything else -- deliberately NOT writer_fps, which is a short,
    early-in-capture measurement used only so the VideoWriter's own
    declared fps matches real elapsed time. A camera whose genuinely
    achievable rate differs from what was configured should keep being
    compared against the configured value for the whole recording, not
    silently re-baseline to whatever writer_fps happened to measure."""
    rec = _recorder(fps=30)
    rec.writer_fps = 25.0
    rec._reported_fps = 60.0
    assert rec._expected_fps() == 30.0


def test_expected_fps_falls_back_to_reported_then_writer_fps():
    """Without a configured cam.FPS, the driver-reported fps is used;
    without that, the measured writer_fps as a last resort."""
    rec = _recorder(fps=0)
    rec._reported_fps = 24.0
    rec.writer_fps = 18.0
    assert rec._expected_fps() == 24.0
    rec._reported_fps = None
    assert rec._expected_fps() == 18.0
    rec.writer_fps = None
    assert rec._expected_fps() == 0.0


# ---------------------------------------------------------------------------
# _maybe_warn_fps
# ---------------------------------------------------------------------------

def test_maybe_warn_fps_within_threshold_is_silent():
    """A small deviation (inside the max(abs, rel*expected) threshold) warns nothing."""
    logs = []
    rec = _recorder(fps=30, status_cb=lambda msg, level: logs.append((level, msg)))
    rec._maybe_warn_fps(inst_fps=29.9, now=100.0)  # |Δ|=0.1, threshold=3.0
    assert logs == []
    assert rec._last_fps_warn_ts is None


def test_maybe_warn_fps_beyond_threshold_warns():
    """A large deviation emits a WARNING and records the warn time."""
    logs = []
    rec = _recorder(fps=30, status_cb=lambda msg, level: logs.append((level, msg)))
    rec._maybe_warn_fps(inst_fps=20.0, now=100.0)  # |Δ|=10, threshold=3.0
    warnings = _collect_warnings(logs)
    assert len(warnings) > 0
    assert any("FPS deviation" in msg for msg in warnings)
    assert rec._last_fps_warn_ts == 100.0


def test_maybe_warn_fps_is_throttled():
    """After warning, further deviations stay silent until the warn interval (5 s) has elapsed."""
    logs = []
    rec = _recorder(fps=30, status_cb=lambda msg, level: logs.append((level, msg)))
    rec._maybe_warn_fps(inst_fps=20.0, now=100.0)  # warns
    rec._maybe_warn_fps(inst_fps=20.0, now=102.0)  # within 5 s -> silent
    warnings = _collect_warnings(logs)
    assert len(warnings) == 1
    rec._maybe_warn_fps(inst_fps=20.0, now=106.0)  # >= 5 s later -> warns again
    warnings = _collect_warnings(logs)
    assert len(warnings) == 2


def test_maybe_warn_fps_noop_when_expected_unknown():
    """With no basis for an expected rate (expected <= 0), no warning is emitted."""
    logs = []
    rec = _recorder(fps=0, status_cb=lambda msg, level: logs.append((level, msg)))
    rec.writer_fps = None
    rec._reported_fps = None
    rec._maybe_warn_fps(inst_fps=1000.0, now=100.0)
    assert logs == []


# ---------------------------------------------------------------------------
# _maybe_emit_preview
# ---------------------------------------------------------------------------

def test_maybe_emit_preview_emits_when_due():
    """When no next-emit time is set yet, the frame is forwarded (copied) and the
    next-emit time is scheduled."""
    emitted = []
    rec = _recorder(preview_cb=lambda f: emitted.append(f), preview_fps=10.0)
    rec._next_preview_ts = None
    rec._maybe_emit_preview(_frame())
    assert len(emitted) == 1
    assert rec._next_preview_ts is not None


def test_maybe_emit_preview_skips_before_interval():
    """A frame arriving before the next scheduled emit time is dropped."""
    emitted = []
    rec = _recorder(preview_cb=lambda f: emitted.append(f), preview_fps=10.0)
    rec._next_preview_ts = time.monotonic() + 100.0  # far in the future
    rec._maybe_emit_preview(_frame())
    assert emitted == []


def test_maybe_emit_preview_noop_without_callback():
    """With no preview callback, _maybe_emit_preview does nothing and doesn't raise."""
    rec = _recorder(preview_cb=None, preview_fps=10.0)
    rec._maybe_emit_preview(_frame())  # must not raise
