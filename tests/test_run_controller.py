"""Tests for recorder.gui.run_controller.RunController."""
from __future__ import annotations

import os

import numpy as np
import pytest
from pylsl import cf_string

from recorder.config import AppConfig, VideoCamConfig
from recorder.gui.run_controller import RunController
from recorder.xdf.xdf_writer import (FULL_BUFFER_DEFAULT_POLICY,
                                     FULL_BUFFER_DROP_NEWEST_POLICY,
                                     FULL_BUFFER_DROP_OLDEST_POLICY)
from tests.shared import MARKER_STREAM_NAME


def _cfg(study_dir, template="rec", **buffering):
    """Build an AppConfig rooted at `study_dir` with buffering overrides."""
    cfg = AppConfig()
    cfg.Output.StudyRoot = str(study_dir)
    cfg.Output.PathTemplate = template
    for key, value in buffering.items():
        setattr(cfg.Buffering, key, value)
    return cfg


@pytest.fixture
def make_rc(tmp_path):
    """Factory building a RunController under a fresh temp StudyRoot per call.

    Returns (rc, logs) where logs is a list of (loglevel, message) tuples.
    """
    counter = {"n": 0}

    def _make(*, template="rec", **buffering):
        counter["n"] += 1
        study = tmp_path / f"study{counter['n']}"
        logs: list[tuple[str, str]] = []
        rc = RunController(
            cfg=_cfg(study, template=template, **buffering),
            status_cb=lambda msg, loglevel: logs.append((loglevel, msg)),
        )
        return rc, logs

    return _make


def _queued_descs(rc) -> list[str]:
    """Drain the writer queue and return the descriptions of the tasks it held."""
    descs = []
    while not rc._writer_queue.empty():
        _fn, desc = rc._writer_queue.get_nowait()
        descs.append(desc)
    return descs


# ---------------------------------------------------------------------------
# Construction / config normalization
# ---------------------------------------------------------------------------

def test_invalid_drop_policy_falls_back_and_warns(make_rc):
    """An unrecognized WriterDropPolicy falls back to default and emits warning."""
    rc, logs = make_rc(WriterDropPolicy="nonsense")
    warnings = [msg for level, msg in logs if level == "WARNING"]
    assert rc._writer_drop_policy == FULL_BUFFER_DEFAULT_POLICY
    assert len(warnings) > 0
    assert any("Invalid writer drop policy" in msg for msg in warnings)


def test_writer_queue_size_is_at_least_one(make_rc):
    """A WriterQueueSize of 0 is clamped up to 1 so the queue is always usable."""
    rc, _ = make_rc(WriterQueueSize=0)
    assert rc._writer_queue_size == 1
    assert rc._writer_queue.maxsize == 1


# ---------------------------------------------------------------------------
# _setup_paths: runNN subdirectory
# ---------------------------------------------------------------------------

def test_setup_paths_uses_base_dir_when_empty(tmp_path):
    """With an empty output directory, the run writes straight into base_dir
    (no runNN subdirectory)."""
    study = tmp_path / "empty_study"
    rc = RunController(cfg=_cfg(study), status_cb=None)
    assert os.path.normpath(rc.outdir) == os.path.normpath(str(study))
    assert "run" not in os.path.basename(os.path.normpath(rc.outdir))


def test_setup_paths_creates_run_subdir_when_nonempty(tmp_path):
    """If the output directory already has content, a runNN subdirectory is
    created so a prior recording isn't overwritten."""
    study = tmp_path / "used_study"
    study.mkdir()
    (study / "leftover.txt").write_text("prior run", encoding="utf-8")
    rc = RunController(cfg=_cfg(study), status_cb=None)
    assert rc.outdir == str(study / "run02")
    assert (study / "run02").is_dir()


# ---------------------------------------------------------------------------
# _enqueue_write: drop policies
# ---------------------------------------------------------------------------

def test_enqueue_drop_newest_keeps_queue_and_drops_incoming(make_rc):
    """Under drop_newest, a full queue keeps its existing tasks and discards the
    incoming one, counting it as a 'newest' drop."""
    rc, _ = make_rc(WriterDropPolicy=FULL_BUFFER_DROP_NEWEST_POLICY, WriterQueueSize=2)
    rc._writer_thread = object()  # truthy sentinel: use the queue, not the fallback
    for desc in ("a", "b", "c"):
        rc._enqueue_write(fn=lambda: None, desc=desc)
    assert _queued_descs(rc) == ["a", "b"]
    assert rc._newest_drop_count == 1
    assert rc._writer_drop_count == 1


