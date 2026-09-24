"""Tests for recorder.audio.lsl_audio helpers.

Covers the pure bitdepth to format mappings and the input-device resolution
logic.
"""
from __future__ import annotations

import numpy as np
import pytest
import sounddevice as sd

from recorder.audio.constants import (BITDEPTH_CONVERSION_FLOAT,
                                      BITDEPTH_CONVERSION_INT)
from recorder.audio.lsl_audio import (AudioLSLStreamer, AudioStreamSettings,
                                      _capture_dtype, _resolve_input_device,
                                      _try_device, convert_samples)


def test_bitdepth_conversion_val():
    assert BITDEPTH_CONVERSION_FLOAT == 32768.0
    assert BITDEPTH_CONVERSION_INT == 32768
    assert BITDEPTH_CONVERSION_INT == int(BITDEPTH_CONVERSION_FLOAT)
    assert BITDEPTH_CONVERSION_FLOAT == float(BITDEPTH_CONVERSION_INT)


# ---------------------------------------------------------------------------
# _capture_dtype
# ---------------------------------------------------------------------------

def test_capture_dtype_maps_supported_bitdepths():
    """16 captures as int16, 32 as float32."""
    assert _capture_dtype(16) == "int16"
    assert _capture_dtype(32) == "float32"


@pytest.mark.parametrize("bad", [8, 24, 64])
def test_capture_dtype_rejects_unsupported_bitdepth(bad):
    """Unsupported depths raise rather than guessing a dtype."""
    with pytest.raises(ValueError):
        _capture_dtype(bad)


# ---------------------------------------------------------------------------
# convert_samples
# ---------------------------------------------------------------------------

def test_convert_samples_same_dtype_is_noop():
    x = np.array([[0.1], [-0.2]], dtype=np.float32)
    assert convert_samples(x, "float32") is x
    y = np.array([[1], [-2]], dtype=np.int16)
    assert convert_samples(y, "int16") is y


def test_convert_samples_int16_to_float32_uses_full_scale_convention():
    """int16 full scale maps to [-1, 1] (divided by 32768) and yields float32."""
    x = np.array([[-32768], [0], [16384], [32767]], dtype=np.int16)
    out = convert_samples(x, "float32")
    assert out.dtype == np.float32
    assert out[:, 0].tolist() == [-1.0, 0.0, 0.5, 32767 / 32768]


def test_convert_samples_float32_to_int16_rounds_and_clips():
    x = np.array([[-1.0], [0.0], [0.5], [1.0], [1.5], [-2.0]], dtype=np.float32)
    out = convert_samples(x, "int16")
    assert out.dtype == np.int16
    assert out[:, 0].tolist() == [-32768, 0, 16384, 32767, 32767, -32768]


def test_convert_samples_int16_float32_round_trip_is_lossless():
    x = np.arange(-32768, 32768, dtype=np.int16)[:, None]
    assert np.array_equal(convert_samples(convert_samples(x, "float32"), "int16"), x)


def test_convert_samples_preserves_shape():
    x = np.zeros((5, 3), dtype=np.int16)
    assert convert_samples(x, "float32").shape == (5, 3)


def test_convert_samples_rejects_unknown_format():
    with pytest.raises(ValueError):
        convert_samples(np.zeros((2, 1), dtype=np.float32), "float64")


# ---------------------------------------------------------------------------
# _try_device
# ---------------------------------------------------------------------------

def test_try_device_returns_requested_rate_when_supported(fake_sd):
    """When the device supports the requested rate, that rate is returned with no error."""
    idx = fake_sd.add_device("Mic", max_input_channels=1, supported_rates=[48000])
    rate, err = _try_device(
        device=idx,
        samplerate=48000,
        channels=1,
        dtype="float32",
    )
    assert (rate, err) == (48000, None)


def test_try_device_falls_back_to_native_rate(fake_sd):
    """When the requested rate is unsupported, the device's native default rate is used instead."""
    default_srate = 44100
    idx = fake_sd.add_device(
        "Mic",
        max_input_channels=1,
        default_samplerate=default_srate,
        supported_rates=[44100],
    )
    rate, err = _try_device(device=idx, samplerate=48000, channels=1, dtype="float32")
    # Requested 48000 is unsupported; the device's native 44100 is used instead
    assert rate == default_srate
    assert err is None


