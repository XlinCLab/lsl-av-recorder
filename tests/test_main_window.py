"""Tests for the pure logic in recorder.gui.main_window that does not require running QApplication."""
from __future__ import annotations

import json

from recorder.config import AppConfig, VideoCamConfig
from recorder.gui import main_window
from recorder.gui.camera_worker import CAMERA_PREVIEW_STREAM_TYPE
from recorder.gui.main_window import (_default_camera_label,
                                      _exclude_camera_preview_streams,
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
