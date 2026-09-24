from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Union

import numpy as np
import sounddevice as sd
from pylsl import local_clock

from .constants import (BITDEPTH_CONVERSION_FLOAT, BITDEPTH_DTYPES,
                        DEFAULT_BIT_DEPTH, DEFAULT_SAMPLE_FORMAT,
                        SAMPLE_FORMATS)


@dataclass
class AudioStreamSettings:
    device: Optional[Union[int, str]] = None
    device_name: Optional[str] = None
    samplerate: int = 48000
    channels: int = 1
    bitdepth: int = DEFAULT_BIT_DEPTH  # capture resolution requested from the device: 16 or 32
    sample_format: str = DEFAULT_SAMPLE_FORMAT  # dtype written to the XDF: float32 or int16
    stream_name: str = "Audio"
    stream_type: str = "Audio"
    source_id: str = "audio"

def _capture_dtype(bitdepth: int) -> str:
    """PortAudio dtype to request for a capture bit depth."""
    try:
        return BITDEPTH_DTYPES[int(bitdepth)]
    except (KeyError, ValueError, TypeError):
        raise ValueError(f"bitdepth must be one of {sorted(BITDEPTH_DTYPES)}, got {bitdepth!r}") from None


def convert_samples(samples: np.ndarray, sample_format: str) -> np.ndarray:
    """Convert captured samples to the dtype that will be written to the XDF.

    int16 <-> float32 use the standard full-scale convention (int16 / 32768 = float in [-1, 1]).
    """
    if sample_format not in SAMPLE_FORMATS:
        raise ValueError(f"sample_format must be one of {SAMPLE_FORMATS}, got {sample_format!r}")
    target = np.dtype(sample_format)
    if samples.dtype == target:
        return samples
    if samples.dtype == np.int16 and target == np.float32:
        return samples.astype(np.float32) / np.float32(BITDEPTH_CONVERSION_FLOAT)
    if samples.dtype == np.float32 and target == np.int16:
        return np.clip(np.rint(samples * BITDEPTH_CONVERSION_FLOAT), -BITDEPTH_CONVERSION_FLOAT, BITDEPTH_CONVERSION_FLOAT-1).astype(np.int16)
    raise ValueError(f"Cannot convert {samples.dtype} samples to {sample_format}")


def _device_default_samplerate(device) -> Optional[int]:
    try:
        rate = sd.query_devices(device).get("default_samplerate")
        return int(round(rate)) if rate else None
    except Exception:
        return None


def _try_device(device, samplerate: int, channels: int, dtype: str) -> tuple[Optional[int], Optional[Exception]]:
    """Check whether `device` works at `samplerate`, falling back to the device's own
    native/default sample rate if the requested one is rejected. Returns
    (working_samplerate, None) on success, or (None, last_error) on failure."""
    try:
        sd.check_input_settings(device=device, samplerate=samplerate, channels=channels, dtype=dtype)
        return samplerate, None
    except Exception as exc:
        native_rate = _device_default_samplerate(device)
        if native_rate and native_rate != samplerate:
            try:
                sd.check_input_settings(device=device, samplerate=native_rate, channels=channels, dtype=dtype)
                return native_rate, None
            except Exception as exc2:
                return None, exc2
        return None, exc


