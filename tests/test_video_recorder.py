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


def _recorder(*,
              fps=30,
              preview_cb=None,
              preview_fps=None,
              status_cb=None,
              divergence_cb=None
              ):
    """Build a VideoRecorder for a single camera without opening hardware."""
    return VideoRecorder(
        cam_cfg=VideoCamConfig(Enabled=True, Label="Cam", FPS=fps),
        output_path="unused.mp4",
        status_cb=status_cb,
        preview_cb=preview_cb,
        preview_fps=preview_fps,
        divergence_cb=divergence_cb,
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


def test_expected_fps_prefers_accepted_override_over_everything():
    """Once the user accepts an observed rate via the divergence
    prompt, that becomes the new baseline -- it takes priority even over
    cam.FPS, since the operator has explicitly overridden their original
    configured intent for this run."""
    rec = _recorder(fps=30)
    rec._accepted_fps_override = 24.0
    rec._reported_fps = 60.0
    rec.writer_fps = 60.0
    assert rec._expected_fps() == 24.0


# ---------------------------------------------------------------------------
# _maybe_warn_fps / divergence_cb (the loud-failure popup hook)
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


def test_maybe_warn_fps_does_not_call_divergence_cb_without_one():
    """No divergence_cb configured: warning still logs, but nothing
    crashes trying to invoke a callback that isn't there."""
    rec = _recorder(fps=30, status_cb=lambda msg, level: None)
    rec._maybe_warn_fps(inst_fps=20.0, now=100.0)  # must not raise error


def test_maybe_warn_fps_prompts_divergence_cb_on_warn():
    """A genuine fps deviation that invokes divergence_cb
    with a human-readable title/message naming the camera
    and both expected and observed frame rates."""
    calls = []
    rec = _recorder(
        fps=30,
        status_cb=lambda msg, level: None,
        divergence_cb=lambda title, msg: calls.append((title, msg)) or True,
    )
    rec._maybe_warn_fps(inst_fps=20.0, now=100.0)
    assert len(calls) == 1
    title, message = calls[0]
    assert "Cam" in title
    assert "30.00" in message and "20.00" in message


def test_maybe_warn_fps_skips_divergence_cb_within_threshold():
    """No deviation -> no log warning -> divergence_cb is never consulted."""
    calls = []
    rec = _recorder(
        fps=30,
        status_cb=lambda msg, level: None,
        divergence_cb=lambda title, msg: calls.append(1) or True,
    )
    rec._maybe_warn_fps(inst_fps=29.9, now=100.0)
    assert calls == []


def test_maybe_warn_fps_accept_updates_expected_fps_baseline():
    """Accepting the divergence (divergence_cb returns True) makes the
    observed rate the new expected baseline going forward."""
    rec = _recorder(
        fps=30,
        status_cb=lambda msg, level: None,
        divergence_cb=lambda title, msg: True,
    )
    rec._maybe_warn_fps(inst_fps=20.0, now=100.0)
    assert rec._accepted_fps_override == 20.0
    assert rec._expected_fps() == 20.0
    # A further reading matching the newly-accepted baseline no longer warns.
    calls = []
    rec.divergence_cb = lambda title, msg: calls.append(1) or True
    rec._maybe_warn_fps(inst_fps=20.1, now=106.0)
    assert calls == []


def test_maybe_warn_fps_reject_does_not_update_baseline():
    """Rejecting (divergence_cb returns False; the caller has already
    triggered an abort itself) leaves _expected_fps() unchanged, so the
    original configured value is still what's being compared against for
    whatever brief remainder of the loop runs before it stops."""
    rec = _recorder(
        fps=30,
        status_cb=lambda msg, level: None,
        divergence_cb=lambda title, msg: False,
    )
    rec._maybe_warn_fps(inst_fps=20.0, now=100.0)
    assert rec._accepted_fps_override is None
    assert rec._expected_fps() == 30.0


def test_maybe_warn_fps_reject_sets_abort_flag_and_suppresses_further_prompts():
    """Once the operator has rejected (aborted) via the divergence prompt,
    no further prompt is shown for this recorder -- e.g. the capture rate
    dropping toward zero as the camera is released during teardown must not
    pop up a second, redundant abort dialog. The warning log line itself
    still fires each time (useful for diagnosing the teardown), only the
    popup is suppressed."""
    calls = []
    rec = _recorder(
        fps=30,
        status_cb=lambda msg, level: None,
        divergence_cb=lambda title, msg: calls.append(1) or False,
    )
    rec._maybe_warn_fps(inst_fps=20.0, now=100.0)
    assert len(calls) == 1
    assert rec._abort_requested is True

    # A later deviation reading (e.g. fps collapsing toward zero as the
    # camera is released during abort teardown) must not prompt again.
    rec._maybe_warn_fps(inst_fps=0.2, now=110.0)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# _maybe_warn_fps grace period after VideoWriter opens
# ---------------------------------------------------------------------------

def test_maybe_warn_fps_silent_during_grace_period_after_writer_opens():
    """Real capture rate can genuinely dip for a few seconds right after the
    VideoWriter opens (e.g. two cameras starting simultaneously contending
    for CPU/USB), then recover -- a large deviation within the grace period
    must not warn at all, not even once."""
    logs = []
    rec = _recorder(fps=60, status_cb=lambda msg, level: logs.append((level, msg)))
    rec._writer_opened_at = 100.0
    rec._maybe_warn_fps(inst_fps=10.0, now=105.0)  # 5s after open, big deviation
    assert logs == []
    assert rec._last_fps_warn_ts is None


def test_maybe_warn_fps_warns_once_grace_period_has_elapsed():
    """The same deviation, once the grace period has passed, warns normally."""
    logs = []
    rec = _recorder(fps=60, status_cb=lambda msg, level: logs.append((level, msg)))
    rec._writer_opened_at = 100.0
    rec._maybe_warn_fps(inst_fps=10.0, now=111.0)  # 11s after open, past the 10s grace period
    warnings = _collect_warnings(logs)
    assert len(warnings) == 1


def test_maybe_warn_fps_grace_period_does_not_apply_before_writer_opens():
    """With no VideoWriter open yet (_writer_opened_at is still None, e.g.
    during fps probing), the grace period does not gate anything -- this
    only guards the post-open settling window."""
    logs = []
    rec = _recorder(fps=60, status_cb=lambda msg, level: logs.append((level, msg)))
    assert rec._writer_opened_at is None
    rec._maybe_warn_fps(inst_fps=10.0, now=100.0)
    warnings = _collect_warnings(logs)
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# _prompt_size_divergence
# ---------------------------------------------------------------------------

def test_prompt_size_divergence_calls_cb_with_camera_and_sizes():
    calls = []
    rec = _recorder(
        fps=30,
        status_cb=lambda msg, level: None,
        divergence_cb=lambda title, msg: calls.append((title, msg)) or True,
    )
    rec._prompt_size_divergence(actual_w=640, actual_h=480)
    assert len(calls) == 1
    title, message = calls[0]
    assert "Cam" in title
    assert "640x480" in message


def test_prompt_size_divergence_only_fires_once_per_recording():
    """The frame-size check only ever runs once (at writer-open time)."""
    calls = []
    rec = _recorder(
        fps=30,
        status_cb=lambda msg, level: None,
        divergence_cb=lambda title, msg: calls.append(1) or True,
    )
    rec._prompt_size_divergence(actual_w=640, actual_h=480)
    rec._prompt_size_divergence(actual_w=640, actual_h=480)
    assert len(calls) == 1


def test_prompt_size_divergence_accept_does_not_set_abort_flag():
    rec = _recorder(
        fps=30,
        status_cb=lambda msg, level: None,
        divergence_cb=lambda title, msg: True,
    )
    rec._prompt_size_divergence(actual_w=640, actual_h=480)
    assert rec._abort_requested is False


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