def test_enqueue_drop_oldest_evicts_and_enqueues_newest(make_rc):
    """Under drop_oldest, a full queue evicts the oldest task and appends the
    incoming one, counting an 'oldest' drop."""
    rc, _ = make_rc(WriterDropPolicy=FULL_BUFFER_DROP_OLDEST_POLICY, WriterQueueSize=2)
    rc._writer_thread = object()
    for desc in ("a", "b", "c"):
        rc._enqueue_write(fn=lambda: None, desc=desc)
    assert _queued_descs(rc) == ["b", "c"]
    assert rc._oldest_drop_count == 1
    assert rc._writer_drop_count == 1


def test_enqueue_no_writer_thread_writes_immediately(make_rc):
    """With no writer thread, an enqueue falls back to writing immediately when
    the XDF writer is active."""
    rc, _ = make_rc()
    rc._writer_thread = None
    rc.xdf = type("FakeXDF", (), {"_started": True})()
    ran = []
    rc._enqueue_write(fn=lambda: ran.append(True), desc="x")
    assert ran == [True]


def test_enqueue_no_writer_thread_no_xdf_is_noop(make_rc):
    """With no writer thread and no active XDF writer, the task is silently
    dropped rather than executed or raised."""
    rc, _ = make_rc()
    rc._writer_thread = None
    rc.xdf = None
    ran = []
    rc._enqueue_write(fn=lambda: ran.append(True), desc="x")
    assert ran == []


# ---------------------------------------------------------------------------
# Drop-count logging throttle
# ---------------------------------------------------------------------------

def test_drop_logging_is_throttled(make_rc):
    """Drop logging fires on the 1st drop and then every 100th, not on every
    drop (so a flood of drops doesn't flood the log)."""
    rc, logs = make_rc(WriterDropPolicy=FULL_BUFFER_DROP_NEWEST_POLICY)
    for _ in range(100):
        rc._increment_drop_counts(newest=1)
    warnings = [msg for level, msg in logs if level == "WARNING"]
    assert len(warnings) == 2  # at count == 1 and count == 100
    assert "Dropped 100 tasks" in warnings[-1]
    assert "newest: 100" in warnings[-1]


# ---------------------------------------------------------------------------
# Audio buffering
# ---------------------------------------------------------------------------

def test_buffer_audio_accumulates_until_target(make_rc):
    """Audio chunks are buffered and only released once the sample target is
    reached, at which point the concatenated batch is returned and the buffer
    resets."""
    rc, _ = make_rc()
    rc._audio_buffer_target = 5
    assert rc._buffer_audio_samples(
        timestamps=np.arange(2.0),
        samples=np.zeros((2, 1), dtype=np.float32),
    ) is None
    assert rc._buffer_audio_samples(
        timestamps=np.arange(2.0),
        samples=np.zeros((2, 1), dtype=np.float32),
    ) is None
    batch = rc._buffer_audio_samples(
        timestamps=np.arange(2.0),
        samples=np.zeros((2, 1), dtype=np.float32),
    )
    assert batch is not None
    ts, samples = batch
    assert ts.shape[0] == 6  # 2 + 2 + 2
    assert samples.shape == (6, 1)
    assert rc._audio_buf_n == 0  # buffer reset after release


def test_flush_audio_buffer_emits_remainder(make_rc, monkeypatch):
    """Flushing emits the buffered remainder as a single blocking write and
    clears the buffer."""
    rc, _ = make_rc()
    rc._audio_buffer_target = 100
    rc.xdf = object()
    rc.audio_sid = 1
    rc._buffer_audio_samples(
        timestamps=np.arange(3.0),
        samples=np.zeros((3, 1), dtype=np.float32),
    )
    calls = []
    monkeypatch.setattr(
        rc, "_enqueue_write",
        lambda fn, desc, block=False: calls.append((desc, block)),
    )
    rc._flush_audio_buffer()
    assert len(calls) == 1
    assert calls[0][1] is True  # blocking flush
    assert rc._audio_buf_n == 0