def _resolve_input_device(
    requested: Optional[Union[int, str]],
    samplerate: int,
    channels: int,
    dtype: str,
) -> tuple[Union[int, str], int]:
    """Resolve (device, samplerate) to hand to sounddevice.

    A device index can be enumerated by PortAudio yet still fail to actually open
    (seen on Windows, where some MME/WASAPI entries are stale, or only support their
    own native sample rate rather than the one requested). Rather than pick a device
    by index alone and let PortAudio raise a cryptic error at stream-open time,
    validate candidates with `check_input_settings` first, falling back to a
    candidate's own native sample rate if the requested one doesn't work.
    """
    if requested is not None:
        resolved_rate, err = _try_device(requested, samplerate, channels, dtype)
        if resolved_rate is None:
            raise RuntimeError(
                f"Configured audio input device (device={requested!r}) is not usable "
                f"with samplerate={samplerate}, channels={channels}: {err}"
            )
        return requested, resolved_rate

    candidates: list[int] = []
    try:
        default_idx = sd.default.device[0]
    except Exception:
        default_idx = None
    if default_idx is not None and default_idx >= 0:
        candidates.append(default_idx)
    try:
        devices = sd.query_devices()
    except Exception:
        devices = []
    for i, d in enumerate(devices):
        if d.get("max_input_channels", 0) > 0 and i not in candidates:
            candidates.append(i)

    last_error: Optional[Exception] = None
    for idx in candidates:
        resolved_rate, err = _try_device(idx, samplerate, channels, dtype)
        if resolved_rate is not None:
            return idx, resolved_rate
        last_error = err

    detail = f" (last error: {last_error})" if last_error else ""
    raise RuntimeError(
        "No usable audio input device was found for the configured sample rate/channels. "
        "Select a specific input device in the Audio tab, adjust the sample rate/channels, "
        f"or disable audio recording.{detail}"
    )


class AudioLSLStreamer:
    def __init__(self, s: AudioStreamSettings, sample_cb: Callable, status_cb: Optional[Callable[[str], None]] = None):
        self.s = s
        self.sample_cb = sample_cb
        self.status_cb = status_cb
        self.stream: Optional[sd.InputStream] = None

    def log(self, msg: str, loglevel: str = "INFO"):
        if self.status_cb:
            self.status_cb(msg, loglevel)

    def info(self, msg: str):
        self.log(msg, loglevel="INFO")

    def warning(self, msg: str):
        self.log(msg, loglevel="WARNING")

    def error(self, msg: str):
        self.log(msg, loglevel="ERROR")

    def start(self):
        sample_format = self.s.sample_format
        dtype = _capture_dtype(self.s.bitdepth)

        def callback(indata, frames, time_info, status):
            if status:
                self.info(f"Audio status: {status}")

            # PortAudio time -> LSL time; timestamp refers to first sample in chunk
            offset = local_clock() - time_info.currentTime
            ts0 = time_info.inputBufferAdcTime + offset
            # Compute timestamps for each sample
            timestamps = ts0 + np.arange(frames) / self.s.samplerate

            x = convert_samples(indata.copy(), sample_format)

            # Custom callback function to write to XDF files 
            if self.sample_cb:
                self.sample_cb(timestamps, x)

        try:
            device, resolved_samplerate = _resolve_input_device(
                requested=self.s.device,
                samplerate=self.s.samplerate,
                channels=self.s.channels,
                dtype=dtype,
            )
        except RuntimeError:
            # Some host APIs/modes (e.g. exclusive WASAPI, ASIO) refuse format conversion;
            # int16 is the most widely accepted capture format, and convert_samples()
            # still delivers the configured sample_format.
            if dtype == "int16":
                raise
            device, resolved_samplerate = _resolve_input_device(
                requested=self.s.device,
                samplerate=self.s.samplerate,
                channels=self.s.channels,
                dtype="int16",
            )
            self.warning(
                f"Audio device does not support {dtype} capture; "
                f"falling back to int16 capture (stored as {sample_format})."
            )
            dtype = "int16"
            self.s.bitdepth = 16
        if resolved_samplerate != self.s.samplerate:
            self.warning(
                f"Requested sample rate {self.s.samplerate} Hz is not supported by the "
                f"selected audio device; using its native rate of {resolved_samplerate} Hz "
                "instead."
            )
            self.s.samplerate = resolved_samplerate

        self.stream = sd.InputStream(
            device=device,
            samplerate=self.s.samplerate,
            channels=self.s.channels,
            dtype=dtype,
            callback=callback,
            blocksize=0,
        )
        self.stream.start()
        self.info(f"AudioLSL: streaming '{self.s.stream_name}' sr={self.s.samplerate} ch={self.s.channels} capture={dtype} stored_as={sample_format}")

    def stop(self):
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            finally:
                self.stream = None
        self.info("AudioLSL: stopped.")
