"""Tests for the pure logic in recorder.video.dshow_capture (Windows).

Most of dshow_capture drives DirectShow through COM (pygrabber/comtypes) and can
only run on real Windows hardware, so it cannot be unit-tested on other OS.
However, a substantial amount of pure logic sits between those COM calls,
which is importable and testable on any host, and that is what this module covers.
"""
from __future__ import annotations

from recorder.video.constants import DEFAULT_BRIGHTNESS
from recorder.video.dshow_capture import (_build_capabilities_from_formats,
                                          _build_device_list,
                                          _find_format_index,
                                          _normalize_formats, _sane_default,
                                          _snap_to_common_fps,
                                          summarize_formats)


def _format(width, height, media_type_str, min_framerate, max_framerate, index=0):
    """Build a pygrabber-style stream-caps dict for the format helpers.

    Named to mirror pygrabber's fields, including its quirk that `min_framerate`
    can hold the *higher* rate and `max_framerate` the *lower* one (see
    _normalize_formats).
    """
    return {
        "width": width,
        "height": height,
        "media_type_str": media_type_str,
        "min_framerate": min_framerate,
        "max_framerate": max_framerate,
        "index": index,
    }


# ---------------------------------------------------------------------------
# _build_device_list
# ---------------------------------------------------------------------------

def test_build_device_list_indexes_names():
    """Device names become {index, name, devnode} dicts, with the list position
    as both index and (stringified) devnode."""
    assert _build_device_list(names=["Cam A", "Cam B"]) == [
        {"index": 0, "name": "Cam A", "devnode": "0"},
        {"index": 1, "name": "Cam B", "devnode": "1"},
    ]


def test_build_device_list_empty():
    """No device names yields an empty list."""
    assert _build_device_list(names=[]) == []


# ---------------------------------------------------------------------------
# _normalize_formats
# ---------------------------------------------------------------------------

def test_normalize_formats_unswaps_reversed_framerates():
    """pygrabber can report min_framerate > max_framerate (it divides by frame
    interval, inverting the relationship); normalization restores low <= high."""
    formats = [
        _format(
            width=1280,
            height=720,
            media_type_str="YUY2",
            min_framerate=60.0,
            max_framerate=30.0
        )
    ]
    normalized = _normalize_formats(formats=formats)
    assert normalized[0]["min_framerate"] == 30.0
    assert normalized[0]["max_framerate"] == 60.0


def test_normalize_formats_leaves_ordered_framerates():
    """Already-ordered frame rates pass through unchanged."""
    formats = [
        _format(
            width=640,
            height=480,
            media_type_str="MJPG",
            min_framerate=15.0,
            max_framerate=30.0,
        )
    ]
    normalized = _normalize_formats(formats=formats)
    assert normalized[0]["min_framerate"] == 15.0
    assert normalized[0]["max_framerate"] == 30.0


# ---------------------------------------------------------------------------
# _build_capabilities_from_formats
# ---------------------------------------------------------------------------

def test_build_capabilities_aggregates_modes_and_formats():
    """The capabilities dict aggregates pixel formats, the fps union, per-mode
    fps lists, and a per-format mode breakdown."""
    formats = _normalize_formats(
        formats=[
            _format(
                width=1280,
                height=720,
                media_type_str="YUY2",
                min_framerate=30.0,
                max_framerate=60.0,
            ),
            _format(
                width=640,
                height=480,
                media_type_str="MJPG",
                min_framerate=30.0,
                max_framerate=30.0,
            ),
    ])
    caps = _build_capabilities_from_formats(formats=formats)
    assert caps["pixel_formats"] == ["MJPG", "YUY2"]
    assert caps["fps"] == [30, 60]
    assert caps["modes"] == [
        {"width": 640, "height": 480, "fps": [30]},
        {"width": 1280, "height": 720, "fps": [30, 60]},
    ]
    assert set(caps["modes_by_format"].keys()) == {"YUY2", "MJPG"}


def test_build_capabilities_skips_nonpositive_dimensions():
    """Formats with a non-positive width/height are ignored (junk entries some drivers report)."""
    formats = _normalize_formats(
        formats=[
            _format(
                width=0,
                height=0,
                media_type_str="YUY2",
                min_framerate=30.0,
                max_framerate=30.0,
            ),
            _format(
                width=640,
                height=480,
                media_type_str="YUY2",
                min_framerate=30.0,
                max_framerate=30.0,
            ),
    ])
    caps = _build_capabilities_from_formats(formats=formats)
    assert caps["modes"] == [{"width": 640, "height": 480, "fps": [30]}]


# ---------------------------------------------------------------------------
# _snap_to_common_fps
# ---------------------------------------------------------------------------

