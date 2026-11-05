"""Tests for recorder.xdf.xdf_validation (the "Test recording settings" check).

Builds small XDF files directly with XDFWriter (mirroring what RunController
would produce for a short test recording) and checks that validate_test_recording
catches both the happy path and each of the ways a real recording can go wrong:
a stream never showing up, wrong channel count, an implausible sample count for
the recording's duration, a missing video file, or a stream that never answered
a time_correction() ping.
"""
from __future__ import annotations

import numpy as np
from pylsl import StreamInfo

from recorder.audio.channel_labels import resolve_channel_labels
from recorder.config import AppConfig, VideoCamConfig
from recorder.video.constants import DEFAULT_HEIGHT, DEFAULT_WIDTH
from recorder.xdf.xdf_validation import validate_test_recording
from recorder.xdf.xdf_writer import XDFWriter
from tests.shared import MARKER_STREAM_NAME


def write_audio_stream(
        writer: XDFWriter,
        name: str,
        samplerate: float,
        channels: int,
        n_samples: int,
    ) -> int:
    sid = writer.add_audio_stream(
        name=name,
        samplerate=samplerate,
        channels=channels,
        channel_labels=resolve_channel_labels([], channels),
    )
    ts = 100.0 + np.arange(n_samples) / samplerate
    samples = np.zeros((n_samples, channels), dtype=np.float32)
    writer.write_audio(
        stream_id=sid,
        timestamps=ts,
        samples=samples,
    )
    return sid


def write_video_stream(
        writer: XDFWriter,
        name: str,
        fps: float,
        n_frames: int,
        video_path: str,
        pixel_format: str | None = None,
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
    ) -> int:
    sid = writer.add_video_stream(
        name=name,
        camera_id="0",
        video_path=video_path,
        width=width,
        height=height,
        fps=fps,
        pixel_format=pixel_format,
    )
    ts = 100.0 + np.arange(n_frames) / fps
    idx = np.arange(n_frames)
    writer.write_video_frames(
        stream_id=sid,
        timestamps=ts,
        frame_indices=idx,
    )
    return sid


def write_lsl_stream(
    writer: XDFWriter,
    name: str,
    srate: float,
    channels: int,
    n_samples: int,
    with_clock_offset: bool = True,
) -> int:
    sid = writer.add_lsl_stream(
        name=name, stype="EEG", channel_count=channels, srate=srate, fmt="float32",
        source_id="src", key=f"lsl:{name}",
    )
    ts = 100.0 + np.arange(n_samples) / srate
    samples = np.zeros((n_samples, channels), dtype=np.float32)
    writer.write_lsl_samples(sid, ts, samples)
    if with_clock_offset:
        writer.record_clock_offset(sid, offset=0.0, now=ts[-1])
    return sid


def make_eeg_stream_info(name: str, channels: int, srate: float) -> StreamInfo:
    return StreamInfo(
        name=name,
        type="EEG",
        channel_count=channels,
        nominal_srate=srate,
        channel_format="float32",
        source_id=f"src-{name}",
    )


def base_cfg() -> AppConfig:
    cfg = AppConfig()
    cfg.Audio.Enabled = False
    cfg.Video.Enabled = False
    return cfg


# ---------------------------------------------------------------------------
# File-level failure
# ---------------------------------------------------------------------------

def test_missing_file_fails_immediately(tmp_path):
    report = validate_test_recording(
        xdf_path=str(tmp_path / "does_not_exist.xdf"),
        cfg=base_cfg(),
        lsl_streams=[],
        expected_duration_s=10.0,
    )
    assert not report.passed
    assert len(report.checks) == 1
    assert not report.checks[0].passed
    assert "XDF file loads" in report.checks[0].name


def test_nothing_configured_fails(xdf_path):
    w = XDFWriter(xdf_path)
    w.start()
    w.stop()
    report = validate_test_recording(
        xdf_path=xdf_path,
        cfg=base_cfg(),
        lsl_streams=[],
        expected_duration_s=10.0,
    )
    assert not report.passed
    assert any("At least one stream configured" in c.name for c in report.checks)


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

