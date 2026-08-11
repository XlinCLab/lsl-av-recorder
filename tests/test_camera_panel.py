"""Tests for the pure logic in recorder.gui.camera_panel that does not require running QApplication."""
from __future__ import annotations

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
