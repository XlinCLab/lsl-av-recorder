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


def _resolve_input_device(requested: Optional[Union[int, str]]):
    """Resolve the input device to hand to sounddevice, falling back away from an
    unusable default (-1) rather than letting PortAudio raise a cryptic error."""
    if requested is not None:
        return requested
    try:
        default_idx = sd.default.device[0]
    except Exception:
        default_idx = None
    if default_idx is not None and default_idx >= 0:
        return default_idx
    # No usable default input device (seen on some Windows machines with no
    # configured default recording device); fall back to the first device
    # that supports input.
    try:
        devices = sd.query_devices()
    except Exception:
        devices = []
    for i, d in enumerate(devices):
        if d.get("max_input_channels", 0) > 0:
            return i
    raise RuntimeError(
        "No audio input device is available. Select a specific input device in the "
        "Audio tab, or disable audio recording."
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
            device=_resolve_input_device(self.s.device),
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