def test_audio_pass(xdf_path):
    duration = 10.0
    cfg = base_cfg()
    cfg.Audio.Enabled = True
    cfg.Audio.StreamName = "Audio"
    cfg.Audio.SampleRate = 1000
    cfg.Audio.Channels = 2

    w = XDFWriter(xdf_path)
    w.start()
    write_audio_stream(
        writer=w,
        name="Audio",
        samplerate=1000,
        channels=2,
        n_samples=int(duration * 1000),
    )
    w.stop()

    report = validate_test_recording(
        xdf_path=xdf_path,
        cfg=cfg,
        lsl_streams=[], # no LSL streams (audio only, not streamed via LSL)
        expected_duration_s=duration,
    )
    assert report.passed, report.detailed_text()


def test_audio_sample_format_mismatch_fails(xdf_path):
    """A stream stored in a different dtype than configured is flagged."""
    cfg = base_cfg()
    cfg.Audio.Enabled = True
    cfg.Audio.StreamName = "Audio"
    cfg.Audio.SampleRate = 1000
    cfg.Audio.Channels = 1
    cfg.Audio.SampleFormat = "int16"

    w = XDFWriter(xdf_path)
    w.start()
    write_audio_stream(w, name="Audio", samplerate=1000, channels=1, n_samples=10000)  # float32
    w.stop()

    report = validate_test_recording(xdf_path, cfg, [], 10.0)
    fmt_check = next(c for c in report.checks if "sample format" in c.name)
    assert not fmt_check.passed
    assert "expected=int16, actual=float32" in fmt_check.detail


def test_audio_missing_stream_fails(xdf_path):
    cfg = base_cfg()
    cfg.Audio.Enabled = True
    cfg.Audio.StreamName = "Audio"

    w = XDFWriter(xdf_path)
    w.start()
    w.stop()

    report = validate_test_recording(
        xdf_path=xdf_path,
        cfg=cfg,
        lsl_streams=[],
        expected_duration_s=10.0,
    )
    assert not report.passed
    assert any("Audio stream <Audio> present" in c.name and not c.passed for c in report.checks)


def test_audio_wrong_channel_count_fails(xdf_path):
    duration = 10.0
    cfg = base_cfg()
    cfg.Audio.Enabled = True
    cfg.Audio.StreamName = "Audio"
    cfg.Audio.SampleRate = 1000
    cfg.Audio.Channels = 1  # configured for mono

    w = XDFWriter(xdf_path)
    w.start()
    write_audio_stream(
        writer=w,
        name="Audio",
        samplerate=1000,
        channels=2,  # recorded stereo
        n_samples=int(duration * 1000)
    )
    w.stop()

    report = validate_test_recording(
        xdf_path=xdf_path,
        cfg=cfg,
        lsl_streams=[],
        expected_duration_s=duration,
    )
    assert not report.passed
    channel_check = next(c for c in report.checks if "channel count" in c.name)
    assert not channel_check.passed


def test_audio_implausible_sample_count_fails(xdf_path):
    duration = 10.0
    cfg = base_cfg()
    cfg.Audio.Enabled = True
    cfg.Audio.StreamName = "Audio"
    cfg.Audio.SampleRate = 1000
    cfg.Audio.Channels = 1

    w = XDFWriter(xdf_path)
    w.start()
    # Only a fraction of the samples a healthy 10s/1kHz capture should produce
    write_audio_stream(
        writer=w,
        name="Audio",
        samplerate=1000,
        channels=1,
        n_samples=100,
    )
    w.stop()

    report = validate_test_recording(
        xdf_path=xdf_path,
        cfg=cfg,
        lsl_streams=[],
        expected_duration_s=duration,
    )
    assert not report.passed
    count_check = next(c for c in report.checks if "sample count" in c.name)
    assert not count_check.passed


# ---------------------------------------------------------------------------
# Video
# ---------------------------------------------------------------------------

def test_video_pass(xdf_path, tmp_path):
    duration = 10.0
    fps = 30.0
    video_path = str(tmp_path / "cam.mp4")
    with open(video_path, "wb") as f:
        f.write(b"\x00" * 1024)

    cfg = base_cfg()
    cfg.Video.Enabled = True
    cfg.Video.Cams = [VideoCamConfig(Enabled=True, Label="Face", FPS=int(fps))]

    w = XDFWriter(xdf_path)
    w.start()
    write_video_stream(
        writer=w,
        name="Camera-Face",
        fps=fps,
        n_frames=int(duration * fps),
        video_path=video_path,
    )
    w.stop()

    report = validate_test_recording(
        xdf_path=xdf_path,
        cfg=cfg,
        lsl_streams=[],
        expected_duration_s=duration,
    )
    assert report.passed, report.detailed_text()
    assert not any("pixel format" in c.name for c in report.checks)


