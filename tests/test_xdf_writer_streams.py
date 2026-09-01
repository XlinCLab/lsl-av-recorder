"""Tests for XDFWriter stream registration (recorder.xdf.xdf_writer)."""
from __future__ import annotations

import io
import xml.etree.ElementTree as ET

import pytest

from recorder.xdf.xdf_writer import XDFWriter


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
        fmt="int64",
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
# add_lsl_stream: key bookkeeping
# ---------------------------------------------------------------------------

def _add_eeg_stream(writer, key=None):
    return writer.add_lsl_stream(
        name="EEG",
        stype="EEG",
        channel_count=1,
        srate=250.0,
        fmt="float32",
        source_id="eeg",
        key=key,
    )


def test_add_lsl_stream_marks_stream_external(writer):
    """Test that a registered LSL stream is recorded as living in an external clock domain."""
    sid = _add_eeg_stream(writer)
    assert sid in writer._external_clock_streams


def test_add_lsl_stream_uses_explicit_key(writer):
    """Test that an explicit key is used verbatim as the streams-map key."""
    sid = _add_eeg_stream(writer, key="lsl:eeg")
    assert writer.streams["lsl:eeg"] == sid


def test_add_lsl_stream_default_key_includes_name_source_and_id(writer):
    """Test that with no explicit key, the default key is 'name:source_id:sid'."""
    sid = _add_eeg_stream(writer, key=None)
    assert writer.streams[f"EEG:eeg:{sid}"] == sid


def test_add_lsl_stream_dedupes_colliding_keys(writer):
    """Test that two streams sharing an explicit key don't clobber each other:
    the second is stored under a uuid-suffixed variant, and both ids
    remain distinct and retrievable."""
    sid1 = _add_eeg_stream(writer, key="lsl:eeg")
    sid2 = _add_eeg_stream(writer, key="lsl:eeg")
    assert sid1 != sid2
    assert writer.streams["lsl:eeg"] == sid1
    suffixed = [k for k in writer.streams if k.startswith("lsl:eeg:")]
    assert len(suffixed) == 1
    assert writer.streams[suffixed[0]] == sid2
