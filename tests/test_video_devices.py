"""Tests for recorder.video.devices device-list parsers.

_parse_mac_avfoundation_video_devices and _parse_linux_v4l2_devices turn raw
`ffmpeg -list_devices` / `v4l2-ctl --list-devices` output into the device dicts
the GUI shows.
"""
from __future__ import annotations

from recorder.video import devices as devices_mod
from recorder.video.devices import (_parse_linux_v4l2_devices,
                                    _parse_mac_avfoundation_video_devices,
                                    list_video_devices)

MAC_LIST = """
[AVFoundation indev @ 0x1] AVFoundation video devices:
[AVFoundation indev @ 0x1] [0] FaceTime HD Camera
[AVFoundation indev @ 0x1] [1] USB Cam
[AVFoundation indev @ 0x1] AVFoundation audio devices:
[AVFoundation indev @ 0x1] [0] MacBook Microphone
"""

LINUX_LIST = """
HD Pro Webcam C920 (usb-0000:00:14.0-1):
\t/dev/video0
\t/dev/video1

Integrated Camera (usb-0000:00:14.0-2):
\t/dev/video2
"""


# ---------------------------------------------------------------------------
# macOS parsing
# ---------------------------------------------------------------------------

def test_parse_mac_devices_only_video_section():
    """Only entries under 'AVFoundation video devices:' are parsed; the audio
    section (which also has '[0] ...' lines) is excluded."""
    devs = _parse_mac_avfoundation_video_devices(text=MAC_LIST)
    assert devs == [
        {"index": 0, "name": "FaceTime HD Camera", "devnode": "0"},
        {"index": 1, "name": "USB Cam", "devnode": "1"},
    ]


def test_parse_mac_devices_empty_when_no_section():
    """Output with no video-devices section yields an empty list."""
    assert _parse_mac_avfoundation_video_devices(text="no devices listed") == []


# ---------------------------------------------------------------------------
# Linux parsing
# ---------------------------------------------------------------------------

def test_parse_linux_devices_associates_label_with_each_node():
    """Each /dev/videoN node inherits the label of the device block it appears
    under, so two nodes of one webcam share that webcam's name."""
    devs = _parse_linux_v4l2_devices(text=LINUX_LIST)
    assert devs == [
        {"index": 0, "name": "HD Pro Webcam C920 (usb-0000:00:14.0-1)", "devnode": "/dev/video0"},
        {"index": 1, "name": "HD Pro Webcam C920 (usb-0000:00:14.0-1)", "devnode": "/dev/video1"},
        {"index": 2, "name": "Integrated Camera (usb-0000:00:14.0-2)", "devnode": "/dev/video2"},
    ]


def test_parse_linux_devices_empty_input():
    """Empty input yields an empty list."""
    assert _parse_linux_v4l2_devices(text="") == []


# ---------------------------------------------------------------------------
# list_video_devices dispatch
# ---------------------------------------------------------------------------

def test_list_video_devices_mac(monkeypatch, as_platform):
    """On macOS, list_video_devices runs the ffmpeg avfoundation probe and returns the parsed video devices."""
    as_platform(devices_mod, "mac")
    monkeypatch.setattr(devices_mod, "run_capture_cmd", lambda cmd, check=True: (MAC_LIST, None))
    devs = list_video_devices()
    assert [d["name"] for d in devs] == ["FaceTime HD Camera", "USB Cam"]


def test_list_video_devices_linux(monkeypatch, as_platform):
    """On Linux, list_video_devices runs v4l2-ctl and returns the parsed
    /dev/video* nodes."""
    as_platform(devices_mod, "linux")
    monkeypatch.setattr(devices_mod, "run_capture_cmd", lambda cmd, check=True: (LINUX_LIST, None))
    devs = list_video_devices()
    assert [d["devnode"] for d in devs] == ["/dev/video0", "/dev/video1", "/dev/video2"]


def test_list_video_devices_windows(monkeypatch, as_platform):
    """On Windows, list_video_devices delegates to the DirectShow enumerator
    (list_windows_video_devices) and returns its result unchanged.

    Unlike the mac/linux paths, Windows enumeration goes through COM (pygrabber's
    FilterGraph), which can't run off-Windows, so this test fakes that call.
    """
    as_platform(devices_mod, "windows")
    fake_devices = [
        {"index": 0, "name": "Integrated Webcam", "devnode": "0"},
        {"index": 1, "name": "USB Cam", "devnode": "1"},
    ]
    monkeypatch.setattr(devices_mod, "list_windows_video_devices", lambda: fake_devices)
    assert list_video_devices() == fake_devices