def test_video_pixel_format_pass(xdf_path, tmp_path):
    """The live-verified pixel format (recorded into the stream's desc)
    matching what was configured is a PASS, and produces its own check."""
    duration = 10.0
    fps = 30.0
    video_path = str(tmp_path / "cam.mp4")
    with open(video_path, "wb") as f:
        f.write(b"\x00" * 1024)

    cfg = base_cfg()
    cfg.Video.Enabled = True
    pixel_format = "NV12"
    cfg.Video.Cams = [
        VideoCamConfig(
            Enabled=True,
            Label="Face",
            FPS=int(fps),
            PixelFormat=pixel_format,
        )
    ]

    w = XDFWriter(xdf_path)
    w.start()
    write_video_stream(
        writer=w,
        name="Camera-Face",
        fps=fps,
        n_frames=int(duration * fps),
        video_path=video_path,
        pixel_format=pixel_format,
    )
    w.stop()

    report = validate_test_recording(
        xdf_path=xdf_path,
        cfg=cfg,
        lsl_streams=[],
        expected_duration_s=duration,
    )
    assert report.passed, report.detailed_text()
    pf_check = next(c for c in report.checks if "pixel format" in c.name)
    assert pf_check.passed


def test_video_pixel_format_mismatch_fails(xdf_path, tmp_path):
    """Check that validation fails when the platform capture backend
    actually negotiated a different pixel format than what was configured."""
    duration = 10.0
    fps = 30.0
    video_path = str(tmp_path / "cam.mp4")
    with open(video_path, "wb") as f:
        f.write(b"\x00" * 1024)

    cfg = base_cfg()
    cfg.Video.Enabled = True
    cfg.Video.Cams = [
        VideoCamConfig(
            Enabled=True,
            Label="Face",
            FPS=int(fps),
            PixelFormat="NV12",
        )
    ]

    w = XDFWriter(xdf_path)
    w.start()
    write_video_stream(
        writer=w,
        name="Camera-Face",
        fps=fps,
        n_frames=int(duration * fps),
        video_path=video_path,
        pixel_format="YUYV",  # a different pixel format was actually negotiated
    )
    w.stop()

    report = validate_test_recording(
        xdf_path=xdf_path,
        cfg=cfg,
        lsl_streams=[],
        expected_duration_s=duration,
    )
    assert not report.passed
    pf_check = next(c for c in report.checks if "pixel format" in c.name)
    assert not pf_check.passed
    assert "NV12" in pf_check.detail and "YUYV" in pf_check.detail


def test_video_pixel_format_skipped_when_unknown(xdf_path, tmp_path):
    """When the platform couldn't determine the actual pixel format
    (e.g. readback unsupported), there is no basis to judge a mismatch,
    so the check is skipped entirely rather than failed."""
    duration = 10.0
    fps = 30.0
    video_path = str(tmp_path / "cam.mp4")
    with open(video_path, "wb") as f:
        f.write(b"\x00" * 1024)

    cfg = base_cfg()
    cfg.Video.Enabled = True
    cfg.Video.Cams = [
        VideoCamConfig(
            Enabled=True,
            Label="Face",
            FPS=int(fps),
            PixelFormat="NV12",
        )
    ]

    w = XDFWriter(xdf_path)
    w.start()
    write_video_stream(
        writer=w,
        name="Camera-Face",
        fps=fps,
        n_frames=int(duration * fps),
        video_path=video_path,
        pixel_format=None,
    )
    w.stop()

    report = validate_test_recording(
        xdf_path=xdf_path,
        cfg=cfg,
        lsl_streams=[],
        expected_duration_s=duration,
    )
    assert report.passed, report.detailed_text()
    assert not any("pixel format" in c.name for c in report.checks)


def test_video_frame_size_pass(xdf_path, tmp_path):
    """The frame size actually written into the XDF (RunController records
    VideoRecorder.writer_size here, not the configured Width/Height) matching
    what was configured is a PASS, and produces its own check."""
    duration = 10.0
    fps = 30.0
    video_path = str(tmp_path / "cam.mp4")
    with open(video_path, "wb") as f:
        f.write(b"\x00" * 1024)

    cfg = base_cfg()
    cfg.Video.Enabled = True
    cfg.Video.Cams = [
        VideoCamConfig(
            Enabled=True,
            Label="Face",
            FPS=int(fps),
            Width=1280,
            Height=720,
        )
    ]

    w = XDFWriter(xdf_path)
    w.start()
    write_video_stream(
        writer=w,
        name="Camera-Face",
        fps=fps,
        n_frames=int(duration * fps),
        video_path=video_path,
        width=1280,
        height=720,
    )
    w.stop()

    report = validate_test_recording(
        xdf_path=xdf_path,
        cfg=cfg,
        lsl_streams=[],
        expected_duration_s=duration,
    )
    assert report.passed, report.detailed_text()
    size_check = next(c for c in report.checks if "frame size" in c.name)
    assert size_check.passed


