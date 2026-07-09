from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Union

import numpy as np
import sounddevice as sd
from pylsl import local_clock


@dataclass
class AudioStreamSettings:
    device: Optional[Union[int, str]] = None
    samplerate: int = 48000
    channels: int = 1
    bitdepth: int = 32  # 16/32/64
    stream_name: str = "Audio"
    stream_type: str = "Audio"
    source_id: str = "audio"

def _lsl_format(bitdepth: int) -> str:
    if bitdepth == 16:
        return "int16"
    if bitdepth == 32:
        return "float32"
    if bitdepth == 64:
        return "double64"
    raise ValueError("bitdepth must be 16, 32, or 64")


def _dtype_format(bitdepth: int) -> str:
    return "int16" if bitdepth == 16 else "float32"


def _resolve_input_device(
    requested: Optional[Union[int, str]],
    samplerate: int,
    channels: int,
    dtype: str,
):
    """Resolve the input device to hand to sounddevice.

    A device index can be enumerated by PortAudio yet still fail to actually open
    (seen on Windows, where some MME/WASAPI entries are stale or don't support the
    requested sample rate/channels). Rather than pick a device by index alone and let
    PortAudio raise a cryptic error at stream-open time, validate candidates with
    `check_input_settings` first and pick the first one that actually works.
    """
    if requested is not None:
        try:
            sd.check_input_settings(device=requested, samplerate=samplerate, channels=channels, dtype=dtype)
        except Exception as exc:
            raise RuntimeError(
                f"Configured audio input device (device={requested!r}) is not usable "
                f"with samplerate={samplerate}, channels={channels}: {exc}"
            ) from exc
        return requested

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
        try:
            sd.check_input_settings(device=idx, samplerate=samplerate, channels=channels, dtype=dtype)
            return idx
        except Exception as exc:
            last_error = exc
            continue

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
        chfmt = _lsl_format(self.s.bitdepth)

        dtype = _dtype_format(self.s.bitdepth)

        def callback(indata, frames, time_info, status):
            if status:
                self.info(f"Audio status: {status}")

            # PortAudio time -> LSL time; timestamp refers to first sample in chunk
            offset = local_clock() - time_info.currentTime
            ts0 = time_info.inputBufferAdcTime + offset
            # Compute timestamps for each sample
            timestamps = ts0 + np.arange(frames) / self.s.samplerate

            x = indata.copy()
            if self.s.bitdepth == 64:
                x = x.astype(np.float64, copy=False)
            elif self.s.bitdepth == 32:
                x = x.astype(np.float32, copy=False)

            # Custom callback function to write to XDF files 
            if self.sample_cb:
                self.sample_cb(timestamps, x)

        self.stream = sd.InputStream(
            device=_resolve_input_device(self.s.device, self.s.samplerate, self.s.channels, dtype),
            samplerate=self.s.samplerate,
            channels=self.s.channels,
            dtype=dtype,
            callback=callback,
            blocksize=0,
        )
        self.stream.start()
        self.info(f"AudioLSL: streaming '{self.s.stream_name}' sr={self.s.samplerate} ch={self.s.channels} fmt={chfmt}")

    def stop(self):
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            finally:
                self.stream = None
        self.info("AudioLSL: stopped.")
