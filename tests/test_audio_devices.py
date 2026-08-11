"""Tests for recorder.audio.devices.

These functions feed the Audio tab: the list of input devices, the default
input index (with PortAudio's -1 "no default" quirk), and the per-device
capability probe that decides which sample rates / bit depths the GUI offers.
All are driven through the FakeSoundDevice fixture from tests/conftest.py.
"""
from __future__ import annotations

from recorder.audio.constants import CANDIDATE_SAMPLE_RATES
from recorder.audio.devices import (default_input_device_index,
                                    get_audio_device_capabilities,
                                    is_input_config_supported,
                                    list_input_devices)

# ---------------------------------------------------------------------------
# list_input_devices
# ---------------------------------------------------------------------------

def test_list_input_devices_returns_only_input_capable(fake_sd):
    fake_sd.add_device("Speaker", max_input_channels=0, hostapi=0)
    fake_sd.add_device("Mic", max_input_channels=2, hostapi=1)
    fake_sd.add_device("Webcam", max_input_channels=1, hostapi=1)

    devices = list_input_devices()
    assert [d["name"] for d in devices] == ["Mic", "Webcam"]
    # Index is the position in the full device table, not the filtered list
    assert devices[0] == {"index": 1, "name": "Mic", "hostapi": 1}
    assert devices[1] == {"index": 2, "name": "Webcam", "hostapi": 1}


def test_list_input_devices_empty_when_none_have_input(fake_sd):
    fake_sd.add_device("Speaker", max_input_channels=0)
    assert list_input_devices() == []


# ---------------------------------------------------------------------------
# default_input_device_index
# ---------------------------------------------------------------------------

def test_default_input_device_index_returns_configured(fake_sd):
    fake_sd.add_device("A", max_input_channels=1)
    idx = fake_sd.add_device("Mic", max_input_channels=1)
    fake_sd.set_default_input(idx)
    assert default_input_device_index() == idx


def test_default_input_device_index_treats_negative_as_none(fake_sd):
    # PortAudio uses -1 (not None) for "no default recording device"
    fake_sd.set_default_input(-1)
    assert default_input_device_index() is None


def test_default_input_device_index_handles_none(fake_sd):
    fake_sd.set_default_input(None)
    assert default_input_device_index() is None


def test_default_input_device_index_swallows_errors(fake_sd):
    # Accessing default.device[0] raising must degrade to None, not crash.
    fake_sd.default.device = []
    assert default_input_device_index() is None


# ---------------------------------------------------------------------------
# is_input_config_supported
# ---------------------------------------------------------------------------

def test_is_input_config_supported_true_and_false(fake_sd):
    idx = fake_sd.add_device(
        name="Mic",
        max_input_channels=1,
        supported_rates=[48000, 44100],
        supported_dtypes=["float32"],
    )
    assert is_input_config_supported(idx, 48000, 1, 32) is True
    assert is_input_config_supported(idx, 44100, 1, 32) is True
    assert is_input_config_supported(idx, 96000, 1, 32) is False  # unsupported rate
    assert is_input_config_supported(idx, 48000, 2, 32) is False  # unsupported channels


# ---------------------------------------------------------------------------
# get_audio_device_capabilities
# ---------------------------------------------------------------------------

def test_capabilities_empty_when_no_device_and_no_default(fake_sd):
    fake_sd.set_default_input(-1)
    caps = get_audio_device_capabilities(None)
    assert caps == {
        "samplerates": [],
        "bitdepths": [],
        "max_channels": None,
        "default_samplerate": None,
    }


def test_capabilities_empty_when_query_fails(fake_sd):
    # An out-of-range index makes query_devices raise; caps degrade to empty
    caps = get_audio_device_capabilities(99)
    assert caps["samplerates"] == []
    assert caps["max_channels"] is None


def test_capabilities_reports_supported_rates_and_bitdepths(fake_sd):
    idx = fake_sd.add_device(
        name="Mic",
        max_input_channels=2,
        default_samplerate=48000,
        supported_rates=[16000, 44100, 48000],
        supported_dtypes=["int16", "float32"],
    )
    caps = get_audio_device_capabilities(idx, channels=1)
    assert caps["max_channels"] == 2
    assert caps["default_samplerate"] == 48000
    assert caps["samplerates"] == [16000, 44100, 48000]
    # int16 (16) and float32 (32) both work; 64 rides along with 32
    assert caps["bitdepths"] == [16, 32, 64]


def test_capabilities_float32_only_yields_32_and_64(fake_sd):
    idx = fake_sd.add_device(
        name="Mic",
        max_input_channels=1,
        default_samplerate=44100,
        supported_rates=[44100],
        supported_dtypes=["float32"],
    )
    caps = get_audio_device_capabilities(idx)
    assert caps["bitdepths"] == [32, 64]


def test_capabilities_appends_native_rate_outside_candidate_list(fake_sd):
    # Test that a sampling rate not in the default list of candidate sampling rates
    # to probe is nevertheless listed, if it is the device's own default sampling rate
    noncandidate_sr = max(CANDIDATE_SAMPLE_RATES) + 100
    idx = fake_sd.add_device(
        name="Mic",
        max_input_channels=1,
        default_samplerate=noncandidate_sr,
        supported_rates=[48000, noncandidate_sr],
        supported_dtypes=["float32"],
    )
    caps = get_audio_device_capabilities(idx)
    assert caps["samplerates"] == [48000, noncandidate_sr]


def test_capabilities_resolves_none_device_via_default(fake_sd):
    idx = fake_sd.add_device(
        name="Mic",
        max_input_channels=1,
        default_samplerate=48000,
        supported_rates=[48000],
        supported_dtypes=["float32"],
    )
    fake_sd.set_default_input(idx)
    caps = get_audio_device_capabilities(None)
    assert caps["default_samplerate"] == 48000
    assert caps["samplerates"] == [48000]