def test_video_frame_size_mismatch_fails(xdf_path, tmp_path):
    """Check that validation fails when the frame size actually written into
    the XDF differs from what was configured."""
    duration = 10.0
    fps = 30.0
    video_path = str(tmp_path / "cam.mp4")
    with open(video_path, "wb") as f:
        f.write(b"\x00" * 1024)

    cfg = base_cfg()
    cfg.Video.Enabled = True
    cfg.Video.Cams = [
        VideoCamConfig(
            Enabled=True,
            Label="Face",
            FPS=int(fps),
            Width=1080,
            Height=1920,
        )
    ]

    w = XDFWriter(xdf_path)
    w.start()
    write_video_stream(
        writer=w,
        name="Camera-Face",
        fps=fps,
        n_frames=int(duration * fps),
        video_path=video_path,
        width=1552,  # a different size was actually delivered/written
        height=1552,
    )
    w.stop()

    report = validate_test_recording(
        xdf_path=xdf_path,
        cfg=cfg,
        lsl_streams=[],
        expected_duration_s=duration,
    )
    assert not report.passed
    size_check = next(c for c in report.checks if "frame size" in c.name)
    assert not size_check.passed
    assert "1080x1920" in size_check.detail and "1552x1552" in size_check.detail


def test_video_missing_file_fails(xdf_path, tmp_path):
    duration = 10.0
    fps = 30.0
    video_path = str(tmp_path / "cam_never_written.mp4")  # some file that was never created

    cfg = base_cfg()
    cfg.Video.Enabled = True
    cfg.Video.Cams = [VideoCamConfig(Enabled=True, Label="Face", FPS=int(fps))]

    w = XDFWriter(xdf_path)
    w.start()
    write_video_stream(
        writer=w,
        name="Camera-Face",
        fps=fps,
        n_frames=int(duration * fps),
        video_path=video_path,
    )
    w.stop()

    report = validate_test_recording(
        xdf_path=xdf_path,
        cfg=cfg,
        lsl_streams=[],
        expected_duration_s=duration,
    )
    assert not report.passed
    file_check = next(c for c in report.checks if "file written" in c.name)
    assert not file_check.passed


def test_video_disabled_cam_is_skipped(xdf_path):
    """A cam present in cfg but not Enabled shouldn't be checked at all."""
    cfg = base_cfg()
    cfg.Video.Enabled = True
    cfg.Video.Cams = [VideoCamConfig(Enabled=False, Label="Unused", FPS=30)]

    w = XDFWriter(xdf_path)
    w.start()
    w.stop()

    report = validate_test_recording(
        xdf_path=xdf_path,
        cfg=cfg,
        lsl_streams=[],
        expected_duration_s=10.0,
    )
    # No video checks emitted for the disabled cam, and nothing else configured
    assert not any("Unused" in c.name for c in report.checks)
    assert any("At least one stream configured" in c.name for c in report.checks)


# ---------------------------------------------------------------------------
# LSL
# ---------------------------------------------------------------------------

def test_lsl_pass(xdf_path):
    duration = 10.0
    srate = 250.0
    stream_info = make_eeg_stream_info(
        name="EEG",
        channels=4,
        srate=srate,
    )

    w = XDFWriter(xdf_path)
    w.start()
    write_lsl_stream(
        writer=w,
        name="EEG",
        srate=srate,
        channels=4,
        n_samples=int(duration * srate),
    )
    w.stop()

    report = validate_test_recording(
        xdf_path=xdf_path,
        cfg=base_cfg(),
        lsl_streams=[stream_info],
        expected_duration_s=duration,
    )
    assert report.passed, report.detailed_text()


def test_lsl_missing_stream_fails(xdf_path):
    stream_info = make_eeg_stream_info(
        name="EEG",
        channels=4,
        srate=250.0,
    )

    w = XDFWriter(xdf_path)
    w.start()
    w.stop()

    report = validate_test_recording(
        xdf_path=xdf_path,
        cfg=base_cfg(),
        lsl_streams=[stream_info],
        expected_duration_s=10.0,
    )
    assert not report.passed
    assert any("LSL stream <EEG> present" in c.name and not c.passed for c in report.checks)


