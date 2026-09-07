"""Tests for the pure logic in recorder.gui.camera_panel that does not require running QApplication."""
from __future__ import annotations

from recorder.gui import camera_panel as camera_panel_module
from recorder.gui.camera_panel import CameraPanel


def test_device_key_builds_identity_tuple():
    """A device dict maps to a (name, index, devnode) identity tuple."""
    key = CameraPanel._device_key(
        {
            "name": "USB Cam",
            "index": 1,
            "devnode": "1",
        }
    )
    assert key == ("USB Cam", 1, "1")


def test_device_key_non_dict_returns_none():
    """A non-dict entry (e.g. the transient empty selection during a refresh) has no identity."""
    assert CameraPanel._device_key("not-a-device") is None
    assert CameraPanel._device_key(None) is None


def test_device_key_missing_fields_use_defaults():
    """Missing fields fall back to neutral defaults ('' name/devnode, -1 index)."""
    assert CameraPanel._device_key({}) == ("", -1, "")


def test_device_key_distinguishes_and_matches_devices():
    """Different cameras produce different keys; the same camera produces the
    same key regardless of dict identity."""
    a = CameraPanel._device_key(
        {
            "name": "Cam",
            "index": 0,
            "devnode": "0",
        }
    )
    b = CameraPanel._device_key(
        {
            "name": "Cam",
            "index": 1,
            "devnode": "1",
        }
    )
    same = CameraPanel._device_key(
        {
            "name": "Cam",
            "index": 0,
            "devnode": "0",
        }
    )
    assert a != b
    assert a == same


# ---------------------------------------------------------------------------
# _resolve_device_identity_by_name
# ---------------------------------------------------------------------------

FAKE_DEVICES = [
    {"index": 0, "name": "FaceTime HD Camera", "devnode": "0"},
    {"index": 1, "name": "External Webcam", "devnode": "1"},
]


def test_resolve_device_identity_by_name_finds_current_index(monkeypatch):
    """Resolves to whatever index/devnode list_video_devices() currently
    reports for this name, not the fallback."""
    monkeypatch.setattr(camera_panel_module, "IS_MAC", True)
    monkeypatch.setattr(camera_panel_module, "list_video_devices", lambda: FAKE_DEVICES)

    idx, devnode = CameraPanel._resolve_device_identity_by_name("External Webcam", 0, "0")
    assert (idx, devnode) == (1, "1")


def test_resolve_device_identity_by_name_falls_back_when_not_found(monkeypatch):
    """A name no longer present in the current device list (e.g. unplugged)
    falls back to the given values rather than raising or silently picking
    a different device."""
    monkeypatch.setattr(camera_panel_module, "IS_MAC", True)
    monkeypatch.setattr(camera_panel_module, "list_video_devices", lambda: FAKE_DEVICES)

    idx, devnode = CameraPanel._resolve_device_identity_by_name("Unplugged Cam", 7, "7")
    assert (idx, devnode) == (7, "7")


def test_resolve_device_identity_by_name_falls_back_without_name(monkeypatch):
    """No name to match against (e.g. a brand-new default panel) means no
    lookup is attempted; the fallback is returned as-is."""
    monkeypatch.setattr(camera_panel_module, "IS_MAC", True)
    called = []
    monkeypatch.setattr(
        camera_panel_module, "list_video_devices", lambda: called.append(1) or FAKE_DEVICES
    )

    idx, devnode = CameraPanel._resolve_device_identity_by_name(None, 3, "3")
    assert (idx, devnode) == (3, "3")
    assert not called


def test_resolve_device_identity_by_name_skipped_off_mac(monkeypatch):
    """This re-resolution only applies on macOS; elsewhere the fallback is trusted as-is."""
    monkeypatch.setattr(camera_panel_module, "IS_MAC", False)
    called = []
    monkeypatch.setattr(
        camera_panel_module, "list_video_devices", lambda: called.append(1) or FAKE_DEVICES
    )

    idx, devnode = CameraPanel._resolve_device_identity_by_name("External Webcam", 5, "/dev/video5")
    assert (idx, devnode) == (5, "/dev/video5")
    assert not called
