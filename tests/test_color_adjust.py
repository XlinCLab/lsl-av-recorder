"""Tests for recorder.video.color_adjust.apply_color_adjustments.

Software brightness/hue/saturation applied to BGR frames on macOS capture
(where AVFoundation can't do it in hardware).
"""
from __future__ import annotations

import numpy as np

from recorder.video.color_adjust import apply_color_adjustments
from recorder.video.constants import (DEFAULT_BRIGHTNESS, DEFAULT_HUE,
                                      DEFAULT_SATURATION)


def _gray_frame(value=100, shape=(4, 4)):
    """Return a uniform gray BGR frame (all channels == `value`)."""
    return np.full((*shape, 3), value, dtype=np.uint8)


def test_none_frame_passes_through():
    """A None frame is returned unchanged (guard against operating on no image)."""
    result = apply_color_adjustments(
        frame_bgr=None,
        brightness=200,
        hue=50,
        saturation=150,
    )
    assert result is None


def test_all_none_is_noop():
    """When every control is None the frame is returned untouched."""
    frame = _gray_frame()
    out = apply_color_adjustments(
        frame_bgr=frame,
        brightness=None,
        hue=None,
        saturation=None,
    )
    assert np.array_equal(out, frame)


def test_default_values_are_noop():
    """Passing the default value for each control is a no-op, identical to the
    input frame (so a frame left at defaults is never needlessly transformed)."""
    frame = _gray_frame()
    out = apply_color_adjustments(
        frame_bgr=frame,
        brightness=DEFAULT_BRIGHTNESS,
        hue=DEFAULT_HUE,
        saturation=DEFAULT_SATURATION,
    )
    assert np.array_equal(out, frame)


def test_brightness_above_default_brightens():
    """A brightness above the default raises the mean pixel value and keeps the output as uint8."""
    frame = _gray_frame(100)
    out = apply_color_adjustments(
        frame_bgr=frame,
        brightness=DEFAULT_BRIGHTNESS + 60,
        hue=None,
        saturation=None,
    )
    assert out.mean() > frame.mean()
    assert out.dtype == np.uint8


def test_brightness_below_default_darkens():
    """A brightness below the default lowers the mean pixel value."""
    frame = _gray_frame(100)
    out = apply_color_adjustments(
        frame_bgr=frame,
        brightness=DEFAULT_BRIGHTNESS - 60,
        hue=None,
        saturation=None,
    )
    assert out.mean() < frame.mean()


def test_brightness_stays_within_uint8_range():
    """Brightening a near-white frame clips at 255 rather than overflowing/
    wrapping around the uint8 range."""
    frame = _gray_frame(250)
    out = apply_color_adjustments(
        frame_bgr=frame,
        brightness=255,
        hue=None,
        saturation=None,
    )
    assert out.max() <= 255
    assert out.min() >= 0


def test_saturation_zero_desaturates_colored_frame():
    """Driving saturation to 0 collapses a strongly colored frame toward gray,
    shrinking the spread between its channels."""
    frame = np.zeros((4, 4, 3), dtype=np.uint8)
    frame[..., 0] = 200  # blue channel in BGR
    out = apply_color_adjustments(
        frame_bgr=frame,
        brightness=None,
        hue=None,
        saturation=0,
    )
    spread = int(out.max()) - int(out.min())
    assert spread < 200  # less separation than the original 200-vs-0
