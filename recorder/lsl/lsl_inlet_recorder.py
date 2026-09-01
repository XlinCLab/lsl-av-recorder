from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
from pylsl import (StreamInfo, StreamInlet, cf_double64, cf_float32, cf_int8,
                   cf_int16, cf_int32, cf_int64, cf_string, local_clock)

from ..xdf.xdf_writer import XDFWriter


def extract_channel_info(stream_info: StreamInfo) -> List[Dict[str, str]]:
    """
    Read per-channel metadata (e.g. label, unit, type) from a StreamInfo's
    desc() XML, following the standard LSL layout:
    <desc><channels><channel><label>...</label>...</channel>...</channels></desc>.

    NB: stream_info must carry the extended description (e.g. from StreamInlet.info()).
    StreamInfo objects returned by resolve_streams() have an empty desc()
    and will yield an empty list here.
    """
    channels: List[Dict[str, str]] = []
    try:
        chan = stream_info.desc().child("channels").child("channel")
        while not chan.empty():
            fields: Dict[str, str] = {}
            field = chan.first_child()
            while not field.empty():
                fields[field.name()] = field.child_value()
                field = field.next_sibling()
            if fields:
                channels.append(fields)
            chan = chan.next_sibling("channel")
    except Exception:
        return []
    return channels


def lsl_format_to_xdf(fmt: int) -> Tuple[str, Optional[np.dtype]]:
    """
    Map an LSL channel-format enum to the (xdf format string, numpy dtype)
    pair used to decode inbound samples.
    cf_string (e.g. EEG marker / trigger streams) have no fixed-width numpy dtype
    as its samples are variable-length strings, so its dtype is None, signalling callers
    to keep samples as plain Python strings rather than casting to an array.
    """
    mapping = {
        cf_string: ("string", None),
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
        clock_offset_interval_s: float = 5.0,
        time_correction_timeout: float = 5.0,
        status_cb: Optional[Callable[[str, str], None]] = None,
    ):
        self.stream_info = stream_info
        self.stream_id = stream_id
        self.xdf_writer = xdf_writer
        self.chunk_size = chunk_size
        self.pull_timeout = pull_timeout
        self.clock_offset_interval_s = float(clock_offset_interval_s)
        self.time_correction_timeout = float(time_correction_timeout)
        self.status_cb = status_cb
        self._thread: Optional[threading.Thread] = None
        self._offset_thread: Optional[threading.Thread] = None
        self._running = False

        self.inlet = StreamInlet(stream_info, max_chunklen=chunk_size)
        fmt, dtype = lsl_format_to_xdf(stream_info.channel_format())
        self.xdf_format = fmt
        self.dtype = dtype

    def log(self, msg: str, loglevel: str = "INFO"):
        if self.status_cb:
            self.status_cb(msg, loglevel)

    def warning(self, msg: str):
        self.log(msg=msg, loglevel="WARNING")

    def error(self, msg: str):
        self.log(msg=msg, loglevel="ERROR")

    def start(self):
        if self._running:
            return
        self._running = True
        uid = self.stream_info.uid()
        self._thread = threading.Thread(
            target=self._loop, name=f"LSLInlet-{uid}", daemon=True
        )
        self._thread.start()
        # Dedicated thread for clock synchronization, mirroring LabRecorder's per-inlet time_correction loop.
        # Kept separate from the sample-pulling loop so a slow/blocking time_correction() never stalls data capture.
        self._offset_thread = threading.Thread(
            target=self._offset_loop, name=f"LSLOffset-{uid}", daemon=True
        )
        self._offset_thread.start()

    def stop(self):
        if not self._running:
            return
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._offset_thread:
            # May be blocked inside time_correction(); allow up to its timeout
            self._offset_thread.join(timeout=2.0 + self.time_correction_timeout)
            self._offset_thread = None

    def _loop(self):
        while self._running:
            samples, timestamps = self.inlet.pull_chunk(timeout=self.pull_timeout)
            self._process_chunk(samples, timestamps)

    def _process_chunk(self, samples, timestamps):
        if not timestamps:
            return
        # String samples (e.g. marker/trigger streams) have no fixed dtype
        values = samples if self.dtype is None else np.asarray(samples, dtype=self.dtype)
        ts = np.asarray(timestamps, dtype=np.float64)
        self.xdf_writer.write_lsl_samples(self.stream_id, ts, values)

    def _offset_loop(self):
        # Take an initial measurement immediately so the file has an early
        # anchor, then repeat on the configured interval.
        self._record_clock_offset()
        next_t = self._next_t()
        while self._running:
            if time.monotonic() >= next_t:
                self._record_clock_offset()
                next_t = self._next_t()
            time.sleep(0.05)

    def _next_t(self):
        return time.monotonic() + self.clock_offset_interval_s

    def _record_clock_offset(self):
        """
        Measure the offset between this inlet's (remote) clock and the
        recorder's local clock and hand it to the XDF writer. Failures are
        non-fatal: a stream that cannot answer a time_correction ping simply
        contributes no offset sample this cycle.
        """
        try:
            offset = self.inlet.time_correction(timeout=self.time_correction_timeout)
            # Sample the local clock as close to the measurement as possible.
            now = local_clock()
        except TimeoutError:
            self.warning(f"time_correction timed out for <{self.stream_info.name()}>; skipping this clock offset measurement")
            return
        except Exception as exc:
            self.warning(f"time_correction failed for <{self.stream_info.name()}>: {exc}")
            return
        try:
            self.xdf_writer.record_clock_offset(self.stream_id, offset=offset, now=now)
        except Exception as exc:
            self.warning(f"Failed to record clock offset for <{self.stream_info.name()}>: {exc}")