def test_flush_audio_buffer_noop_when_empty(make_rc, monkeypatch):
    """Flushing an empty audio buffer enqueues nothing."""
    rc, _ = make_rc()
    rc._audio_buffer_target = 100
    rc.xdf = object()
    rc.audio_sid = 1
    calls = []
    monkeypatch.setattr(rc, "_enqueue_write", lambda *a, **k: calls.append(True))
    rc._flush_audio_buffer()
    assert calls == []


# ---------------------------------------------------------------------------
# Video buffering
# ---------------------------------------------------------------------------

def test_buffer_video_accumulates_until_target(make_rc):
    """Video frames are buffered per label and released as numpy arrays once the
    frame target is reached, resetting that label's buffer."""
    rc, _ = make_rc()
    rc.video_buffer_frames = 3
    assert rc._buffer_video_frames(label="Face", timestamp=1.0, frame_index=0) is None
    assert rc._buffer_video_frames(label="Face", timestamp=2.0, frame_index=1) is None
    batch = rc._buffer_video_frames(label="Face", timestamp=3.0, frame_index=2)
    assert batch is not None
    ts, idx = batch
    assert list(ts) == [1.0, 2.0, 3.0]
    assert list(idx) == [0, 1, 2]
    assert rc._video_ts_buf["Face"] == []


def test_buffer_video_is_per_label(make_rc):
    """Frames for different camera labels accumulate independently."""
    rc, _ = make_rc()
    rc.video_buffer_frames = 3
    rc._buffer_video_frames(label="Face", timestamp=1.0, frame_index=0)
    rc._buffer_video_frames(label="Hand", timestamp=1.0, frame_index=0)
    assert rc._video_ts_buf["Face"] == [1.0]
    assert rc._video_ts_buf["Hand"] == [1.0]


# ---------------------------------------------------------------------------
# Config-derived helpers
# ---------------------------------------------------------------------------

def test_audio_stream_settings_numeric_device_index(tmp_path):
    """A numeric Audio.Device string is coerced to an int device index."""
    cfg = _cfg(tmp_path / "s")
    cfg.Audio.Device = "2"
    rc = RunController(cfg=cfg, status_cb=None)
    aset = rc._get_audio_stream_settings()
    assert aset.device == 2


def test_audio_stream_settings_named_device_kept_as_string(tmp_path):
    """A non-numeric Audio.Device is kept as a string and echoed in source_id."""
    cfg = _cfg(tmp_path / "s")
    cfg.Audio.Device = "USB Mic"
    rc = RunController(cfg=cfg, status_cb=None)
    aset = rc._get_audio_stream_settings()
    assert aset.device == "USB Mic"
    assert aset.source_id == "audio:USB Mic"


def test_audio_stream_settings_default_device(tmp_path):
    """With no Audio.Device, the device is None and source_id notes 'default'."""
    cfg = _cfg(tmp_path / "s")
    cfg.Audio.Device = None
    rc = RunController(cfg=cfg, status_cb=None)
    aset = rc._get_audio_stream_settings()
    assert aset.device is None
    assert aset.source_id == "audio:default"


def test_get_active_cams_filters_disabled(tmp_path):
    """Only enabled cameras are returned as active."""
    cfg = _cfg(tmp_path / "s")
    cfg.Video.Enabled = True
    cfg.Video.Cams = [
        VideoCamConfig(Enabled=True, Label="Face", FPS=30),
        VideoCamConfig(Enabled=False, Label="Off", FPS=30),
        VideoCamConfig(Enabled=True, Label="Hand", FPS=30),
    ]
    rc = RunController(cfg=cfg, status_cb=None)
    assert [c.Label for c in rc.cams] == ["Face", "Hand"]


def test_get_active_cams_empty_when_video_disabled(tmp_path):
    """No active cameras are returned when video is disabled, even if cams exist."""
    cfg = _cfg(tmp_path / "s")
    cfg.Video.Enabled = False
    cfg.Video.Cams = [VideoCamConfig(Enabled=True, Label="Face", FPS=30)]
    rc = RunController(cfg=cfg, status_cb=None)
    assert rc.cams == []


