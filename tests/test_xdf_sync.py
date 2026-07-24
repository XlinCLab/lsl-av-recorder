"""
Tests for XDF stream-synchronization handling.

These verify that the XDFWriter follows the same clock-synchronization model as
LabStreamingLayer / LabRecorder:

  * Locally generated streams (audio, video) live in the recorder's own clock
    domain and get periodic zero-valued clock offsets.
  * External LSL inlets carry timestamps from a foreign clock domain; their
    offsets come from StreamInlet.time_correction() and are recorded via
    XDFWriter.record_clock_offset(). No synthetic zero offset is emitted for
    them on the sample-writing path.
  * The stored offset chunks use the LabRecorder convention
    (collection_time = now - offset, value = offset), which is what pyxdf needs
    to fold a foreign-clock stream back onto the recorder's clock.
"""
from __future__ import annotations

import numpy as np
import pytest
import pyxdf
from pylsl import cf_float32, local_clock

from recorder.lsl import lsl_inlet_recorder
from recorder.lsl.lsl_inlet_recorder import LslInletRecorder
from recorder.xdf.xdf_writer import XDFWriter


def load_by_name(path: str, **kwargs) -> dict:
    """Load an XDF file and return {stream_name: stream_dict}.

    Defaults to the *raw* view (no clock sync / dejitter) so tests can inspect
    the clock offsets and unmodified timestamps the writer actually produced.
    """
    kwargs.setdefault("synchronize_clocks", False)
    kwargs.setdefault("dejitter_timestamps", False)
    streams, _ = pyxdf.load_xdf(path, **kwargs)
    return {s["info"]["name"][0]: s for s in streams}


def add_eeg_stream(w: XDFWriter) -> int:
    return w.add_lsl_stream(
        name="EEG",
        stype="EEG",
        channel_count=1,
        srate=250.0,
        fmt="float32",
        source_id="eeg",
        key="lsl:eeg",
    )

@pytest.fixture
def xdf_path(tmp_path):
    return str(tmp_path / "test.xdf")


# ---------------------------------------------------------------------------
# Stream classification
# ---------------------------------------------------------------------------

def test_only_lsl_streams_are_marked_external(xdf_path):
    w = XDFWriter(xdf_path)
    w.start()
    audio_sid = w.add_audio_stream(
        name="Audio",
        samplerate=48000.0,
        channels=1,
    )
    video_sid = w.add_video_stream(
        name="Video",
        camera_id="0",
        video_path="v.mp4",
        width=640,
        height=480,
        fps=30.0,
    )
    eeg_sid = add_eeg_stream(w)
    w.stop()

    assert eeg_sid in w._external_clock_streams
    assert audio_sid not in w._external_clock_streams
    assert video_sid not in w._external_clock_streams


# ---------------------------------------------------------------------------
# Local streams: synthetic zero clock offsets
# ---------------------------------------------------------------------------

def test_local_audio_emits_zero_clock_offset(xdf_path):
    w = XDFWriter(xdf_path)
    w.start()
    sid = w.add_audio_stream(name="Audio", samplerate=1000.0, channels=1)
    ts = np.array([100.0, 100.001, 100.002], dtype=np.float64)
    w.write_audio(
        stream_id=sid,
        timestamps=ts,
        samples=np.array([[0.1], [0.2], [0.3]], dtype=np.float32),
    )
    w.stop()

    audio = load_by_name(xdf_path)["Audio"]
    assert audio["clock_values"] == [0.0]
    # collection_time == now - offset == first sample timestamp (offset 0)
    assert audio["clock_times"] == pytest.approx([ts[0]])


def test_local_video_emits_zero_clock_offset(xdf_path):
    w = XDFWriter(xdf_path)
    w.start()
    sid = w.add_video_stream(
        name="Video",
        camera_id="0",
        video_path="v.mp4",
        width=640,
        height=480,
        fps=30.0,
    )
    ts = np.array([50.0], dtype=np.float64)
    w.write_video_frames(
        stream_id=sid,
        timestamps=ts,
        frame_indices=np.array([0], dtype=np.int64),
    )
    w.stop()

    video = load_by_name(xdf_path)["Video"]
    assert video["clock_values"] == [0.0]
    assert video["clock_times"] == pytest.approx([ts[0]])


