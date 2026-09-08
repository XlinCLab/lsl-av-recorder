"""Tests for recorder.video.ffmpeg_utils.

These parse AVFoundation probe output (macOS) into supported modes / pixel formats
and build the ffmpeg argument lists used for probing.
"""
from __future__ import annotations

from recorder.video import ffmpeg_utils
from recorder.video.ffmpeg_utils import (_build_mode_args,
                                         _parse_supported_modes,
                                         _pixel_format_probe_succeeded,
                                         _probe_mac_modes_by_format,
                                         _probe_mac_supported_fps)

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


DUPLICATE_RESOLUTION_LINES_TEXT = """
[avfoundation @ 0x1] Supported modes:
[avfoundation @ 0x1]   176x144@[30.000030 30.000030]fps
[avfoundation @ 0x1]   176x144@[24.000038 24.000038]fps
[avfoundation @ 0x1]   176x144@[20.000000 20.000000]fps
[avfoundation @ 0x1]   176x144@[15.000015 15.000015]fps
[avfoundation @ 0x1]   160x120@[30.000030 30.000030]fps
[avfoundation @ 0x1]   176x144@[30.000030 30.000030]fps
[avfoundation @ 0x1]   176x144@[24.000038 24.000038]fps
"""


def test_parse_supported_modes_merges_repeated_resolution_lines():
    """Some AVFoundation devices report the same resolution across multiple
    lines, one per discrete rate they support, and/or once per pixel format.
    Test that this format is parsed correctly."""
    modes = _parse_supported_modes(text=DUPLICATE_RESOLUTION_LINES_TEXT)
    assert modes == [
        (176, 144, [15, 20, 24, 30]),
        (160, 120, [30]),
    ]


def test_parse_supported_modes_preserves_first_seen_resolution_order():
    """Resolutions are ordered by when they were first seen, even when a
    later duplicate line for an earlier resolution appears further down."""
    text = """
    100x100@[10.0 10.0]fps
    200x200@[20.0 20.0]fps
    100x100@[15.0 15.0]fps
    """
    modes = _parse_supported_modes(text=text)
    assert [(w, h) for w, h, _ in modes] == [(100, 100), (200, 200)]
    assert modes == [(100, 100, [10, 15]), (200, 200, [20])]


# ---------------------------------------------------------------------------
# _probe_mac_supported_fps
# ---------------------------------------------------------------------------

def test_probe_mac_supported_fps_unions_all_modes():
    """The supported-fps list is the sorted union of every mode's frame rates."""
    modes = _parse_supported_modes(text=SUPPORTED_MODES_TEXT)
    assert _probe_mac_supported_fps(modes=modes) == [1, 15, 30, 60]


def test_probe_mac_supported_fps_matches_per_resolution_merge():
    """The supported-fps list is the sorted union of every mode's frame rates.
    This tests the ffmpeg output with multiple/duplicate lines."""
    modes = _parse_supported_modes(text=DUPLICATE_RESOLUTION_LINES_TEXT)
    assert _probe_mac_supported_fps(modes=modes) == [15, 20, 24, 30]


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
# _probe_mac_modes_by_format
# ---------------------------------------------------------------------------

def test_probe_mac_modes_by_format_only_keeps_confirmed_combinations(monkeypatch):
    """Every (resolution, fps) candidate is tested independently against
    every pixel format: a format that only opens successfully for some of
    a resolution's fps values must not have the others attributed to it."""
    candidate_modes = [(1280, 720, [15, 30])]

    def fake_probe(device, extra_args):
        # yuyv422 opens at both 15 and 30fps
        if "yuyv422" in extra_args:
            return True, "ok"
        # mjpeg opens only at 30fps
        if "mjpeg" in extra_args and "30" in extra_args:
            return True, "ok"
        # everything else fails to open
        return True, "pixel format not supported"

    monkeypatch.setattr(ffmpeg_utils, "_ffmpeg_avfoundation_probe", fake_probe)
    result = _probe_mac_modes_by_format(device="0", candidate_modes=candidate_modes)
    assert result == {"YUYV": [(1280, 720, [15, 30])], "MJPG": [(1280, 720, [30])]}


def test_probe_mac_modes_by_format_does_not_generalize_across_fps(monkeypatch):
    """A format/resolution succeeding at one fps must NOT be assumed to succeed
    at another fps that was never itself confirmed."""
    candidate_modes = [(640, 480, [15, 30, 60])]

    def fake_probe(device, extra_args):
        # yuyv422 only opens at 30fps and 60fps; 15fps always fails for every format
        if "yuyv422" in extra_args and "15" not in extra_args:
            return True, "ok"
        return False, "error"

    monkeypatch.setattr(ffmpeg_utils, "_ffmpeg_avfoundation_probe", fake_probe)
    result = _probe_mac_modes_by_format(device="0", candidate_modes=candidate_modes)
    assert result == {"YUYV": [(640, 480, [30, 60])]}


def test_probe_mac_modes_by_format_no_candidates_falls_back_to_default_fps(monkeypatch):
    """With no candidate modes at all (e.g. AVFoundation's dump was empty),
    a single default-fps probe is still attempted for each pixel format."""
    seen_args = []

    def fake_probe(device, extra_args):
        seen_args.append(extra_args)
        return True, "ok"

    monkeypatch.setattr(ffmpeg_utils, "_ffmpeg_avfoundation_probe", fake_probe)
    result = _probe_mac_modes_by_format(device="0", candidate_modes=[])
    assert all("-video_size" not in args for args in seen_args)
    assert set(result.keys()) == set(ffmpeg_utils.PIXEL_FORMAT_MAP.keys())


def test_probe_mac_modes_by_format_reports_progress(monkeypatch):
    """The progress callback is invoked once per (format, candidate) probe
    and never reports a 'done' count that exceeds the declared total."""
    candidate_modes = [(1280, 720, [30])]
    monkeypatch.setattr(
        ffmpeg_utils,
        "_ffmpeg_avfoundation_probe",
        lambda device, extra_args: (True, "ok"),
    )
    seen = []
    _probe_mac_modes_by_format(
        device="0",
        candidate_modes=candidate_modes,
        progress_cb=lambda done, total, fmt: seen.append((done, total, fmt)),
    )
    assert len(seen) == len(ffmpeg_utils.PIXEL_FORMAT_MAP)
    assert all(done <= total for done, total, _ in seen)