def test_get_active_cams_warns_on_invalid_settings(tmp_path):
    """A camera with an invalid FPS/Width/Height is warned about (by Label) and
    still returned, rather than crashing construction."""
    cfg = _cfg(tmp_path / "s")
    cfg.Video.Enabled = True
    cfg.Video.Cams = [
        VideoCamConfig(
            Enabled=True,
            Label="Face",
            FPS=0,
            Width=0,
            Height=0,
        )
    ]
    logs: list[tuple[str, str]] = []
    rc = RunController(
        cfg=cfg,
        status_cb=lambda msg, level: logs.append((level, msg))
    )
    assert [c.Label for c in rc.cams] == ["Face"]
    warnings = [msg for level, msg in logs if level == "WARNING"]
    assert any("Camera Face sampling rate is 0" in msg for msg in warnings)
    assert any("Camera Face width is 0" in msg for msg in warnings)
    assert any("Camera Face height is 0" in msg for msg in warnings)


def test_video_output_path_uses_label_and_container(tmp_path):
    """The per-camera video path combines the run base name, camera label, and
    configured container extension, under the run's output directory."""
    cfg = _cfg(tmp_path / "s")
    cfg.Video.Container = "mkv"
    rc = RunController(cfg=cfg, status_cb=None)
    cam = VideoCamConfig(Enabled=True, Label="Face", FPS=30)
    path = rc._get_video_output_path(cam)
    assert path.endswith(f"{rc.base_name}_cam-Face.mkv")
    assert path.startswith(rc.outdir)


# ---------------------------------------------------------------------------
# _lsl_stream_meta
# ---------------------------------------------------------------------------

def test_lsl_stream_meta_collects_fields_and_skips_failures(tmp_path):
    """Stream metadata is collected field-by-field, and a field whose getter
    raises is simply omitted rather than aborting the whole dict."""
    rc = RunController(cfg=_cfg(tmp_path / "s"), status_cb=None)

    class FakeStreamInfo:
        def name(self): return "EEG"
        def type(self): return "EEG"
        def channel_count(self): return 4
        def nominal_srate(self): return 250.0
        def source_id(self): return "eeg"
        def uid(self): return "uid-1"
        def hostname(self): raise RuntimeError("unavailable")

    meta = rc._lsl_stream_meta(FakeStreamInfo())
    assert meta == {
        "name": "EEG",
        "type": "EEG",
        "channel_count": 4,
        "nominal_srate": 250.0,
        "source_id": "eeg",
        "uid": "uid-1",
    }
    assert "hostname" not in meta  # getter raised -> field skipped


# ---------------------------------------------------------------------------
# _add_streams_to_xdf_writer: string-format (marker/trigger) streams
# ---------------------------------------------------------------------------

def test_add_streams_to_xdf_writer_registers_string_format_stream(tmp_path, monkeypatch):
    """A cf_string marker stream gets registered correctly in the XDF writer."""
    class FakeMarkerStream:
        def name(self): return MARKER_STREAM_NAME
        def type(self): return "Markers"
        def channel_count(self): return 1
        def nominal_srate(self): return 0.0
        def channel_format(self): return cf_string
        def source_id(self): return "markers"
        def uid(self): return "uid-markers"
        def hostname(self): return "host1"

    class FailingInlet:
        def __init__(self, *a, **k):
            raise TimeoutError("no network stream to probe in this test")

    monkeypatch.setattr("recorder.gui.run_controller.StreamInlet", FailingInlet)

    logs: list[tuple[str, str]] = []
    rc = RunController(
        cfg=_cfg(tmp_path / "s"),
        status_cb=lambda msg, loglevel: logs.append((loglevel, msg)),
        lsl_streams=[FakeMarkerStream()],
    )
    xdf_writer = rc._initialize_xdf_writer(xdf_path=rc._get_xdf_path())
    xdf_writer.start()
    rc._add_streams_to_xdf_writer(xdf_writer)
    xdf_writer.stop()

    assert "lsl:uid-markers" in xdf_writer.streams
    assert xdf_writer._stream_formats[xdf_writer.streams["lsl:uid-markers"]] == "string"
    assert not any("Skipping LSL stream" in msg for _, msg in logs)
