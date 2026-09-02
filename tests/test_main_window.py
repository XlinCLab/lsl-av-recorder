"""Tests for the pure logic in recorder.gui.main_window that does not require running QApplication."""
from __future__ import annotations

from recorder.gui.camera_worker import CAMERA_PREVIEW_STREAM_TYPE
from recorder.gui.main_window import _exclude_camera_preview_streams
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


def test_keeps_non_preview_streams_unchanged():
    """Streams of any other type (including externally-published streams that
    happen to share a name pattern) pass through untouched."""
    streams = [
        FakeStream(name=MARKER_STREAM_NAME, stype="Markers"), 
        FakeStream(name="EEG", stype="EEG"),
    ]
    assert _exclude_camera_preview_streams(streams) == streams