def test_local_offsets_are_rate_limited(xdf_path):
    """Zero offsets are emitted at most once per clock_offset_interval_s."""
    w = XDFWriter(xdf_path, clock_offset_interval_s=5.0)
    w.start()
    sid = w.add_audio_stream("Audio", samplerate=1000.0, channels=1)
    # Six writes 1 s apart -> should emit exactly two clock offsets
    for t in (100.0, 101.0, 102.0, 103.0, 104.0, 105.0):
        w.write_audio(
            stream_id=sid,
            timestamps=np.array([t], dtype=np.float64),
            samples=np.array([[0.0]], dtype=np.float32),
        )
    w.stop()

    assert load_by_name(xdf_path)["Audio"]["clock_values"] == [0.0, 0.0]


# ---------------------------------------------------------------------------
# External streams: no synthetic offsets, real measured offsets only
# ---------------------------------------------------------------------------

def test_lsl_samples_emit_no_synthetic_offset(xdf_path):
    """write_lsl_samples alone must not emit any clock offset chunk."""
    w = XDFWriter(xdf_path)
    w.start()
    sid = add_eeg_stream(w)
    w.write_lsl_samples(
        stream_id=sid,
        timestamps=np.array([1000.0, 1000.004], dtype=np.float64),
        samples=np.array([[1.0], [2.0]], dtype=np.float32),
    )
    w.stop()

    eeg = load_by_name(xdf_path)["EEG"]
    assert eeg["clock_values"] == []          # no offsets recorded
    assert eeg["time_series"].shape[0] == 2    # but the samples are there


def test_record_clock_offset_sign_and_collection_time(xdf_path):
    """collection_time == now - offset; value == offset (LabRecorder convention)."""
    w = XDFWriter(xdf_path)
    w.start()
    sid = add_eeg_stream(w)
    # Remote clock 1000 s ahead => time_correction() returns local - remote = -1000
    w.record_clock_offset(stream_id=sid, offset=-1000.0, now=10.0)
    w.record_clock_offset(stream_id=sid, offset=-1000.5, now=15.0)
    w.write_lsl_samples(
        stream_id=sid,
        timestamps=np.array([1010.0], dtype=np.float64),
        samples=np.array([[1.0]], dtype=np.float32),
    )
    w.stop()

    eeg = load_by_name(xdf_path)["EEG"]
    # clock_times are the collection instants expressed in the remote domain
    assert eeg["clock_times"] == pytest.approx([1010.0, 1015.5])
    assert eeg["clock_values"] == pytest.approx([-1000.0, -1000.5])


def test_ensure_local_clock_offset_skips_external(xdf_path):
    """Ensure external streams get no zero clock offset."""
    w = XDFWriter(xdf_path)
    w.start()
    sid = add_eeg_stream(w)
    w._ensure_local_clock_offset(
        stream_id=sid,
        timestamps=np.array([1000.0], dtype=np.float64),
    )
    w.write_lsl_samples(
        stream_id=sid,
        timestamps=np.array([1000.0], dtype=np.float64),
        samples=np.array([[1.0]], dtype=np.float32),
    )
    w.stop()

    assert load_by_name(xdf_path)["EEG"]["clock_values"] == []


# ---------------------------------------------------------------------------
# Footer contents
# ---------------------------------------------------------------------------

def test_footer_records_measured_offsets(xdf_path):
    w = XDFWriter(xdf_path)
    w.start()
    sid = add_eeg_stream(w)
    w.record_clock_offset(stream_id=sid, offset=-1000.0, now=10.0)
    w.write_lsl_samples(
        stream_id=sid,
        timestamps=np.array([1010.0], dtype=np.float64),
        samples=np.array([[1.0]], dtype=np.float32),
    )
    w.stop()

    footer = load_by_name(xdf_path)["EEG"]["footer"]["info"]
    offsets = footer["clock_offsets"][0]["offset"]
    assert [float(o["value"][0]) for o in offsets] == [-1000.0]
    assert [float(o["time"][0]) for o in offsets] == [1010.0]


# ---------------------------------------------------------------------------
# Recorder wiring (LslInletRecorder -> XDFWriter)
# 
# Test using fake LSL stream, since a live StreamInlet needs a real LSL outlet on the network, 
# so we monkeypatch it with a fake and inject a fake writer to capture the call.
# ---------------------------------------------------------------------------

class _FakeStreamInfo:
    """Minimal pylsl.StreamInfo stand-in for constructing an LslInletRecorder."""

    def channel_format(self):
        return cf_float32

    def uid(self):
        return "uid-fake"

    def name(self):
        return "EEG"


