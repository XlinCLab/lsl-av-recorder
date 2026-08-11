"""Tests for recorder.video.ffmpeg_utils.

These parse AVFoundation probe output (macOS) into supported modes / pixel formats
and build the ffmpeg argument lists used for probing.
"""
from __future__ import annotations

from recorder.video import ffmpeg_utils
from recorder.video.ffmpeg_utils import (_build_mode_args,
                                         _parse_supported_modes,
                                         _pixel_format_probe_succeeded,
                                         _probe_mac_supported_fps,
                                         _probe_mac_supported_ui_formats)

SUPPORTED_MODES_TEXT = """
[avfoundation @ 0x1] Supported modes:
[avfoundation @ 0x1]   1280x720@[1.000000 30.000000]fps
[avfoundation @ 0x1]   1920x1080@[15.000000 30.000000 60.000000]fps
"""


# ---------------------------------------------------------------------------
# _parse_supported_modes
# ---------------------------------------------------------------------------

def test_parse_supported_modes_extracts_resolution_and_fps():
    """Each 'WxH@[fps...]fps' line becomes a (width, height, [fps]) tuple with
    the frame rates parsed and rounded to ints."""
    modes = _parse_supported_modes(text=SUPPORTED_MODES_TEXT)
    assert modes == [
        (1280, 720, [1, 30]),
        (1920, 1080, [15, 30, 60]),
    ]


def test_parse_supported_modes_ignores_non_matching_lines():
    """Lines that don't match the mode pattern yield no entries."""
    assert _parse_supported_modes(text="no modes here\nrandom text") == []


def test_parse_supported_modes_rounds_and_dedups_fps():
    """Frame rates are rounded to the nearest int and de-duplicated, so 29.97
    and 30.0 collapse to a single 30."""
    text = "[x]   640x480@[29.970000 30.000000 30.000000]fps"
    modes = _parse_supported_modes(text=text)
    assert modes == [(640, 480, [30])]


# ---------------------------------------------------------------------------
# _probe_mac_supported_fps
# ---------------------------------------------------------------------------

def test_probe_mac_supported_fps_unions_all_modes():
    """The supported-fps list is the sorted union of every mode's frame rates."""
    modes = _parse_supported_modes(text=SUPPORTED_MODES_TEXT)
    assert _probe_mac_supported_fps(modes=modes) == [1, 15, 30, 60]


def test_probe_mac_supported_fps_empty_when_no_modes():
    """No modes means no frame rates to report."""
    assert _probe_mac_supported_fps(modes=[]) == []


# ---------------------------------------------------------------------------
# _build_mode_args
# ---------------------------------------------------------------------------

def test_build_mode_args_with_size():
    """When width and height are given, both -video_size and -framerate are emitted."""
    assert _build_mode_args(width=1280, height=720, fps=30) == [
        "-video_size", "1280x720", "-framerate", "30",
    ]


def test_build_mode_args_without_size():
    """With no resolution, only -framerate is emitted (no -video_size)."""
    assert _build_mode_args(width=None, height=None, fps=30) == ["-framerate", "30"]


# ---------------------------------------------------------------------------
# _pixel_format_probe_succeeded
# ---------------------------------------------------------------------------

def test_pixel_format_probe_succeeded_true_on_clean_success():
    """A zero-exit probe with no override/unsupported warning counts as success."""
    assert _pixel_format_probe_succeeded(text="stream mapping ok", ok=True) is True


def test_pixel_format_probe_succeeded_false_when_command_failed():
    """A non-zero exit (ok=False) is a failure regardless of output text."""
    assert _pixel_format_probe_succeeded(text="stream mapping ok", ok=False) is False


def test_pixel_format_probe_succeeded_false_on_override_warning():
    """ffmpeg silently overriding the requested format means it wasn't honored,
    so the probe is treated as a failure even on a zero exit."""
    assert _pixel_format_probe_succeeded(
        text="Overriding selected pixel format", ok=True,
    ) is False


def test_pixel_format_probe_succeeded_false_on_not_supported():
    """An explicit 'pixel format ... not supported' message is a failure."""
    assert _pixel_format_probe_succeeded(
        text="Selected pixel format (yuyv422) is not supported", ok=True,
    ) is False


# ---------------------------------------------------------------------------
# _probe_mac_supported_ui_formats
# ---------------------------------------------------------------------------

def test_probe_mac_supported_ui_formats_returns_only_working_formats(monkeypatch):
    """Only UI pixel formats whose probe succeeds are returned; formats that
    report 'not supported' for every mode are dropped."""
    modes = [(1280, 720, [30])]

    def fake_probe(device, extra_args):
        # Only YUYV (yuyv422) probes cleanly; everything else "not supported"
        if "yuyv422" in extra_args:
            return True, "ok"
        return True, "pixel format not supported"

    monkeypatch.setattr(ffmpeg_utils, "_ffmpeg_avfoundation_probe", fake_probe)
    result = _probe_mac_supported_ui_formats(device="0", modes=modes)
    assert result == ["YUYV"]


def test_probe_mac_supported_ui_formats_reports_progress(monkeypatch):
    """The progress callback is invoked and never reports a 'done' count that exceeds the declared total."""
    modes = [(1280, 720, [30])]
    monkeypatch.setattr(
        ffmpeg_utils,
        "_ffmpeg_avfoundation_probe",
        lambda device, extra_args: (True, "ok"),
    )
    seen = []
    _probe_mac_supported_ui_formats(
        device="0",
        modes=modes,
        progress_cb=lambda done, total, fmt: seen.append((done, total, fmt)),
    )
    assert len(seen) > 0
    assert all(done <= total for done, total, _ in seen)
