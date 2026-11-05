"""Building the settings to open an audio stream from the saved configuration."""
from __future__ import annotations

from typing import Callable, Optional

from ..config import AudioConfig
from .device_identity import identity_from_audio_config, resolve_identity
from .devices import list_input_devices
from .lsl_audio import AudioStreamSettings


def resolve_audio_device_name(device) -> str:
    """Human-readable name for an audio input device selector, for display purposes."""
    if device is None:
        return "(default)"
    if isinstance(device, int):
        try:
            for d in list_input_devices():
                if d["index"] == device:
                    return str(d["name"])
        except Exception:
            pass
        return f"Device {device}"
    return str(device)


def build_audio_stream_settings(
    audio_cfg: AudioConfig,
    log: Optional[Callable[[str, str], None]] = None,
) -> AudioStreamSettings:
    """AudioStreamSettings for `audio_cfg`, with the device found by its identity
    (name, host API, location) in the current device list, not by a saved PortAudio
    index that may have shifted. Raises RuntimeError if the saved device is not
    available. `log(message, level)` receives what was resolved."""
    def emit(message: str, level: str = "INFO"):
        if log:
            log(message, level)

    aset = AudioStreamSettings(
        device=None,
        samplerate=audio_cfg.SampleRate,
        channels=audio_cfg.Channels,
        bitdepth=audio_cfg.BitDepth,
        sample_format=audio_cfg.SampleFormat,
        stream_name=audio_cfg.StreamName or "Audio",
        stream_type="Audio",
        source_id=f"audio:{audio_cfg.Device or 'default'}",
    )
    identity = identity_from_audio_config(audio_cfg)
    if identity is not None:
        match = resolve_identity(identity, list_input_devices())
        if not match.found:
            raise RuntimeError(
                f"The configured audio input device is not available: {identity.describe()} "
                f"({match.detail}). Select the device in the Audio tab."
            )
        aset.device = match.device["index"]
        aset.source_id = f"audio:{identity.name}"
        emit(
            f"Audio device resolved: {identity.describe()} -> "
            f"[{match.device['index']}] {match.device['name']} ({match.level})",
            "WARNING" if match.degraded else "INFO",
        )
    elif audio_cfg.Device is not None:
        try:
            aset.device = int(audio_cfg.Device)
        except ValueError:
            aset.device = audio_cfg.Device
        emit(
            f"Audio device [{audio_cfg.Device}] was configured by index only, "
            "which may have changed since it was chosen; its identity is unknown.",
            "WARNING",
        )
    aset.device_name = resolve_audio_device_name(aset.device)
    return aset
