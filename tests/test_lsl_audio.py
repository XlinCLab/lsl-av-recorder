"""Tests for recorder.audio.lsl_audio helpers.

Covers the pure bitdepth to format mappings and the input-device resolution
logic.
"""
from __future__ import annotations

import pytest

from recorder.audio.lsl_audio import (_dtype_format, _lsl_format,
                                      _resolve_input_device, _try_device)

# ---------------------------------------------------------------------------
# _lsl_format / _dtype_format
# ---------------------------------------------------------------------------

def test_lsl_format_maps_supported_bitdepths():
    assert _lsl_format(16) == "int16"
    assert _lsl_format(32) == "float32"
    assert _lsl_format(64) == "double64"


def test_lsl_format_rejects_unsupported_bitdepth():
    with pytest.raises(ValueError):
        _lsl_format(24)


def test_dtype_format_collapses_to_capture_dtype():
    # Capture happens as int16 or float32; 64-bit is upcast in software later
    assert _dtype_format(16) == "int16"
    assert _dtype_format(32) == "float32"
    assert _dtype_format(64) == "float32"


# ---------------------------------------------------------------------------
# _try_device
# ---------------------------------------------------------------------------

def test_try_device_returns_requested_rate_when_supported(fake_sd):
    idx = fake_sd.add_device("Mic", max_input_channels=1, supported_rates=[48000])
    rate, err = _try_device(
        device=idx,
        samplerate=48000,
        channels=1,
        dtype="float32",
    )
    assert (rate, err) == (48000, None)


def test_try_device_falls_back_to_native_rate(fake_sd):
    idx = fake_sd.add_device(
        "Mic",
        max_input_channels=1,
        default_samplerate=44100,
        supported_rates=[44100],
    )
    rate, err = _try_device(idx, 48000, 1, "float32")
    # Requested 48000 is unsupported; the device's native 44100 is used instead.
    assert rate == 44100
    assert err is None


def test_try_device_reports_error_when_unusable(fake_sd):
    idx = fake_sd.add_device("Mic", max_input_channels=1, supported_rates=[])
    rate, err = _try_device(
        device=idx,
        samplerate=48000,
        channels=1,
        dtype="float32",
    )
    assert rate is None
    assert isinstance(err, Exception)


# ---------------------------------------------------------------------------
# _resolve_input_device: explicit device requested
# ---------------------------------------------------------------------------

def test_resolve_requested_device_supported(fake_sd):
    idx = fake_sd.add_device("Mic", max_input_channels=1, supported_rates=[48000])
    resolved = _resolve_input_device(
        requested=idx,
        samplerate=48000,
        channels=1,
        dtype="float32"
    )
    assert resolved == (idx, 48000)


def test_resolve_requested_device_native_fallback(fake_sd):
    idx = fake_sd.add_device(
        "Mic",
        max_input_channels=1,
        default_samplerate=44100,
        supported_rates=[44100],
    )
    resolved = _resolve_input_device(
        requested=idx,
        samplerate=48000,
        channels=1,
        dtype="float32"
    )
    assert resolved == (idx, 44100)


def test_resolve_requested_device_unusable_raises(fake_sd):
    idx = fake_sd.add_device("Mic", max_input_channels=1, supported_rates=[])
    with pytest.raises(RuntimeError, match="not usable"):
        _resolve_input_device(
            requested=idx,
            samplerate=48000,
            channels=1,
            dtype="float32",
        )


# ---------------------------------------------------------------------------
# _resolve_input_device: auto-selection (requested is None)
# ---------------------------------------------------------------------------

def test_resolve_auto_picks_default_device(fake_sd):
    fake_sd.add_device("Speaker", max_input_channels=0)
    mic = fake_sd.add_device("Mic", max_input_channels=1, supported_rates=[48000])
    fake_sd.set_default_input(mic)
    resolved = _resolve_input_device(
        requested=None,
        samplerate=48000,
        channels=1,
        dtype="float32",
    )
    assert resolved == (mic, 48000)


def test_resolve_auto_skips_unusable_default_for_working_device(fake_sd):
    # Default device enumerates but supports nothing usable.
    bad = fake_sd.add_device("BadDefault", max_input_channels=1, supported_rates=[])
    good = fake_sd.add_device("Mic", max_input_channels=1, supported_rates=[48000])
    fake_sd.set_default_input(bad)
    resolved = _resolve_input_device(
        requested=None,
        samplerate=48000,
        channels=1,
        dtype="float32",
    )
    assert resolved == (good, 48000)


def test_resolve_auto_raises_when_no_device_usable(fake_sd):
    fake_sd.add_device("Mic", max_input_channels=1, supported_rates=[])
    fake_sd.set_default_input(-1)  # PortAudio's "no default device" sentinel
    with pytest.raises(RuntimeError, match="No usable audio input device"):
        _resolve_input_device(
            requested=None,
            samplerate=48000,
            channels=1,
            dtype="float32",
        )
