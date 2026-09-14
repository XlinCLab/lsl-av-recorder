"""Tests for the pure logic in recorder.gui.camera_worker that does not require running QApplication."""
from __future__ import annotations

from recorder.gui.camera_worker import compute_preview_key


def test_compute_preview_key_prefers_device_name():
    """Device name is the stable, authoritative identity:
    used whenever available, regardless of what index/devnode also happen to be set."""
    assert compute_preview_key("FaceTime HD Camera", "1", 1) == "FaceTime HD Camera"


def test_compute_preview_key_falls_back_to_devnode_without_a_name():
    assert compute_preview_key(None, "/dev/video2", 2) == "/dev/video2"
    assert compute_preview_key("", "/dev/video2", 2) == "/dev/video2"


def test_compute_preview_key_falls_back_to_index_with_nothing_else():
    assert compute_preview_key(None, "", 3) == "idx3"


def test_compute_preview_key_distinguishes_two_cameras_sharing_an_index():
    """Prevent two different physical cameras from being assigned the
    same raw device index by AVFoundation's enumeration
    (not guaranteed to be stable), especially while another
    camera is actively open. Keying by name instead means they never
    collide, even when their index does."""
    key_a = compute_preview_key("FaceTime HD Camera", "1", 1)
    key_b = compute_preview_key("C922 Pro Stream Webcam", "1", 1)
    assert key_a != key_b
