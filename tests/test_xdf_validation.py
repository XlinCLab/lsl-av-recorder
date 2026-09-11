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
