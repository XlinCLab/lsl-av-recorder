"""Tests for XDFWriter stream registration (recorder.xdf.xdf_writer)."""
from __future__ import annotations

import io
import xml.etree.ElementTree as ET

import numpy as np
import pytest
from pylsl import cf_string

from recorder.lsl.lsl_inlet_recorder import LslInletRecorder
from recorder.xdf.xdf_writer import VIDEO_STREAM_DTYPE, XDFWriter
from tests.shared import (MARKER_STREAM_NAME, _patch_inlet, add_eeg_stream,
                          load_by_name)


@pytest.fixture
def writer():
    """An XDFWriter writing headers into an in-memory buffer (no file opened)."""
    w = XDFWriter(path="unused.xdf")
    w.f = io.BytesIO()
    return w

# ---------------------------------------------------------------------------
# _make_stream_header_xml
# ---------------------------------------------------------------------------

def test_stream_header_xml_contains_core_fields(writer):
    """Test that XDF's header XML carries the core stream descriptors and a generated uid."""
    xml = writer._make_stream_header_xml(
        name="EEG",
        stype="EEG",
        channel_count=4,
        srate=250.0,
        fmt="float32",
        source_id="eeg",
    )
    root = ET.fromstring(xml)
    assert root.findtext("name") == "EEG"
    assert root.findtext("type") == "EEG"
    assert root.findtext("channel_count") == "4"
    assert root.findtext("nominal_srate") == "250.0"
    assert root.findtext("channel_format") == "float32"
    assert root.findtext("source_id") == "eeg"
    assert root.findtext("uid") is not None # a uid was generated


def test_stream_header_xml_includes_extra_desc(writer):
    """Extra key/values are emitted as children of a <desc> element."""
    xml = writer._make_stream_header_xml(
        name="Video",
        stype="Video",
        channel_count=1,
        srate=30.0,
        fmt=VIDEO_STREAM_DTYPE,
        source_id="camera:0",
        extra={"video_path": "cam.mp4", "width": 640},
    )
    desc = ET.fromstring(xml).find("desc")
    assert desc is not None
    assert desc.findtext("video_path") == "cam.mp4"
    assert desc.findtext("width") == "640"


def test_stream_header_xml_includes_channels_desc(writer):
    """Per-channel metadata is emitted as <desc><channels><channel>...</channel></channels></desc>,
    the layout pyxdf/MNE expect channel labels to live at."""
    xml = writer._make_stream_header_xml(
        name="EEG",
        stype="EEG",
        channel_count=2,
        srate=250.0,
        fmt="float32",
        source_id="eeg",
        channels=[
            {"label": "FPz", "unit": "microvolts", "type": "EEG"},
            {"label": "Cz"},
        ],
    )
    desc = ET.fromstring(xml).find("desc")
    assert desc is not None
    channel_els = desc.findall("channels/channel")
    assert [c.findtext("label") for c in channel_els] == ["FPz", "Cz"]
    assert channel_els[0].findtext("unit") == "microvolts"
    assert channel_els[0].findtext("type") == "EEG"


def test_stream_header_xml_combines_extra_and_channels(writer):
    """extra fields and per-channel metadata can coexist under the same <desc>."""
    xml = writer._make_stream_header_xml(
        name="EEG",
        stype="EEG",
        channel_count=1,
        srate=250.0,
        fmt="float32",
        source_id="eeg",
        extra={"hostname": "host1"},
        channels=[{"label": "FPz"}],
    )
    desc = ET.fromstring(xml).find("desc")
    assert desc.findtext("hostname") == "host1"
    assert desc.findtext("channels/channel/label") == "FPz"


def test_stream_header_xml_omits_desc_without_extra_or_channels(writer):
    """No <desc> element is written when it would otherwise be empty."""
    xml = writer._make_stream_header_xml(
        name="Audio",
        stype="Audio",
        channel_count=1,
        srate=44100.0,
        fmt="float32",
        source_id="audio",
    )
    assert ET.fromstring(xml).find("desc") is None


# ---------------------------------------------------------------------------
# per-stream formats
# ---------------------------------------------------------------------------

def test_add_audio_stream_records_its_format(writer):
    sid = writer.add_audio_stream(name="Audio", samplerate=48000.0, channels=1, fmt="int16")
    assert writer._stream_formats[sid] == "int16"


def test_add_video_stream_records_int64_format(writer):
    sid = writer.add_video_stream(
        name="Video", camera_id="0", video_path="v.mp4", width=640, height=480, fps=30.0,
    )
    assert writer._stream_formats[sid] == VIDEO_STREAM_DTYPE == "int64"


def test_add_lsl_stream_records_string_format(writer):
    sid = writer.add_lsl_stream(
        name=MARKER_STREAM_NAME,
        stype="Markers",
        channel_count=1,
        srate=0.0,
        fmt="string",
        source_id="markers",
    )
    assert writer._stream_formats[sid] == "string"


# ---------------------------------------------------------------------------
# add_lsl_stream: key bookkeeping
# ---------------------------------------------------------------------------

def test_add_lsl_stream_marks_stream_external(writer):
    """Test that a registered LSL stream is recorded as living in an external clock domain."""
    sid = add_eeg_stream(writer)
    assert sid in writer._external_clock_streams


