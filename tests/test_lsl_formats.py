"""Tests for lsl_format_to_xdf (recorder.lsl.lsl_inlet_recorder).

Maps a pylsl channel-format enum to the (xdf format string, numpy dtype) pair
used to decode inbound LSL samples. A wrong mapping would silently misinterpret
every sample of an external stream, so the whole enum is pinned down here,
including cf_string (used by marker/trigger streams, which have no fixed-width dtype)
and the unknown-format rejection.
"""
from __future__ import annotations

import numpy as np
import pytest
from pylsl import (cf_double64, cf_float32, cf_int8, cf_int16, cf_int32,
                   cf_int64, cf_string)

from recorder.lsl.lsl_inlet_recorder import lsl_format_to_xdf


@pytest.mark.parametrize(
    "fmt, expected",
    [
        (cf_float32, ("float32", np.float32)),
        (cf_double64, ("double64", np.float64)),
        (cf_int8, ("int8", np.int8)),
        (cf_int16, ("int16", np.int16)),
        (cf_int32, ("int32", np.int32)),
        (cf_int64, ("int64", np.int64)),
    ],
)
def test_maps_each_numeric_format(fmt, expected):
    """Each supported numeric channel format maps to its (xdf name, numpy dtype) pair."""
    assert lsl_format_to_xdf(fmt=fmt) == expected


def test_maps_string_format():
    """cf_string maps to the "string" xdf format with no numpy dtype (None),
    signalling callers to keep samples as plain Python strings rather than
    casting to a fixed-width array."""
    assert lsl_format_to_xdf(fmt=cf_string) == ("string", None)


def test_unknown_format_is_rejected():
    """An unrecognized format enum raises rather than silently guessing a dtype."""
    with pytest.raises(ValueError, match="Unsupported"):
        lsl_format_to_xdf(fmt=999)
