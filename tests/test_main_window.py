"""Tests for the pure logic in recorder.gui.main_window that does not require running QApplication."""
from __future__ import annotations

import json

from recorder.config import AppConfig, VideoCamConfig
from recorder.gui import main_window
from recorder.gui.camera_worker import CAMERA_PREVIEW_STREAM_TYPE
from recorder.gui.main_window import (_default_camera_label,
                                      _exclude_camera_preview_streams,
                                      _recording_stream_rows,
                                      build_config_log_payload)
from tests.shared import MARKER_STREAM_NAME


class FakeStream:
    def __init__(self, name: str, stype: str):
        self._name = name
        self._stype = stype

    def name(self):
        return self._name

    def type(self):
        return self._stype


def test_excludes_camera_preview_streams():
    """A stream published by this app's own camera preview (CameraWorker) is dropped."""
    preview = FakeStream(
        name="VideoFrames_cam-00_role-Face",
        stype=CAMERA_PREVIEW_STREAM_TYPE,
    )
    eeg = FakeStream(name="EEG", stype="EEG")

    result = _exclude_camera_preview_streams([preview, eeg])

    assert result == [eeg]


def test_default_camera_label_fills_blank_with_numbered_default():
    """A blank camera label (whitespace-only or empty) falls back to Cam{position}."""
    assert _default_camera_label("", 1) == "Cam1"
    assert _default_camera_label("   ", 2) == "Cam2"


def test_default_camera_label_preserves_explicit_label():
    """A real, non-blank label is kept verbatim regardless of position."""
    assert _default_camera_label("FaceTime", 1) == "FaceTime"
    assert _default_camera_label("  Left Cam  ", 3) == "Left Cam"


def test_keeps_non_preview_streams_unchanged():
    """Streams of any other type (including externally-published streams that
    happen to share a name pattern) pass through untouched."""
    streams = [
        FakeStream(name=MARKER_STREAM_NAME, stype="Markers"), 
        FakeStream(name="EEG", stype="EEG"),
    ]
    assert _exclude_camera_preview_streams(streams) == streams


# ---------------------------------------------------------------------------
# build_config_log_payload
# ---------------------------------------------------------------------------

def test_build_config_log_payload_is_valid_json_with_expected_shape(monkeypatch):
    """The logged payload is valid JSON carrying the event label, environment
    info, and the full config -- so a single log line is self-contained and
    self-identifying (no need to cross-reference a separate line/file)."""
    fake_env = {
        "commit": "abc123def456",
        "platform": "macOS-test",
        "hostname": "test-host",
        "python_version": "3.12.0",
    }
    monkeypatch.setattr(
        main_window,
        "get_environment_info",
        lambda root: fake_env,
    )
    cfg = AppConfig()
    cfg.Prompts.Subject = "01"
    event = "recording_started"
    line = build_config_log_payload(event, cfg)
    parsed = json.loads(line)

    assert parsed["event"] == event
    assert parsed["environment"] == fake_env
    assert parsed["config"]["Prompts"]["Subject"] == "01"


def test_build_config_log_payload_serializes_full_nested_config(monkeypatch):
    """Nested dataclasses (e.g. per-camera configs) round-trip through JSON."""
    monkeypatch.setattr(main_window, "get_environment_info", lambda root: {})
    cfg = AppConfig()
    cfg.Video.Cams = [VideoCamConfig(Label="Face", FPS=30)]

    parsed = json.loads(build_config_log_payload("config_loaded", cfg))
    assert parsed["config"]["Video"]["Cams"][0]["Label"] == "Face"
    assert parsed["config"]["Video"]["Cams"][0]["FPS"] == 30


# ---------------------------------------------------------------------------
# _recording_stream_rows
# ---------------------------------------------------------------------------

class _FakeAudioSettings:
    def __init__(
        self,
        stream_name="Audio",
        device_name="Built-in Microphone",
        samplerate=48000,
        channels=1,
        bitdepth=32,
    ):
        self.stream_name = stream_name
        self.device_name = device_name
        self.samplerate = samplerate
        self.channels = channels
        self.bitdepth = bitdepth


