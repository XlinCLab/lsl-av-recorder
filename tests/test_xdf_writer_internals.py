"""Tests for the low-level binary encoders of recorder.xdf.xdf_writer.XDFWriter."""
from __future__ import annotations

import io
import struct

import numpy as np
import pytest

from recorder.xdf.xdf_writer import TAG_SAMPLES, XDFWriter


@pytest.fixture
def writer():
    """An XDFWriter with its output redirected to an in-memory buffer.

    The path is never opened (start() is not called); tests call the low-level
    encoders directly, which write to writer.f.
    """
    w = XDFWriter(path="unused.xdf")
    w.f = io.BytesIO()
    return w


# ---------------------------------------------------------------------------
# _write_varlen_int
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value, expected",
    [
        (0, b"\x01" + struct.pack("<B", 0)),          # 1-byte length prefix
        (1, b"\x01" + struct.pack("<B", 1)),
        (255, b"\x01" + struct.pack("<B", 255)),      # largest 1-byte value
        (256, b"\x04" + struct.pack("<I", 256)),      # promotes to 4 bytes
        (2**32 - 1, b"\x04" + struct.pack("<I", 2**32 - 1)),  # largest 4-byte value
        (2**32, b"\x08" + struct.pack("<Q", 2**32)),  # promotes to 8 bytes
    ],
)
def test_write_varlen_int_boundaries(writer, value, expected):
    """The length prefix (1/4/8) is chosen by magnitude, followed by the value's
    little-endian bytes, at each byte-width boundary."""
    writer._write_varlen_int(value)
    assert writer.f.getvalue() == expected


def test_write_varlen_int_to_buffer_matches_stream_version(writer):
    """The buffer-based varlen encoder produces the same bytes as the
    stream-based one for the same value."""
    for value in (200, 300, 2**33):
        writer.f = io.BytesIO()
        writer._write_varlen_int(value)
        buf = bytearray()
        writer._write_varlen_int_to_buffer(buf=buf, value=value)
        assert bytes(buf) == writer.f.getvalue()


# ---------------------------------------------------------------------------
# _format_timestamp
# ---------------------------------------------------------------------------

def test_format_timestamp_zero_is_single_byte(writer):
    """A zero timestamp encodes as the single 'no timestamp' byte (0x00)."""
    assert writer._format_timestamp(0) == b"\x00"


def test_format_timestamp_nonzero_is_tagged_double(writer):
    """A non-zero timestamp encodes as 0x08 followed by a little-endian float64."""
    assert writer._format_timestamp(1.5) == b"\x08" + struct.pack("<d", 1.5)


# ---------------------------------------------------------------------------
# _write_chunk_header / _write_chunk
# ---------------------------------------------------------------------------

def test_chunk_header_length_excludes_stream_id_when_absent(writer):
    """With no stream id, the chunk length covers payload + 2 tag bytes,
    followed by the 2-byte tag."""
    writer._write_chunk_header(tag=TAG_SAMPLES, payload_len=10, stream_id=None)
    # varlen(12) == b"\x01\x0c", then tag 3 as 2 LE bytes.
    assert writer.f.getvalue() == b"\x01\x0c" + TAG_SAMPLES.to_bytes(2, "little")


def test_chunk_header_length_includes_stream_id(writer):
    """With a stream id, the length grows by 4 and the 4-byte stream id follows the tag."""
    writer._write_chunk_header(tag=TAG_SAMPLES, payload_len=10, stream_id=7)
    # varlen(16) == b"\x01\x10", tag, then stream id 7 as 4 LE bytes.
    assert writer.f.getvalue() == (
        b"\x01\x10" + TAG_SAMPLES.to_bytes(2, "little") + (7).to_bytes(4, "little")
    )


def test_write_chunk_appends_payload_after_header(writer):
    """_write_chunk writes the header immediately followed by the raw payload."""
    payload = b"abcd"
    writer._write_chunk(tag=TAG_SAMPLES, payload=payload, stream_id=None)
    written = writer.f.getvalue()
    assert written.endswith(payload)
    # Header length prefix accounts for payload(4) + tag(2) == 6.
    assert written == b"\x01\x06" + TAG_SAMPLES.to_bytes(2, "little") + payload


# ---------------------------------------------------------------------------
# _update_timestamps
# ---------------------------------------------------------------------------

def test_update_timestamps_skips_zeros_for_first_and_last(writer):
    """first/last timestamps ignore zero entries (zeros mark 'no timestamp'),
    while the sample count includes every sample."""
    ts = np.array([0.0, 100.0, 101.0, 0.0, 102.0], dtype=np.float64)
    writer._update_timestamps(timestamps=ts, stream_id=1, n_samples=ts.size)
    assert writer._first_timestamp[1] == 100.0
    assert writer._last_timestamp[1] == 102.0
    assert writer._sample_count[1] == 5


def test_update_timestamps_accumulates_sample_count(writer):
    """Sample counts accumulate across successive calls for a stream."""
    writer._update_timestamps(
        timestamps=np.array([10.0, 11.0], dtype=np.float64),
        stream_id=1,
        n_samples=2,
    )
    writer._update_timestamps(
        timestamps=np.array([12.0, 13.0, 14.0], dtype=np.float64),
        stream_id=1,
        n_samples=3,
    )
    assert writer._sample_count[1] == 5
    assert writer._first_timestamp[1] == 10.0  # first stays put
    assert writer._last_timestamp[1] == 14.0


# ---------------------------------------------------------------------------
# _write_samples: strictly-increasing timestamp enforcement
# ---------------------------------------------------------------------------

def test_write_samples_rewrites_non_increasing_timestamps(writer):
    """When a chunk's first timestamp is <= the previous chunk's last, the
    timestamps are rewritten forward using the chunk's own step (dt from its
    first two samples)."""
    writer._last_timestamp[1] = 50.0
    writer._write_samples(
        stream_id=1,
        timestamps=np.array([40.0, 41.0], dtype=np.float64),  # dt == 1.0
        values=np.array([[1.0], [2.0]], dtype=np.float32),
    )
    # Rewritten to 51.0, 52.0 -> last is 52.0.
    assert writer._last_timestamp[1] == 52.0


def test_write_samples_single_sample_rewrite_uses_default_step(writer):
    """A single-sample non-increasing chunk cannot derive a step,
    so it advances by the 1e-3 s default."""
    writer._last_timestamp[1] = 50.0
    writer._write_samples(
        stream_id=1,
        timestamps=np.array([10.0], dtype=np.float64),
        values=np.array([[1.0]], dtype=np.float32),
    )
    assert writer._last_timestamp[1] == pytest.approx(50.001)


def test_write_samples_keeps_increasing_timestamps(writer):
    """Timestamps already ahead of the previous last are written unchanged."""
    writer._last_timestamp[1] = 50.0
    writer._write_samples(
        stream_id=1,
        timestamps=np.array([60.0, 61.0], dtype=np.float64),
        values=np.array([[1.0], [2.0]], dtype=np.float32),
    )
    assert writer._last_timestamp[1] == 61.0


def test_write_samples_first_chunk_is_not_rewritten(writer):
    """With no previous last timestamp, the first chunk's timestamps are kept
    as-is."""
    writer._write_samples(
        stream_id=1,
        timestamps=np.array([5.0, 6.0], dtype=np.float64),
        values=np.array([[1.0], [2.0]], dtype=np.float32),
    )
    assert writer._first_timestamp[1] == 5.0
    assert writer._last_timestamp[1] == 6.0