def _patch_inlet(monkeypatch, inlet):
    """Make LslInletRecorder.__init__ build `inlet` instead of a live one."""
    monkeypatch.setattr(lsl_inlet_recorder, "StreamInlet", lambda *a, **k: inlet)


def test_recorder_forwards_time_correction_unmodified(monkeypatch, xdf_path):
    offset = -1000.0  # e.g. remote clock 1000 s ahead of the recorder
    class FakeInlet:
        def time_correction(self, timeout=None):
            return offset

    w = XDFWriter(xdf_path)
    w.start()
    sid = add_eeg_stream(w)

    _patch_inlet(monkeypatch, FakeInlet())
    rec = LslInletRecorder(
        stream_info=_FakeStreamInfo(),
        stream_id=sid,
        xdf_writer=w,
    )

    before = local_clock()
    rec._record_clock_offset()
    after = local_clock()
    w.write_lsl_samples(
        stream_id=sid,
        timestamps=np.array([1010.0], dtype=np.float64),
        samples=np.array([[1.0]], dtype=np.float32),
    )
    w.stop()

    eeg = load_by_name(xdf_path)["EEG"]
    # Offset is passed through untouched (correct sign, no re-derivation)
    assert eeg["clock_values"] == pytest.approx([offset])
    # collection_time == now - offset, with `now` taken from local_clock()
    assert before - offset <= eeg["clock_times"][0] <= after - offset


def test_recorder_skips_offset_on_timeout(monkeypatch, xdf_path):
    class FakeInlet:
        def time_correction(self, timeout=None):
            raise TimeoutError("no response from stream")

    logs = []

    w = XDFWriter(xdf_path)
    w.start()
    sid = add_eeg_stream(w)

    _patch_inlet(monkeypatch, FakeInlet())
    rec = LslInletRecorder(
        stream_info=_FakeStreamInfo(),
        stream_id=sid,
        xdf_writer=w,
        status_cb=lambda msg, loglevel: logs.append((loglevel, msg)),
    )

    rec._record_clock_offset()  # must not raise error
    w.write_lsl_samples(
        stream_id=sid,
        timestamps=np.array([1010.0], dtype=np.float64),
        samples=np.array([[1.0]], dtype=np.float32),
    )
    w.stop()

    # Failed measurement records nothing
    assert load_by_name(xdf_path)["EEG"]["clock_values"] == []
    # but it is surfaced as a warning
    assert logs[0] == (
        'WARNING',
        'time_correction timed out for <EEG>; skipping this clock offset measurement'
    )

# ---------------------------------------------------------------------------
# End-to-end round trip through pyxdf (the real downstream consumer)
# ---------------------------------------------------------------------------

def test_pyxdf_synchronizes_foreign_clock_stream(xdf_path):
    """A stream from a clock D seconds off must sync back onto the local domain."""
    D = 1000.0  # remote clock runs 1000 s ahead of the recorder
    w = XDFWriter(xdf_path)
    w.start()
    audio_sid = w.add_audio_stream(
        name="Audio",
        samplerate=10.0,
        channels=1,
    )
    eeg_sid = add_eeg_stream(w)

    local_event_times = np.array([10.0, 10.1, 10.2], dtype=np.float64)
    w.write_audio(
        stream_id=audio_sid,
        timestamps=local_event_times,
        samples=np.array([[1.0], [2.0], [3.0]], dtype=np.float32),
    )

    # EEG stamped in the remote domain (= local + D); offsets = local - remote = -D.
    for now_local in (10.0, 15.0):
        w.record_clock_offset(stream_id=eeg_sid, offset=-D, now=now_local)
    w.write_lsl_samples(
        stream_id=eeg_sid,
        timestamps=local_event_times + D,
        samples=np.array([[7.0], [8.0], [9.0]], dtype=np.float32)
    )
    w.stop()

    # Load WITH clock synchronization and confirm alignment
    synced = load_by_name(xdf_path, synchronize_clocks=True)
    # After sync, EEG timestamps collapse onto the local/audio domain
    np.testing.assert_allclose(
        actual=synced["Audio"]["time_stamps"],
        desired=local_event_times,
        atol=1e-9,
    )
    np.testing.assert_allclose(
        actual=synced["EEG"]["time_stamps"],
        desired=local_event_times,
        atol=1e-6,
    )