class _FakeLslStreamInfo:
    def __init__(self, name, stype, srate, channels, hostname="lsl-host"):
        self._name = name
        self._type = stype
        self._srate = srate
        self._channels = channels
        self._hostname = hostname

    def name(self):
        return self._name

    def type(self):
        return self._type

    def nominal_srate(self):
        return self._srate

    def channel_count(self):
        return self._channels

    def hostname(self):
        return self._hostname


class _FakeRunController:
    def __init__(
        self, audio_enabled=False, audio_settings=None,
        video_enabled=False, cams=None, lsl_streams=None,
    ):
        self.audio_enabled = audio_enabled
        self.audio_settings = audio_settings
        self.video_enabled = video_enabled
        self.cams = cams or []
        self.lsl_streams = lsl_streams or []


def test_recording_stream_rows_none_controller_is_empty():
    """Before any recording has started, there's no controller yet."""
    assert _recording_stream_rows(None) == []


def test_recording_stream_rows_audio_only():
    controller = _FakeRunController(
        audio_enabled=True,
        audio_settings=_FakeAudioSettings(
            device_name="USB Microphone",
            samplerate=44100,
            channels=2,
            bitdepth=16,
        ),
    )
    assert _recording_stream_rows(controller) == [
        ("Audio", "Audio", "USB Microphone", "44100 Hz, 2 ch, 16-bit"),
    ]


def test_recording_stream_rows_audio_falls_back_to_default_when_unnamed():
    controller = _FakeRunController(
        audio_enabled=True,
        audio_settings=_FakeAudioSettings(device_name=None),
    )
    name, stype, device, details = _recording_stream_rows(controller)[0]
    assert device == "(default)"


def test_recording_stream_rows_video_one_row_per_cam():
    controller = _FakeRunController(
        video_enabled=True,
        cams=[
            VideoCamConfig(
                Label="Cam1",
                DeviceName="Logitech BRIO",
                Width=1280,
                Height=720,
                FPS=30,
                PixelFormat="NV12",
            ),
            VideoCamConfig(
                Label="Cam2",
                Width=640,
                Height=480,
                FPS=60,
                PixelFormat="YUYV",
            ),
        ],
    )
    assert _recording_stream_rows(controller) == [
        ("Cam1", "Video", "Logitech BRIO", "1280x720 @ 30fps, NV12"),
        ("Cam2", "Video", "Unknown device", "640x480 @ 60fps, YUYV"),
    ]


def test_recording_stream_rows_lsl_regular_and_irregular_rate():
    """A genuine sample rate renders as 'X Hz'; nominal_srate()==0
    (LSL's IRREGULAR_RATE, e.g. a Markers stream) renders as 'irregular rate'.
    The device column is the outlet's hostname."""
    controller = _FakeRunController(
        lsl_streams=[
            _FakeLslStreamInfo("EEG", "EEG", 250.0, 4, hostname="eeg-host"),
            _FakeLslStreamInfo("Markers", "Markers", 0.0, 1, hostname="stim-pc"),
        ],
    )
    assert _recording_stream_rows(controller) == [
        ("EEG", "EEG", "eeg-host", "250 Hz, 4 ch"),
        ("Markers", "Markers", "stim-pc", "irregular rate, 1 ch"),
    ]


def test_recording_stream_rows_combines_all_stream_types_in_order():
    controller = _FakeRunController(
        audio_enabled=True,
        audio_settings=_FakeAudioSettings(),
        video_enabled=True,
        cams=[VideoCamConfig(Label="Cam1", Width=1280, Height=720, FPS=30, PixelFormat="NV12")],
        lsl_streams=[_FakeLslStreamInfo("EEG", "EEG", 250.0, 4)],
    )
    rows = _recording_stream_rows(controller)
    assert [r[1] for r in rows] == ["Audio", "Video", "EEG"]


def test_recording_stream_rows_disabled_audio_and_video_are_excluded():
    """audio_enabled/video_enabled False means those streams weren't
    actually started, even if audio_settings/cams happen to be set."""
    controller = _FakeRunController(
        audio_enabled=False,
        audio_settings=_FakeAudioSettings(),
        video_enabled=False,
        cams=[VideoCamConfig(Label="Cam1", Width=1280, Height=720, FPS=30, PixelFormat="NV12")],
    )
    assert _recording_stream_rows(controller) == []
