from __future__ import annotations
import struct
import threading
import time
import xml.etree.ElementTree as ET
from typing import BinaryIO, Dict, Optional

import numpy as np


# XDF chunk tags
TAG_FILE_HEADER = 1
TAG_STREAM_HEADER = 2
TAG_SAMPLES = 3
TAG_CLOCK_OFFSET = 4
TAG_STREAM_FOOTER = 6


class XDFWriter:
    """
    Minimal XDF writer for:
      - 1 audio stream (continuous)
      - N video streams (frame index + timestamp)
    """

    def __init__(self, path: str):
        self.path = path
        self.f: Optional[BinaryIO] = None
        self._lock = threading.Lock()
        self._next_stream_id = 1
        self.streams: Dict[str, int] = {}
        self._started = False

    # -------------------------
    # Low-level helpers
    # -------------------------

    def _write_chunk(self, tag: int, payload: bytes):
        self.f.write(struct.pack("<HI", tag, len(payload)))
        self.f.write(payload)

    def _write_file_header(self):
        self._write_chunk(TAG_FILE_HEADER, b"XDF:1.0")

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

        if extra:
            desc = ET.SubElement(root, "desc")
            for k, v in extra.items():
                ET.SubElement(desc, k).text = str(v)

        return ET.tostring(root, encoding="utf-8")

    def _write_stream_header(self, stream_id: int, xml: bytes):
        payload = struct.pack("<I", stream_id) + xml
        self._write_chunk(TAG_STREAM_HEADER, payload)

    def _write_stream_footer(self, stream_id: int):
        payload = struct.pack("<I", stream_id)
        self._write_chunk(TAG_STREAM_FOOTER, payload)

    # -------------------------
    # Public API
    # -------------------------

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
        sid = self._next_stream_id
        self._next_stream_id += 1

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
        sid = self._next_stream_id
        self._next_stream_id += 1

        xml = self._make_stream_header_xml(
            name=name,
            stype="Video",
            channel_count=1,
            srate=fps or 0.0,
            fmt="int64",
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

    def write_audio(
        self,
        stream_id: int,
        timestamps: np.ndarray,
        samples: np.ndarray,
    ):
        """
        samples shape: (n_samples, n_channels)
        """
        timestamps = np.asarray(timestamps, dtype=np.float64)
        samples = np.asarray(samples, dtype=np.float32)

        n = len(timestamps)
        payload = struct.pack("<I", n)
        payload += timestamps.tobytes(order="C")
        payload += samples.tobytes(order="C")

        with self._lock:
            self._write_chunk(TAG_SAMPLES, payload)

    def write_video_frames(
        self,
        stream_id: int,
        timestamps: np.ndarray,
        frame_indices: np.ndarray,
    ):
        timestamps = np.asarray(timestamps, dtype=np.float64)
        frame_indices = np.asarray(frame_indices, dtype=np.int64)

        n = len(timestamps)
        payload = struct.pack("<I", n)
        payload += timestamps.tobytes()
        payload += frame_indices.tobytes()

        with self._lock:
            self._write_chunk(TAG_SAMPLES, payload)

    def stop(self):
        if not self._started:
            return

        with self._lock:
            for sid in self.streams.values():
                self._write_stream_footer(sid)

            self.f.flush()
            self.f.close()

        self._started = False
