from __future__ import annotations

import datetime
import struct
import threading
import uuid
import xml.etree.ElementTree as ET
from typing import BinaryIO, Dict, Optional

import numpy as np

# XDF chunk tags
TAG_FILE_HEADER = 1
TAG_STREAM_HEADER = 2
TAG_SAMPLES = 3
TAG_CLOCK_OFFSET = 4
TAG_BOUNDARY = 5
TAG_STREAM_FOOTER = 6

# XDF header bytes
XDF_MAGIC_BYTES = b"XDF:"


class XDFWriter:
    """
    Minimal XDF writer for:
      - 1 audio stream (continuous)
      - N video streams (frame index + timestamp)
      - N generic LSL streams (numeric samples)
    """

    def __init__(self, path: str, clock_offset_interval_s: float = 5.0):
        self.path = path
        self.f: Optional[BinaryIO] = None
        self._lock = threading.Lock()
        self._next_stream_id = 1
        self.streams: Dict[str, int] = {}
        self._started = False
        self._sample_count: Dict[int, int] = {}
        self._first_timestamp: Dict[int, float] = {}
        self._last_timestamp: Dict[int, float] = {}
        self._clock_offsets: Dict[int, list[tuple[float, float]]] = {}
        self._clock_offset_interval_s = float(clock_offset_interval_s)
        self._last_clock_offset_time: Dict[int, float] = {}

    def _get_next_stream_id(self):
        sid = self._next_stream_id
        self._next_stream_id += 1
        return sid

    def _update_timestamps(self, timestamps: np.ndarray, stream_id: int, n_samples: int):
        if stream_id not in self._first_timestamp:
            self._first_timestamp[stream_id] = timestamps[timestamps != 0][0]

        self._last_timestamp[stream_id] = timestamps[timestamps != 0][-1]
        self._sample_count[stream_id] = self._sample_count.get(stream_id, 0) + n_samples

    # -------------------------------------------------
    # Low-level binary helpers
    # -------------------------------------------------

    def _write_varlen_int(self, value: int):
        """
        Write an XDF variable-length integer.
        First byte = number of bytes that follow (1, 4, or 8)
        Then little-endian bytes of the value.
        """
        if value < 2**8:
            # 1 byte follows
            self.f.write(b"\x01")
            self.f.write(struct.pack("<B", value))
        elif value < 2**32:
            # 4 bytes follow
            self.f.write(b"\x04")
            self.f.write(struct.pack("<I", value))
        else:
            # 8 bytes follow
            self.f.write(b"\x08")
            self.f.write(struct.pack("<Q", value))

    def _write_varlen_int_to_buffer(self, buf: bytearray, value: int):
        if value < 2**8:
            buf.append(1)
            buf.append(value)
        elif value < 2**32:
            buf.append(4)
            buf += struct.pack("<I", value)
        else:
            buf.append(8)
            buf += struct.pack("<Q", value)

    def _format_timestamp(self, ts: float):
        if ts == 0:
            return b"\x00"  # TimeStampBytes = 0
        else:
            return b"\x08" + struct.pack("<d", ts)

    def _write_chunk_header(
        self,
        tag: int,
        payload_len: int,
        stream_id: int | None = None,
    ):
        """
        Write an XDF chunk header:
        [VarLenLength][Tag][Optional StreamID]
        payload_len: length of content only
        """
        # total length includes tag + optional stream id
        total_len = payload_len + 2  # 2 bytes for tag
        if stream_id is not None:
            total_len += 4  # 4 bytes for stream id

        # write length as variable-length integer
        self._write_varlen_int(total_len)
        # write tag
        self.f.write(tag.to_bytes(2, "little"))
        # optional stream id
        if stream_id is not None:
            self.f.write(stream_id.to_bytes(4, "little"))

    def _write_chunk(
        self,
        tag: int,
        payload: bytes,
        stream_id: int | None = None,
    ):
        """
        Write a full XDF chunk: header + payload
        """
        self._write_chunk_header(tag, len(payload), stream_id)
        self.f.write(payload)

    # -------------------------------------------------
    # File + stream headers
    # -------------------------------------------------

    def _write_file_header(self):
        self.f.write(XDF_MAGIC_BYTES)  # magic bytes

        # create XML header
        now = datetime.datetime.now()
        header_xml = f"""<?xml version="1.0"?>
<info>
  <version>1.0</version>
  <datetime>{now.strftime('%Y-%m-%dT%H:%M:%S')}</datetime>
</info>""".encode("utf-8")

        self._write_chunk(TAG_FILE_HEADER, header_xml)

    def _make_stream_header_xml(
        self,
        name: str,
        stype: str,
        channel_count: int,
        srate: float,
        fmt: str,
        source_id: str,
        extra: Optional[Dict[str, str]] = None,
    ) -> bytes:
        root = ET.Element("info")
        ET.SubElement(root, "name").text = name
        ET.SubElement(root, "type").text = stype
        ET.SubElement(root, "channel_count").text = str(channel_count)
        ET.SubElement(root, "nominal_srate").text = str(srate)
        ET.SubElement(root, "channel_format").text = fmt
        ET.SubElement(root, "source_id").text = source_id
        ET.SubElement(root, "uid").text = str(uuid.uuid4())

        if extra:
            desc = ET.SubElement(root, "desc")
            for k, v in extra.items():
                ET.SubElement(desc, k).text = str(v)

        return ET.tostring(root, encoding="utf-8")

    def _make_stream_footer_xml(self, stream_id: int) -> bytes:
        root = ET.Element("info")

        ET.SubElement(
            root, "first_timestamp"
        ).text = str(self._first_timestamp.get(stream_id, 0.0))

        ET.SubElement(
            root, "last_timestamp"
        ).text = str(self._last_timestamp.get(stream_id, 0.0))

        ET.SubElement(
            root, "sample_count"
        ).text = str(self._sample_count.get(stream_id, 0))

        # Clock offsets, if present
        offsets = self._clock_offsets.get(stream_id)
        if offsets:
            clock_offsets_el = ET.SubElement(root, "clock_offsets")

            for t, v in offsets:
                offset_el = ET.SubElement(clock_offsets_el, "offset")
                ET.SubElement(offset_el, "time").text = str(t)
                ET.SubElement(offset_el, "value").text = str(v)

        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    def _write_stream_header(self, stream_id: int, xml: bytes):
        self._write_chunk(tag=TAG_STREAM_HEADER, payload=xml, stream_id=stream_id)

    def _write_stream_footer(self, stream_id: int):
        xml = self._make_stream_footer_xml(stream_id)
        self._write_chunk(TAG_STREAM_FOOTER, xml, stream_id)

    def _write_stream_offset(self, stream_id: int, now: float, offset: float):  # TODO not yet used, may be needed for multiple streams
        """
        Write a clock offset chunk (TAG_CLOCK_OFFSET) and
        store it for inclusion in the stream footer.

        now: current time (float64)
        offset: offset to apply (float64)
        """
        collection_time = now - offset

        payload = struct.pack(
            "<dd",
            collection_time,  # time when offset was measured
            offset,           # offset value
        )

        # Write XDF clock offset chunk
        with self._lock:
            self._write_chunk(
                tag=TAG_CLOCK_OFFSET,
                payload=payload,
                stream_id=stream_id,
            )

            # Store for footer
            self._clock_offsets.setdefault(stream_id, []).append(
                (collection_time, offset)
            )

    def _write_boundary_chunk(self):
        """
        Write a boundary chunk (used for resync / recovery).
        """
        boundary_uuid = bytes([
            0x43, 0xA5, 0x46, 0xDC, 0xCB, 0xF5, 0x41, 0x0F,
            0xB3, 0x0E, 0xD5, 0x46, 0x73, 0x83, 0xCB, 0xE4,
        ])

        with self._lock:
            self._write_chunk(
                tag=TAG_BOUNDARY,
                payload=boundary_uuid,
                stream_id=None,
            )

    def _ensure_clock_offset(self, stream_id: int, timestamps):
        """
        Emit periodic clock offset chunks per stream (offset=0.0).
        This keeps pyxdf's clock_segments aligned with segments when
        all streams share the same clock domain.
        """
        if timestamps is None:
            return
        ts = np.asarray(timestamps, dtype=np.float64)
        if ts.size == 0:
            return
        ts0 = float(ts[0])
        last = self._last_clock_offset_time.get(stream_id)
        if last is not None and (ts0 - last) < self._clock_offset_interval_s:
            return
        # Use the first sample timestamp as the collection time; offset is zero
        self._write_stream_offset(stream_id, now=ts0, offset=0.0)
        self._last_clock_offset_time[stream_id] = ts0

    # -------------------------------------------------
    # Samples
    # -------------------------------------------------

    def _write_samples(
        self,
        stream_id: int,
        timestamps: np.ndarray,
        values: np.ndarray,
    ):
        """
        Write a Samples chunk.
        Each sample: [TimeStampBytes][TimeStamp][SampleValues]
        Preceded by [NumSamples (uint32)][SampleFormat (uint8)]
        """
        timestamps = np.asarray(timestamps, dtype=np.float64)
        values = np.asarray(values)  # type depends on stream (float32 or int64)
        if values.ndim == 1:
            values = values.reshape(-1, 1)
        n_samples = timestamps.shape[0]
        assert values.shape[0] == n_samples, "values/timestamps length mismatch"

        # Enforce strictly increasing timestamps across chunks
        last_ts = self._last_timestamp.get(stream_id)
        if last_ts is not None and timestamps[0] <= last_ts:
            dt = timestamps[1] - timestamps[0] if n_samples > 1 else 1e-3
            timestamps = last_ts + dt * (1 + np.arange(n_samples))
        self._update_timestamps(timestamps, stream_id, n_samples)

        # --- XDF sample payload ---
        # uint32: sample count
        # float64[n]: timestamps
        # float32[n, channels] | float64[n, channels]: values (row-major)

        payload = bytearray()
        buf = bytearray()
        self._write_varlen_int_to_buffer(buf, n_samples)
        payload += buf

        # Per-sample data
        for i in range(n_samples):
            payload += self._format_timestamp(timestamps[i])
            payload += values[i].tobytes(order="C")

        self._write_chunk(TAG_SAMPLES, bytes(payload), stream_id)

    # -------------------------------------------------
    # Public API
    # -------------------------------------------------

    def start(self):
        if self._started:
            return
        self.f = open(self.path, "wb")
        self._write_file_header()
        self._started = True

    def add_audio_stream(
        self,
        name: str,
        samplerate: float,
        channels: int,
        fmt: str = "float32",
        source_id: str = "audio",
    ) -> int:
        """
        Register an audio stream. Samples must be written via write_audio().
        """
        sid = self._get_next_stream_id()
        xml = self._make_stream_header_xml(
            name=name,
            stype="Audio",
            channel_count=channels,
            srate=samplerate,
            fmt=fmt,
            source_id=source_id,
        )

        with self._lock:
            self._write_stream_header(sid, xml)

        self.streams[name] = sid
        return sid

    def add_video_stream(
        self,
        name: str,
        camera_id: str,
        video_path: str,
        width: int,
        height: int,
        fps: Optional[float] = None,
    ) -> int:
        """
        Video stream stores frame index (int64) with timestamps.
        """
        sid = self._get_next_stream_id()
        xml = self._make_stream_header_xml(
            name=name,
            stype="Video",
            channel_count=1,  # NB: only one channel for video in XDF because only frame index is stored
            srate=fps or 0.0,  # NB: 0.0 marks the sampling rate as "irregular", expected to be positive otherwise
            fmt="int64",  # TODO check this
            source_id=f"camera:{camera_id}",
            extra={
                "video_path": video_path,
                "width": width,
                "height": height,
                "fps": fps or "irregular",
            },
        )

        with self._lock:
            self._write_stream_header(sid, xml)

        self.streams[name] = sid
        return sid

    def add_lsl_stream(
        self,
        name: str,
        stype: str,
        channel_count: int,
        srate: float,
        fmt: str,
        source_id: str,
        extra: Optional[Dict[str, str]] = None,
        key: Optional[str] = None,
    ) -> int:
        """
        Register a generic LSL stream (numeric samples). Samples must be written via write_lsl_samples().
        """
        sid = self._get_next_stream_id()
        xml = self._make_stream_header_xml(
            name=name,
            stype=stype,
            channel_count=channel_count,
            srate=srate,
            fmt=fmt,
            source_id=source_id,
            extra=extra,
        )

        with self._lock:
            self._write_stream_header(sid, xml)

        stream_key = key or f"{name}:{source_id}:{sid}"
        if stream_key in self.streams:
            stream_key = f"{stream_key}:{uuid.uuid4()}"
        self.streams[stream_key] = sid
        return sid

    def write_audio(
        self,
        stream_id: int,
        timestamps: np.ndarray,
        samples: np.ndarray,
    ):
        self._write_boundary_chunk()
        self._ensure_clock_offset(stream_id, timestamps)
        with self._lock:
            self._write_samples(stream_id, timestamps, samples)

    def write_video_frames(
        self,
        stream_id: int,
        timestamps: np.ndarray,
        frame_indices: np.ndarray,
    ):
        self._write_boundary_chunk()
        self._ensure_clock_offset(stream_id, timestamps)
        frame_indices = np.asarray(frame_indices, dtype=np.int64)
        with self._lock:
            self._write_samples(stream_id, timestamps, frame_indices)

    def write_lsl_samples(
        self,
        stream_id: int,
        timestamps: np.ndarray,
        samples: np.ndarray,
    ):
        self._write_boundary_chunk()
        self._ensure_clock_offset(stream_id, timestamps)
        with self._lock:
            self._write_samples(stream_id, timestamps, samples)

    def stop(self):
        if not self._started:
            return

        with self._lock:
            for sid in self.streams.values():
                self._write_stream_footer(sid)

            self.f.flush()
            self.f.close()

        self._started = False