def test_try_device_reports_error_when_unusable(fake_sd):
    """When no rate works, the device is reported as unusable: (None, error)."""
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
    """An explicitly requested, supported device resolves to (device, requested rate)."""
    idx = fake_sd.add_device("Mic", max_input_channels=1, supported_rates=[48000])
    resolved = _resolve_input_device(
        requested=idx,
        samplerate=48000,
        channels=1,
        dtype="float32"
    )
    assert resolved == (idx, 48000)


def test_resolve_requested_device_native_fallback(fake_sd):
    """An explicitly requested device that does not support
    the requested rate resolves to its native rate."""
    default_srate = 44100
    idx = fake_sd.add_device(
        "Mic",
        max_input_channels=1,
        default_samplerate=default_srate,
        supported_rates=[44100],
    )
    resolved = _resolve_input_device(
        requested=idx,
        samplerate=48000,
        channels=1,
        dtype="float32"
    )
    assert resolved == (idx, default_srate)


def test_resolve_requested_device_unusable_raises(fake_sd):
    """An explicitly requested but unusable device raises RuntimeError rather
    than silently falling through to another device."""
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
    """With no device requested, the system default input device is selected."""
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
    """Auto-selection skips a default device that enumerates but can't open
    and falls back on the next working input device."""
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
    """When no input device is usable, auto-selection raises a RuntimeError
    that instructs the user to fix their audio config."""
    fake_sd.add_device("Mic", max_input_channels=1, supported_rates=[])
    fake_sd.set_default_input(-1)  # PortAudio's "no default device" sentinel
    with pytest.raises(RuntimeError, match="No usable audio input device"):
        _resolve_input_device(
            requested=None,
            samplerate=48000,
            channels=1,
            dtype="float32",
        )


# ---------------------------------------------------------------------------
# AudioLSLStreamer.start: capture-format fallback
# ---------------------------------------------------------------------------

class _StubInputStream:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _StubInputStream.instances.append(self)

    def start(self):
        pass


def test_start_falls_back_to_int16_capture_when_float32_is_rejected(fake_sd, monkeypatch):
    """A device that only accepts int16 still records: capture drops to int16
    (bitdepth updated to match), the warning names the format that was refused,
    and the configured sample_format is unchanged."""
    idx = fake_sd.add_device(
        "Mic", max_input_channels=1, default_samplerate=48000,
        supported_rates=[48000], supported_dtypes=["int16"],
    )
    _StubInputStream.instances = []
    monkeypatch.setattr(sd, "InputStream", _StubInputStream, raising=False)
    messages = []
    settings = AudioStreamSettings(device=idx, samplerate=48000, bitdepth=32, sample_format="float32")

    AudioLSLStreamer(settings, sample_cb=None, status_cb=lambda m, lvl: messages.append((lvl, m))).start()

    assert _StubInputStream.instances[0].kwargs["dtype"] == "int16"
    assert settings.bitdepth == 16
    assert settings.sample_format == "float32"
    warning = next(m for lvl, m in messages if lvl == "WARNING")
    assert "float32 capture" in warning and "falling back to int16" in warning


def test_start_uses_requested_capture_dtype_when_supported(fake_sd, monkeypatch):
    idx = fake_sd.add_device(
        name="Mic",
        max_input_channels=1,
        default_samplerate=48000,
        supported_rates=[48000],
        supported_dtypes=["int16", "float32"],
    )
    _StubInputStream.instances = []
    monkeypatch.setattr(sd, "InputStream", _StubInputStream, raising=False)
    settings = AudioStreamSettings(device=idx, samplerate=48000, bitdepth=32)

    AudioLSLStreamer(settings, sample_cb=None).start()

    assert _StubInputStream.instances[0].kwargs["dtype"] == "float32"
    assert settings.bitdepth == 32