def test_lsl_no_clock_offset_fails(xdf_path):
    duration = 10.0
    srate = 250.0
    stream_info = make_eeg_stream_info(
        name="EEG",
        channels=4,
        srate=srate,
    )

    w = XDFWriter(xdf_path)
    w.start()
    write_lsl_stream(
        writer=w,
        name="EEG",
        srate=srate,
        channels=4,
        n_samples=int(duration * srate),
        with_clock_offset=False,
    )
    w.stop()

    report = validate_test_recording(
        xdf_path=xdf_path,
        cfg=base_cfg(),
        lsl_streams=[stream_info],
        expected_duration_s=duration,
    )
    assert not report.passed
    offset_check = next(c for c in report.checks if "clock offset measured" in c.name)
    assert not offset_check.passed


def test_lsl_irregular_rate_stream_checks_only_presence(xdf_path):
    """Markers/irregular-rate streams (nominal_srate == 0, LSL's IRREGULAR_RATE) skip the rate/count checks."""
    stream_info = make_eeg_stream_info(
        name="Markers",
        channels=1,
        srate=0.0,
    )

    w = XDFWriter(xdf_path)
    w.start()
    sid = w.add_lsl_stream(
        name="Markers",
        stype="Markers",
        channel_count=1,
        srate=0.0,
        fmt="float32",
        source_id="src",
        key="lsl:Markers",
    )
    # Irregular streams have no fixed sample period; use arbitrarily-spaced timestamps
    ts = np.array([100.0, 102.5, 107.0])
    w.write_lsl_samples(
        stream_id=sid,
        timestamps=ts,
        samples=np.zeros((3, 1), dtype=np.float32),
    )
    w.record_clock_offset(
        stream_id=sid,
        offset=0.0,
        now=ts[-1],
    )
    w.stop()

    report = validate_test_recording(
        xdf_path=xdf_path,
        cfg=base_cfg(),
        lsl_streams=[stream_info],
        expected_duration_s=10.0,
    )
    assert not any("sample rate" in c.name for c in report.checks if "Markers" in c.name)
    count_check = next(c for c in report.checks if "Markers" in c.name and "sample count" in c.name)
    assert count_check.passed


def test_lsl_irregular_rate_stream_zero_samples_pass(xdf_path):
    """An event-driven Markers stream that emits no events during the test
    window (e.g. no stimuli/triggers happened to fire) is a normal outcome,
    not a validation failure."""
    stream_info = make_eeg_stream_info(name=MARKER_STREAM_NAME, channels=1, srate=0.0)

    w = XDFWriter(xdf_path)
    w.start()
    sid = w.add_lsl_stream(
        name=MARKER_STREAM_NAME,
        stype="Markers",
        channel_count=1,
        srate=0.0,
        fmt="float32",
        source_id="src",
        key="lsl:Markers",
    )
    # No write_lsl_samples() call: zero events occurred during the test
    w.record_clock_offset(stream_id=sid, offset=0.0, now=110.0)
    w.stop()

    report = validate_test_recording(
        xdf_path=xdf_path,
        cfg=base_cfg(),
        lsl_streams=[stream_info],
        expected_duration_s=10.0,
    )
    assert report.passed, report.detailed_text()
    count_check = next(c for c in report.checks if MARKER_STREAM_NAME in c.name and "sample count" in c.name)
    assert count_check.passed
    assert "0 samples" in count_check.detail


def test_lsl_string_format_marker_stream_pass(xdf_path):
    """Test that a real  Markers-shaped stream (channel_format="string", irregular rate) validates cleanly."""
    stream_info = StreamInfo(
        name=MARKER_STREAM_NAME,
        type="Markers",
        channel_count=1,
        nominal_srate=0.0,
        channel_format="string",
        source_id="src-markers",
    )

    w = XDFWriter(xdf_path)
    w.start()
    sid = w.add_lsl_stream(
        name=MARKER_STREAM_NAME,
        stype="Markers",
        channel_count=1,
        srate=0.0,
        fmt="string",
        source_id="src-markers",
        key="lsl:markers",
    )
    ts = np.array([100.0, 102.5, 107.0])
    w.write_lsl_samples(
        stream_id=sid,
        timestamps=ts,
        samples=[["Stimulus/S1"], ["Stimulus/S2"], ["Response/R1"]],
    )
    w.record_clock_offset(stream_id=sid, offset=0.0, now=ts[-1])
    w.stop()

    report = validate_test_recording(
        xdf_path=xdf_path,
        cfg=base_cfg(),
        lsl_streams=[stream_info],
        expected_duration_s=10.0,
    )
    assert report.passed, report.detailed_text()


