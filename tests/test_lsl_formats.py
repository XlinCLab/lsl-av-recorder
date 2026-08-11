"""Tests for lsl_format_to_xdf (recorder.lsl.lsl_inlet_recorder).

Maps a pylsl channel-format enum to the (xdf format string, numpy dtype) pair
used to decode inbound LSL samples. A wrong mapping would silently misinterpret
every sample of an external stream, so the whole enum is pinned down here,
including the two rejection paths.
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
    assert lsl_format_to_xdf(fmt) == expected


def test_string_format_is_rejected():
    # String streams can't be written to the numeric XDF sample path.
    with pytest.raises(ValueError, match="string"):
        lsl_format_to_xdf(cf_string)


def test_unknown_format_is_rejected():
    with pytest.raises(ValueError, match="Unsupported"):
        lsl_format_to_xdf(999)
