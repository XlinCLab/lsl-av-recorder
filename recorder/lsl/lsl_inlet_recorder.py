from __future__ import annotations

import threading
from typing import Optional, Tuple

import numpy as np
from pylsl import (StreamInfo, StreamInlet, cf_double64, cf_float32, cf_int8,
                   cf_int16, cf_int32, cf_int64, cf_string)

from ..xdf.xdf_writer import XDFWriter


def lsl_format_to_xdf(fmt: int) -> Tuple[str, np.dtype]:
    if fmt == cf_string:
        raise ValueError("LSL string channel format is not supported for XDF writing")
    mapping = {
        cf_float32: ("float32", np.float32),
        cf_double64: ("double64", np.float64),
        cf_int8: ("int8", np.int8),
        cf_int16: ("int16", np.int16),
        cf_int32: ("int32", np.int32),
        cf_int64: ("int64", np.int64),
    }
    if fmt not in mapping:
        raise ValueError(f"Unsupported LSL channel format: {fmt}")
    return mapping[fmt]


class LslInletRecorder:
    def __init__(
        self,
        stream_info: StreamInfo,
        stream_id: int,
        xdf_writer: XDFWriter,
        chunk_size: int = 128,
        pull_timeout: float = 0.1,
    ):
        self.stream_info = stream_info
        self.stream_id = stream_id
        self.xdf_writer = xdf_writer
        self.chunk_size = chunk_size
        self.pull_timeout = pull_timeout
        self._thread: Optional[threading.Thread] = None
        self._running = False

        self.inlet = StreamInlet(stream_info, max_chunklen=chunk_size)
        fmt, dtype = lsl_format_to_xdf(stream_info.channel_format())
        self.xdf_format = fmt
        self.dtype = dtype

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name=f"LSLInlet-{self.stream_info.uid()}", daemon=True)
        self._thread.start()

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _loop(self):
        while self._running:
            samples, timestamps = self.inlet.pull_chunk(timeout=self.pull_timeout)
            if not timestamps:
                continue
            values = np.asarray(samples, dtype=self.dtype)
            ts = np.asarray(timestamps, dtype=np.float64)
            self.xdf_writer.write_lsl_samples(self.stream_id, ts, values)