# ---------------------------------------------------------------------------
# Audio properties: declared and measured rate, bit depth, device, level
# ---------------------------------------------------------------------------

def _audio_cfg(samplerate=1000, bitdepth=32, device_name=None, channels=1, labels=None):
    cfg = base_cfg()
    cfg.Audio.Enabled = True
    cfg.Audio.StreamName = "Audio"
    cfg.Audio.SampleRate = samplerate
    cfg.Audio.BitDepth = bitdepth
    cfg.Audio.Channels = channels
    cfg.Audio.ChannelLabels = list(labels or [])
    cfg.Audio.DeviceName = device_name
    return cfg


def _record_audio(
    path,
    *,
    samplerate=1000,
    duration=10.0,
    extra=None,
    footer=None,
    level=0.1,
    fmt="float32",
    channel_levels=None,
    labels=None,
):
    """Write an audio stream carrying `extra` header fields and `footer` fields.
    `channel_levels` gives the amplitude of each channel (default: one channel at `level`);
    `labels` the channel labels (default: 'Channel N')."""
    channel_levels = list(channel_levels) if channel_levels is not None else [level]
    n_channels = len(channel_levels)
    w = XDFWriter(path)
    w.start()
    sid = w.add_audio_stream(
        name="Audio",
        samplerate=samplerate,
        channels=n_channels,
        fmt=fmt,
        extra=extra,
        channel_labels=labels if labels is not None else resolve_channel_labels([], n_channels),
    )
    n = int(duration * samplerate)
    t = np.arange(n) / samplerate
    signal = np.stack([lvl * np.sin(2 * np.pi * 5 * t) for lvl in channel_levels], axis=1)
    samples = np.rint(signal * 32768).astype(np.int16) if fmt == "int16" else signal.astype(np.float32)
    w.write_audio(sid, 100.0 + t, samples)
    if footer:
        w.set_stream_footer_extra(sid, footer)
    w.stop()
    return duration


def _check_named(report, name):
    return next(c for c in report.checks if c.name == name)


def test_audio_measured_rate_within_tolerance_passes(xdf_path):
    duration = _record_audio(xdf_path, footer={"measured_srate": "1003.000"})
    report = validate_test_recording(xdf_path, _audio_cfg(), [], duration)
    assert _check_named(report, "Audio measured sample rate (real time)").passed
    assert report.passed, report.detailed_text()


def test_audio_measured_rate_far_from_configured_fails(xdf_path):
    """A device delivered about 48 kHz while the stream was labelled 44.1 kHz.
    The timestamps (built from the configured rate) look perfect; only the measured rate shows it."""
    duration = _record_audio(
        xdf_path,
        samplerate=44100,
        duration=2.0,
        footer={"measured_srate": "47940.000"},
    )
    report = validate_test_recording(xdf_path, _audio_cfg(samplerate=44100), [], duration)
    check = _check_named(report, "Audio measured sample rate (real time)")
    assert not check.passed and "47940" in check.detail
    assert not report.passed


def test_audio_measured_rate_missing_is_a_warning_not_a_failure(xdf_path):
    duration = _record_audio(xdf_path)
    report = validate_test_recording(xdf_path, _audio_cfg(), [], duration)
    check = _check_named(report, "Audio measured sample rate (real time)")
    assert check.passed and check.warning
    assert report.passed and report.warnings


def test_audio_header_sample_rate_mismatch_fails(xdf_path):
    """The device did not accept the configured rate and the stream was opened at another."""
    duration = _record_audio(xdf_path, samplerate=1000)
    report = validate_test_recording(xdf_path, _audio_cfg(samplerate=2000), [], duration)
    check = _check_named(report, "Audio sample rate in header")
    assert not check.passed and "did not accept" in check.detail


def test_audio_capture_bit_depth_mismatch_fails(xdf_path):
    """The device refused float32 capture and fell back to 16-bit."""
    duration = _record_audio(xdf_path, extra={"capture_bit_depth": 16})
    report = validate_test_recording(xdf_path, _audio_cfg(bitdepth=32), [], duration)
    check = _check_named(report, "Audio capture bit depth")
    assert not check.passed and "refused" in check.detail