def test_snap_to_common_fps_picks_nearest_common_value():
    """A noisy measured fps snaps to the nearest valid fps value."""
    assert _snap_to_common_fps(value=29.4) == 30
    assert _snap_to_common_fps(value=58.0) == 60
    assert _snap_to_common_fps(value=23.7) == 24


# ---------------------------------------------------------------------------
# _sane_default
# ---------------------------------------------------------------------------

def test_sane_default_replaces_default_sitting_at_range_floor():
    """A driver default that sits exactly at the range floor is treated as
    bogus and replaced by the app's own default, clamped into range."""
    brightness_result = _sane_default(
        rng=(0, 255),
        default=0,
        app_default=DEFAULT_BRIGHTNESS,
    )
    assert brightness_result == DEFAULT_BRIGHTNESS


def test_sane_default_keeps_genuine_default():
    """A default that is not at the floor is trusted and returned unchanged."""
    brightness_result = _sane_default(
        rng=(0, 255),
        default=100,
        app_default=DEFAULT_BRIGHTNESS,
    )
    assert brightness_result == 100


def test_sane_default_passes_through_when_no_range():
    """With no range (or no default) to judge against, the default is returned as-is."""
    brightness_result = _sane_default(
        rng=None,
        default=5,
        app_default=DEFAULT_BRIGHTNESS,
    )
    assert brightness_result == 5


# ---------------------------------------------------------------------------
# summarize_formats
# ---------------------------------------------------------------------------

def test_summarize_formats_lists_unique_combos():
    """The summary lists the unique 'PIXFMT WxH' combinations, sorted."""
    formats = [
        _format(
            width=1280,
            height=720,
            media_type_str="YUY2",
            min_framerate=30.0,
            max_framerate=60.0
        ),
        _format(
            width=640,
            height=480,
            media_type_str="MJPG",
            min_framerate=30.0,
            max_framerate=30.0,
        ),
    ]
    assert summarize_formats(formats=formats) == "MJPG 640x480, YUY2 1280x720"


def test_summarize_formats_empty():
    """No formats produces a human-readable 'none found' note (device busy/unreachable)."""
    assert summarize_formats(formats=[]) == "none found (device may be busy/unreachable)"


def test_summarize_formats_truncates_with_more_suffix():
    """Beyond the limit, remaining combinations are elided with a '... (N more)' suffix."""
    formats = [
        _format(
            width=w,
            height=480,
            media_type_str="YUY2",
            min_framerate=30.0,
            max_framerate=30.0,
        )
        for w in range(100, 130)
    ]
    summary = summarize_formats(formats=formats, limit=5)
    assert summary == "YUY2 100x480, YUY2 101x480, YUY2 102x480, YUY2 103x480, YUY2 104x480, ... (25 more)"


# ---------------------------------------------------------------------------
# _find_format_index
# ---------------------------------------------------------------------------

def test_find_format_index_matches_resolution_and_pixel_format():
    """A matching (resolution, pixel format) combination returns its index,
    matched case-insensitively on the pixel format."""
    formats = _normalize_formats(
        formats=[
            _format(
                width=1280,
                height=720,
                media_type_str="YUY2",
                min_framerate=30.0,
                max_framerate=60.0,
                index=0,
            ),
            _format(
                width=640,
                height=480,
                media_type_str="MJPG",
                min_framerate=30.0,
                max_framerate=30.0,
                index=1,
            ),
        ]
    )
    result0 = _find_format_index(
        formats=formats,
        width=1280,
        height=720,
        pixel_format="yuy2"
    )
    result1 = _find_format_index(
        formats=formats,
        width=640,
        height=480,
        pixel_format="mjpg"
    )
    assert result0 == 0
    assert result1 == 1


def test_find_format_index_respects_fps_range():
    """When a fps is given, a candidate whose [min, max] range covers it is preferred."""
    formats = _normalize_formats(
        formats=[
            _format(
                width=1280,
                height=720,
                media_type_str="YUY2",
                min_framerate=30.0,
                max_framerate=60.0,
                index=0,
            ),
            _format(
                width=1280,
                height=720,
                media_type_str="MJPG",
                min_framerate=5.0,
                max_framerate=15.0,
                index=1,
            ),
        ]
    )
    result = _find_format_index(
        formats=formats,
        width=1280,
        height=720,
        pixel_format="YUY2",
        fps=45,
    )
    assert result == 0


def test_find_format_index_returns_none_when_unmatched():
    """No matching resolution/format returns None."""
    formats = _normalize_formats(
        formats=[
            _format(
                width=640,
                height=480,
                media_type_str="YUY2",
                min_framerate=30.0,
                max_framerate=30.0,
                index=0,
            ),
        ]
    )
    result = _find_format_index(
        formats=formats,
        width=1920,
        height=1080,
        pixel_format="YUY2",
    )
    assert result is None
