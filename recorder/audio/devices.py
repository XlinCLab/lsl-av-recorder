from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

import sounddevice as sd

from ..audio.constants import BITDEPTH_DTYPES, CANDIDATE_SAMPLE_RATES


def list_input_devices() -> List[Dict[str, Any]]:
    devs = sd.query_devices()
    out = []
    for i, d in enumerate(devs):
        if d.get("max_input_channels", 0) > 0:
            out.append({"index": i, "name": d.get("name"), "hostapi": d.get("hostapi")})
    return out


def default_input_device_index():
    try:
        di = sd.default.device[0]
        # PortAudio uses -1 (rather than None) to signal "no default device",
        # which notably happens on some Windows machines with no configured
        # default recording device.
        return di if di is not None and di >= 0 else None
    except Exception:
        return None


def is_input_config_supported(
    device: Optional[Union[int, str]],
    samplerate: int,
    channels: int,
    bitdepth: int,
) -> bool:
    dtype = BITDEPTH_DTYPES.get(int(bitdepth), "float32")
    try:
        sd.check_input_settings(device=device, samplerate=samplerate, channels=channels, dtype=dtype)
        return True
    except Exception:
        return False


def get_audio_device_capabilities(
    device: Optional[Union[int, str]],
    channels: int = 1,
) -> Dict[str, Any]:
    """Probe which sample rates/bit depths a given input device actually supports,
    so the GUI can offer only valid choices instead of a static list that may not
    match the device (which can otherwise lead to a silently corrected sample rate
    at recording time)."""
    caps: Dict[str, Any] = {
        "samplerates": [],
        "bitdepths": [],
        "max_channels": None,
        "default_samplerate": None,
    }
    # sd.query_devices(None) returns the full device table rather than a single
    # device's info, so resolve "(default)" to a concrete index first.
    resolved_device = device if device is not None else default_input_device_index()
    if resolved_device is None:
        return caps
    try:
        info = sd.query_devices(resolved_device)
    except Exception:
        return caps
    device = resolved_device

    max_channels = int(info.get("max_input_channels") or 0)
    caps["max_channels"] = max_channels
    default_sr = info.get("default_samplerate")
    default_samplerate = int(round(default_sr)) if default_sr else None
    caps["default_samplerate"] = default_samplerate

    test_channels = max(1, min(int(channels) or 1, max_channels or 1))

    candidate_rates = CANDIDATE_SAMPLE_RATES.copy()
    if default_samplerate and default_samplerate not in candidate_rates:
        candidate_rates.append(default_samplerate)
    supported_rates = sorted(
        {
            sr
            for sr in candidate_rates
            if is_input_config_supported(device, sr, test_channels, 32)
        }
    )
    caps["samplerates"] = supported_rates

    probe_rate = supported_rates[0] if supported_rates else (default_samplerate or 44100)
    bitdepths = []
    for bitdepth in (16, 32):
        if is_input_config_supported(device, probe_rate, test_channels, bitdepth):
            bitdepths.append(bitdepth)
            if bitdepth == 32:
                bitdepths.append(64)
    caps["bitdepths"] = sorted(set(bitdepths))

    return caps