def test_audio_capture_bit_depth_match_passes(xdf_path):
    duration = _record_audio(xdf_path, extra={"capture_bit_depth": 32})
    report = validate_test_recording(xdf_path, _audio_cfg(bitdepth=32), [], duration)
    assert _check_named(report, "Audio capture bit depth").passed


def test_audio_device_name_ignores_alsa_card_suffix(xdf_path):
    duration = _record_audio(xdf_path, extra={"device_name": "Scarlett 2i2 USB: Audio (hw:6,0)"})
    report = validate_test_recording(xdf_path, _audio_cfg(device_name="Scarlett 2i2 USB: Audio"), [], duration)
    assert _check_named(report, "Audio input device").passed


def test_audio_device_name_mismatch_fails(xdf_path):
    duration = _record_audio(xdf_path, extra={"device_name": "Logitech BRIO: USB Audio (hw:3,0)"})
    report = validate_test_recording(xdf_path, _audio_cfg(device_name="Scarlett 2i2 USB: Audio"), [], duration)
    assert not _check_named(report, "Audio input device").passed


def test_audio_device_check_skipped_without_a_selected_device(xdf_path):
    """The system default device has no saved name to compare against."""
    duration = _record_audio(xdf_path, extra={"device_name": "Built-in Microphone"})
    report = validate_test_recording(xdf_path, _audio_cfg(device_name=None), [], duration)
    assert all(c.name != "Audio input device" for c in report.checks)


def test_audio_input_overflows_are_a_warning(xdf_path):
    duration = _record_audio(xdf_path, footer={"measured_srate": "1000.000", "input_overflows": 3})
    report = validate_test_recording(xdf_path, _audio_cfg(), [], duration)
    check = _check_named(report, "Audio input overflows")
    assert check.passed and check.warning and "3 overflow" in check.detail


def test_audio_normal_level_has_no_warning(xdf_path):
    duration = _record_audio(xdf_path, level=0.1, footer={"measured_srate": "1000.000"})
    report = validate_test_recording(xdf_path, _audio_cfg(), [], duration)
    check = _check_named(report, "Audio signal level")
    assert check.passed and not check.warning
    assert "peak -20.0 dBFS" in check.detail
    assert not report.warnings


def test_audio_silent_quiet_and_clipping_signals_are_flagged(xdf_path):
    expectations = {0.0: "silent", 0.003: "very quiet", 1.0: "clipping"}
    for level, word in expectations.items():
        duration = _record_audio(xdf_path, level=level, footer={"measured_srate": "1000.000"})
        report = validate_test_recording(xdf_path, _audio_cfg(), [], duration)
        check = _check_named(report, "Audio signal level")
        assert check.passed and check.warning and word in check.detail, (level, check.detail)


def test_audio_level_check_scales_int16_samples(xdf_path):
    """int16 storage is scaled by full scale (32768) so levels read the same as for float32."""
    duration = _record_audio(xdf_path, fmt="int16", level=0.1, footer={"measured_srate": "1000.000"})
    cfg = _audio_cfg()
    cfg.Audio.SampleFormat = "int16"
    report = validate_test_recording(xdf_path, cfg, [], duration)
    check = _check_named(report, "Audio signal level")
    assert "peak -20.0 dBFS" in check.detail and not check.warning


def test_audio_sample_count_tolerance_is_tighter_than_other_streams(xdf_path):
    """12% short still passes (start-up latency); 20% short fails."""
    for n_samples, should_pass in ((8800, True), (8000, False)):
        w = XDFWriter(xdf_path)
        w.start()
        write_audio_stream(w, "Audio", 1000, 1, n_samples)
        w.stop()
        report = validate_test_recording(xdf_path, _audio_cfg(), [], 10.0)
        assert _check_named(report, "Audio sample count").passed is should_pass, n_samples


def test_report_summary_and_detail_mention_warnings(xdf_path):
    duration = _record_audio(xdf_path, level=0.0)
    report = validate_test_recording(xdf_path, _audio_cfg(), [], duration)
    assert "warning(s)" in report.summary()
    assert "[WARN] Audio signal level" in report.detailed_text()


# ---------------------------------------------------------------------------
# Audio channels: labels in the header and per-channel levels
# ---------------------------------------------------------------------------

