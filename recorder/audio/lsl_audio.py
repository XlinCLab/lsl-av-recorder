from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Union

import numpy as np
import sounddevice as sd
from pylsl import StreamInfo, local_clock


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


class AudioLSLStreamer:
    def __init__(self, s: AudioStreamSettings, sample_cb: Callable, status_cb: Optional[Callable[[str], None]] = None):
        self.s = s
        self.sample_cb = sample_cb
        self.status_cb = status_cb
        # self.outlet: Optional[StreamOutlet] = None
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
        info = StreamInfo(
            name=self.s.stream_name,
            type=self.s.stream_type,
            channel_count=self.s.channels,
            nominal_srate=float(self.s.samplerate),
            channel_format=chfmt,
            source_id=self.s.source_id,
        )
        # self.outlet = StreamOutlet(info, chunk_size=0, max_buffered=360)

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

            # # if self.outlet:
            # #     self.outlet.push_chunk(x.tolist(), timestamp=ts0)
            # Custom callback function to write to XDF files 
            if self.sample_cb:
                self.sample_cb(timestamps, x)

        self.stream = sd.InputStream(
            device=self.s.device,
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
        # self.outlet = None
        self.info("AudioLSL: stopped.")