def test_add_lsl_stream_uses_explicit_key(writer):
    """Test that an explicit key is used verbatim as the streams-map key."""
    sid = add_eeg_stream(writer, key="lsl:eeg")
    assert writer.streams["lsl:eeg"] == sid


def test_add_lsl_stream_default_key_includes_name_source_and_id(writer):
    """Test that with no explicit key, the default key is 'name:source_id:sid'."""
    sid = add_eeg_stream(writer, key=None)
    assert writer.streams[f"EEG:eeg:{sid}"] == sid


def test_add_lsl_stream_dedupes_colliding_keys(writer):
    """Test that two streams sharing an explicit key don't clobber each other:
    the second is stored under a uuid-suffixed variant, and both ids
    remain distinct and retrievable."""
    sid1 = add_eeg_stream(writer, key="lsl:eeg")
    sid2 = add_eeg_stream(writer, key="lsl:eeg")
    assert sid1 != sid2
    assert writer.streams["lsl:eeg"] == sid1
    suffixed = [k for k in writer.streams if k.startswith("lsl:eeg:")]
    assert len(suffixed) == 1
    assert writer.streams[suffixed[0]] == sid2


# ---------------------------------------------------------------------------
# String-format (marker/trigger) streams
# ---------------------------------------------------------------------------

class _FakeStringStreamInfo:
    """Minimal pylsl.StreamInfo stand-in for a cf_string marker/trigger stream."""

    def channel_format(self):
        return cf_string

    def uid(self):
        return "uid-markers"

    def name(self):
        return "Fake Marker Stream"


def add_marker_stream(w: XDFWriter, channel_count: int = 1) -> int:
    return w.add_lsl_stream(
        name=MARKER_STREAM_NAME,
        stype="Markers",
        channel_count=channel_count,
        srate=0.0,  # irregular rate, as marker streams declare
        fmt="string",
        source_id="markers",
        key="lsl:markers",
    )


def test_recorder_has_no_dtype_for_string_stream(monkeypatch, xdf_path):
    """LslInletRecorder resolves cf_string to xdf_format="string" / dtype=None,
    which is what tells _process_chunk to skip the numpy cast."""
    w = XDFWriter(xdf_path)
    w.start()
    sid = add_marker_stream(w)

    _patch_inlet(monkeypatch, object())
    rec = LslInletRecorder(
        stream_info=_FakeStringStreamInfo(),
        stream_id=sid,
        xdf_writer=w,
    )
    w.stop()

    assert rec.xdf_format == "string"
    assert rec.dtype is None


def test_recorder_writes_string_samples_via_process_chunk(monkeypatch, xdf_path):
    """A pulled chunk of marker strings is passed straight through to XDFWriter
    (no numpy cast), and comes back out through pyxdf unchanged."""
    w = XDFWriter(xdf_path)
    w.start()
    sid = add_marker_stream(w)

    _patch_inlet(monkeypatch, object())  # inlet.pull_chunk() isn't used by this test
    rec = LslInletRecorder(
        stream_info=_FakeStringStreamInfo(),
        stream_id=sid,
        xdf_writer=w,
    )

    # Mirrors what StreamInlet.pull_chunk() hands back for a cf_string inlet:
    # a list of samples, each itself a list of decoded str (one per channel)
    samples = [["Stimulus/S1"], ["Stimulus/S2"], ["Response/R1"]]
    timestamps = [10.0, 10.5, 11.2]
    rec._process_chunk(
        samples=samples,
        timestamps=timestamps,
    )
    w.stop()

    markers = load_by_name(xdf_path)[MARKER_STREAM_NAME]
    assert markers["time_series"] == samples
    np.testing.assert_allclose(markers["time_stamps"], timestamps)


def test_recorder_ignores_empty_chunk(monkeypatch, xdf_path):
    """An empty pull (no samples available this cycle) writes nothing;
    mirrors numeric-stream behavior of skipping writes when pull_chunk times out."""
    w = XDFWriter(xdf_path)
    w.start()
    sid = add_marker_stream(w)

    _patch_inlet(monkeypatch, object())
    rec = LslInletRecorder(
        stream_info=_FakeStringStreamInfo(),
        stream_id=sid,
        xdf_writer=w,
    )
    rec._process_chunk(samples=[], timestamps=[])
    w.stop()

    assert load_by_name(xdf_path)[MARKER_STREAM_NAME]["time_series"] == []


def test_pyxdf_reads_back_multichannel_string_samples(xdf_path):
    """Test that multi-channel string streams (e.g. a Type/Description marker pair)
    round trip correctly."""
    w = XDFWriter(xdf_path)
    w.start()
    sid = add_marker_stream(w, channel_count=2)
    samples = [["Stimulus", "S1"], ["Response", "R1"]]
    w.write_lsl_samples(
        stream_id=sid,
        timestamps=np.array([1.0, 2.0], dtype=np.float64),
        samples=samples,
    )
    w.stop()

    markers = load_by_name(xdf_path)[MARKER_STREAM_NAME]
    assert markers["time_series"] == samples
