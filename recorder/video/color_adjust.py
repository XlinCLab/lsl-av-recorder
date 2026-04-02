from __future__ import annotations

import cv2
import numpy as np

from .constants import DEFAULT_BRIGHTNESS, DEFAULT_HUE, DEFAULT_SATURATION


def apply_color_adjustments(
    frame_bgr: np.ndarray,
    brightness: int | None,
    hue: int | None,
    saturation: int | None,
) -> np.ndarray:
    """Apply lightweight software color adjustments to a BGR frame."""
    if frame_bgr is None:
        return frame_bgr

    bval = DEFAULT_BRIGHTNESS if brightness is None else int(brightness)
    hval = DEFAULT_HUE if hue is None else int(hue)
    sval = DEFAULT_SATURATION if saturation is None else int(saturation)

    if (
        bval == DEFAULT_BRIGHTNESS
        and hval == DEFAULT_HUE
        and sval == DEFAULT_SATURATION
    ):
        return frame_bgr

    out = frame_bgr

    if bval != DEFAULT_BRIGHTNESS:
        if DEFAULT_BRIGHTNESS == 0:
            return out
        offset = (float(bval) - float(DEFAULT_BRIGHTNESS)) / float(DEFAULT_BRIGHTNESS)
        beta = offset * 255.0
        out = np.clip(out.astype(np.float32) + beta, 0.0, 255.0).astype(np.uint8)

    if hval != DEFAULT_HUE or sval != DEFAULT_SATURATION:
        hsv = cv2.cvtColor(out, cv2.COLOR_BGR2HSV)
        h_chan, s_chan, v_chan = cv2.split(hsv)
        if hval != DEFAULT_HUE:
            shift = int(round(float(hval) / 2.0))
            h_chan = (h_chan.astype(np.int16) + shift) % 180
            h_chan = h_chan.astype(np.uint8)
        if sval != DEFAULT_SATURATION:
            factor = float(sval) / float(DEFAULT_SATURATION)
            s_chan = np.clip(s_chan.astype(np.float32) * factor, 0.0, 255.0).astype(np.uint8)
        hsv = cv2.merge([h_chan, s_chan, v_chan])
        out = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)

    return out