def test_audio_silent_second_channel_is_flagged(xdf_path):
    """One silent channel next to an active one."""
    duration = _record_audio(xdf_path, channel_levels=[0.1, 0.0])
    report = validate_test_recording(xdf_path, _audio_cfg(channels=2), [], duration)
    flagged = [c for c in report.checks if c.name == "Audio channel levels"]
    assert len(flagged) == 1 and flagged[0].passed and flagged[0].warning
    assert "Channel 2 is silent" in flagged[0].detail and "another channel carries a signal" in flagged[0].detail
    assert report.passed and report.warnings


def test_audio_channel_labels_in_header_match_config(xdf_path):
    duration = _record_audio(xdf_path, channel_levels=[0.1, 0.1], labels=["Participant A", "Participant B"])
    cfg = _audio_cfg(channels=2, labels=["Participant A", "Participant B"])
    report = validate_test_recording(xdf_path, cfg, [], duration)
    assert _check_named(report, "Audio channel labels in header").passed


def test_audio_channel_labels_missing_from_header_fail(xdf_path):
    duration = _record_audio(xdf_path)
    w_labels = XDFWriter(xdf_path)
    w_labels.start()
    sid = w_labels.add_audio_stream(name="Audio", samplerate=1000, channels=1)  # no labels written
    w_labels.write_audio(sid, 100.0 + np.arange(10000) / 1000, np.full((10000, 1), 0.1, dtype=np.float32))
    w_labels.stop()
    report = validate_test_recording(xdf_path, _audio_cfg(), [], duration)
    check = _check_named(report, "Audio channel labels in header")
    assert not check.passed and "no channel labels" in check.detail


def test_audio_channel_labels_differing_from_config_fail(xdf_path):
    duration = _record_audio(xdf_path, channel_levels=[0.1, 0.1], labels=["A", "B"])
    cfg = _audio_cfg(channels=2, labels=["X", "Y"])
    report = validate_test_recording(xdf_path, cfg, [], duration)
    assert not _check_named(report, "Audio channel labels in header").passed


def test_audio_unlabelled_channels_get_default_labels_in_header(xdf_path):
    duration = _record_audio(xdf_path, channel_levels=[0.1, 0.1])
    cfg = _audio_cfg(channels=2, labels=["Mic"])
    # the file was written with default labels only, so the configured first label is missing
    report = validate_test_recording(xdf_path, cfg, [], duration)
    check = _check_named(report, "Audio channel labels in header")
    assert not check.passed and "Mic" in check.detail


def test_audio_duplicate_labels_are_a_warning(xdf_path):
    duration = _record_audio(xdf_path, channel_levels=[0.1, 0.1], labels=["A", "A"])
    cfg = _audio_cfg(channels=2, labels=["A", "A"])
    report = validate_test_recording(xdf_path, cfg, [], duration)
    warned = [c for c in report.checks if c.name == "Audio channel labels" and c.warning]
    assert warned and "more than one channel" in warned[0].detail


def test_audio_silent_second_channel_is_flagged_with_its_label(xdf_path):
    """One silent audio channel next to an active one."""
    duration = _record_audio(
        xdf_path, channel_levels=[0.1, 0.0], labels=["Participant A", "Participant B"],
    )
    cfg = _audio_cfg(channels=2, labels=["Participant A", "Participant B"])
    report = validate_test_recording(xdf_path, cfg, [], duration)
    flagged = [c for c in report.checks if c.name == "Audio channel levels"]
    assert len(flagged) == 1 and flagged[0].passed and flagged[0].warning
    assert "Channel 2 (Participant B) is silent" in flagged[0].detail


def test_audio_all_channels_active_passes_without_warning(xdf_path):
    duration = _record_audio(xdf_path, channel_levels=[0.1, 0.1])
    report = validate_test_recording(xdf_path, _audio_cfg(channels=2), [], duration)
    check = _check_named(report, "Audio channel levels")
    assert check.passed and not check.warning


def test_audio_silent_recording_is_flagged_once_not_per_channel(xdf_path):
    duration = _record_audio(xdf_path, channel_levels=[0.0, 0.0])
    report = validate_test_recording(xdf_path, _audio_cfg(channels=2), [], duration)
    assert _check_named(report, "Audio signal level").warning
    assert not [c for c in report.checks if c.name == "Audio channel levels" and c.warning]


def test_audio_single_channel_has_no_channel_level_check(xdf_path):
    duration = _record_audio(xdf_path)
    report = validate_test_recording(xdf_path, _audio_cfg(), [], duration)
    assert not [c for c in report.checks if c.name == "Audio channel levels"]
