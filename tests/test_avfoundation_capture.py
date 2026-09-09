"""Tests for recorder.video.avfoundation_capture."""
from __future__ import annotations

from recorder.video.avfoundation_capture import (_build_modes_by_format,
                                                 _decode_fourcc,
                                                 _expand_frame_rate_range,
                                                 _resolve_device_position,
                                                 modes_match,
                                                 pixel_format_label)

# ---------------------------------------------------------------------------
# _decode_fourcc / pixel_format_label
# ---------------------------------------------------------------------------

def _fourcc_int(code: str) -> int:
    value = 0
    for c in code:
        value = (value << 8) | ord(c)
    return value


def test_decode_fourcc_roundtrips_known_codes():
    assert _decode_fourcc(_fourcc_int("yuvs")) == "yuvs"
    assert _decode_fourcc(_fourcc_int("420v")) == "420v"


def test_pixel_format_label_maps_known_apple_fourccs():
    assert pixel_format_label(_fourcc_int("yuvs")) == "YUYV"
    assert pixel_format_label(_fourcc_int("420v")) == "NV12"
    assert pixel_format_label(_fourcc_int("2vuy")) == "UYVY"
    assert pixel_format_label(_fourcc_int("BGRA")) == "BGRA"


def test_pixel_format_label_falls_back_to_raw_code_for_unknown_fourcc():
    """An unrecognized FourCC is surfaced as its raw uppercased code rather
    than being silently dropped."""
    assert pixel_format_label(_fourcc_int("zzzz")) == "ZZZZ"


# ---------------------------------------------------------------------------
# _expand_frame_rate_range
# ---------------------------------------------------------------------------

def test_expand_frame_rate_range_discrete_returns_single_value():
    """A range where min == max is one discrete rate."""
    assert _expand_frame_rate_range(30.0, 30.0) == [30]


def test_expand_frame_rate_range_continuous_intersects_common_fps_values():
    """A continuous range (15-30) is reduced to the human-recognizable
    common fps values within it, not every raw float."""
    assert _expand_frame_rate_range(15.0, 30.0) == [15, 20, 24, 25, 30]


def test_expand_frame_rate_range_continuous_with_no_common_values_falls_back_to_max():
    """A narrow continuous range with no COMMON_FPS_VALUES entry inside it
    still yields at least the lower and upper bound values instead of nothing."""
    assert _expand_frame_rate_range(27.0, 29.0) == [27, 29]


def test_expand_frame_rate_range_handles_reversed_bounds():
    assert _expand_frame_rate_range(30.0, 15.0) == [15, 20, 24, 25, 30]


# ---------------------------------------------------------------------------
# _build_modes_by_format
# ---------------------------------------------------------------------------

def test_build_modes_by_format_groups_discrete_formats_by_resolution():
    """A BRIO-style device reports one AVCaptureDeviceFormat per discrete
    fps; these must be merged into a single fps list per (format, resolution)."""
    entries = [
        (_fourcc_int("yuvs"), 1280, 720, [(30.0, 30.0)]),
        (_fourcc_int("yuvs"), 1280, 720, [(24.0, 24.0)]),
        (_fourcc_int("yuvs"), 1280, 720, [(60.0, 60.0)]),
    ]
    result = _build_modes_by_format(entries)
    assert result == {"YUYV": [{"width": 1280, "height": 720, "fps": [24, 30, 60]}]}


def test_build_modes_by_format_handles_continuous_range_format():
    """A FaceTime-style device reports one format with a genuine range."""
    entries = [(_fourcc_int("yuvs"), 640, 480, [(15.0, 30.0)])]
    result = _build_modes_by_format(entries)
    assert result == {"YUYV": [{"width": 640, "height": 480, "fps": [15, 20, 24, 25, 30]}]}


def test_build_modes_by_format_skips_zero_dimensions():
    entries = [(_fourcc_int("yuvs"), 0, 0, [(30.0, 30.0)])]
    assert _build_modes_by_format(entries) == {}


def test_build_modes_by_format_separates_formats_and_sorts_resolutions():
    entries = [
        (_fourcc_int("420v"), 1920, 1080, [(30.0, 30.0)]),
        (_fourcc_int("yuvs"), 640, 480, [(30.0, 30.0)]),
        (_fourcc_int("420v"), 640, 480, [(30.0, 30.0)]),
    ]
    result = _build_modes_by_format(entries)
    assert result == {
        "NV12": [
            {"width": 640, "height": 480, "fps": [30]},
            {"width": 1920, "height": 1080, "fps": [30]},
        ],
        "YUYV": [{"width": 640, "height": 480, "fps": [30]}],
    }


# ---------------------------------------------------------------------------
# _resolve_device_position
# ---------------------------------------------------------------------------

def test_resolve_device_position_prefers_name_match():
    names = ["FaceTime HD Camera", "Logitech BRIO"]
    assert _resolve_device_position(names, "Logitech BRIO", fallback_index=0) == 1


def test_resolve_device_position_falls_back_to_index_without_name_match():
    names = ["FaceTime HD Camera", "Logitech BRIO"]
    assert _resolve_device_position(names, None, fallback_index=1) == 1
    assert _resolve_device_position(names, "Unknown Camera", fallback_index=0) == 0


def test_resolve_device_position_none_when_nothing_matches():
    names = ["FaceTime HD Camera"]
    assert _resolve_device_position(names, "Unknown", fallback_index=None) is None
    assert _resolve_device_position(names, None, fallback_index=5) is None


# ---------------------------------------------------------------------------
# modes_match
# ---------------------------------------------------------------------------

MODES_BY_FORMAT = {
    "YUYV": [{"width": 1280, "height": 720, "fps": [24, 30, 60]}],
    "NV12": [{"width": 1280, "height": 720, "fps": [30]}],
}


def test_modes_match_true_for_confirmed_combination():
    assert modes_match(MODES_BY_FORMAT, 1280, 720, 60, pixel_format="YUYV") is True


def test_modes_match_false_for_unconfirmed_fps():
    """This is the exact bug class this module fixes: a BRIO 'verified' at
    60fps by the old ffmpeg probe that actually only supports 30fps for NV12."""
    assert modes_match(MODES_BY_FORMAT, 1280, 720, 60, pixel_format="NV12") is False


def test_modes_match_searches_across_formats_when_none_given():
    assert modes_match(MODES_BY_FORMAT, 1280, 720, 24, pixel_format=None) is True


def test_modes_match_false_when_no_capabilities_known():
    """Fails closed: with nothing to check against, nothing is confirmed."""
    assert modes_match({}, 1280, 720, 30) is False
