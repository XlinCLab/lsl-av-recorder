"""Tests for the pure logic in recorder.gui.camera_worker that does not require running QApplication."""
from __future__ import annotations

import numpy as np

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


# ---------------------------------------------------------------------------
# LSL frame-sync timestamps are taken right after cap.read()
# ---------------------------------------------------------------------------

class _FakeClock:
    """Stand-in for pylsl.local_clock whose time only moves when told to."""

    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class _FakeOutlet:
    def __init__(self, *args, **kwargs):
        self.pushed: list[tuple[list[float], float]] = []

    def push_sample(self, sample, timestamp):
        self.pushed.append((sample, timestamp))


def test_preview_lsl_timestamps_exclude_later_per_frame_work(monkeypatch):
    """The LSL outlet sample is stamped with the time cap.read() returned, even
    when other work in the loop (here: opening a writer on a record request)
    takes a long time before the sample is pushed."""
    from recorder.gui import camera_worker
    from recorder.gui.camera_worker import CameraWorker, RecordParams

    clock = _FakeClock()
    monkeypatch.setattr(camera_worker, "local_clock", clock)
    monkeypatch.setattr(camera_worker, "StreamOutlet", _FakeOutlet)

    worker = CameraWorker(cam_index=0, devnode="/dev/video0", label="Cam", fps=30, size=(2, 2))
    read_times: list[float] = []

    class _Cap:
        def read(self):
            if len(read_times) >= 3:
                worker._running = False
                return False, None
            clock.t += 0.033
            read_times.append(clock.t)
            return True, np.zeros((2, 2, 3), dtype=np.uint8)

        def release(self):
            pass

    def fake_open_cap():
        worker.cap = _Cap()

    def slow_open_writer(rp):
        clock.t += 5.0  # e.g. a slow VideoWriter open
        worker.writer = None

    monkeypatch.setattr(worker, "_open_cap", fake_open_cap)
    monkeypatch.setattr(worker, "_open_writer", slow_open_writer)
    worker._record_request = RecordParams(out_path="unused.mp4", fps=30, size=(2, 2))

    worker.start_preview()

    assert [ts for _, ts in worker.outlet.pushed] == read_times
    assert [s[0] for s, _ in worker.outlet.pushed] == [0.0, 1.0, 2.0]
