"""Integration test for the LSL inlet path using a real pylsl loopback stream.

Every other LSL test in this suite (test_xdf_sync.py, test_lsl_formats.py)
exercises LslInletRecorder against a faked StreamInfo/StreamInlet, which
never touches pylsl's actual network resolution or wire protocol. This test
instead starts a genuine pylsl.StreamOutlet in a background thread, resolves
it with pylsl.resolve_byprop() exactly as MainWindow.on_discover_lsl_streams() does,
and runs it through the real StreamInlet inside LslInletRecorder end-to-end
into an XDF file.
"""
from __future__ import annotations

import threading
import time
import uuid

import numpy as np
import pylsl
import pytest
import pyxdf

from recorder.lsl.lsl_inlet_recorder import LslInletRecorder
from recorder.xdf.xdf_writer import XDFWriter


def load_by_name(path: str, **kwargs) -> dict:
    kwargs.setdefault("synchronize_clocks", False)
    kwargs.setdefault("dejitter_timestamps", False)
    streams, _ = pyxdf.load_xdf(path, **kwargs)
    return {s["info"]["name"][0]: s for s in streams}


def create_mock_outlet(name: str, n_channels: int, srate: float) -> pylsl.StreamOutlet:
    """Create and open a real LSL outlet, resolvable on the network under `name`."""
    info = pylsl.StreamInfo(
        name=name,
        type="EEG", 
        channel_count=n_channels,
        nominal_srate=srate,
        channel_format="float32",
        source_id=f"mock_{name}",
    )
    return pylsl.StreamOutlet(info)


def push_samples(
        outlet: pylsl.StreamOutlet,
        n_channels: int,
        srate: float,
        n_samples: int,
    ) -> threading.Thread:
    """Push `n_samples` samples to `outlet` at `srate` Hz from a daemon thread."""
    def _run():
        period = 1.0 / srate
        for i in range(n_samples):
            outlet.push_sample([float(i)] * n_channels)
            time.sleep(period)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


@pytest.fixture
def xdf_path(tmp_path):
    return str(tmp_path / "test.xdf")


def test_lsl_inlet_recorder_receives_real_outlet(xdf_path):
    stream_name = f"TestMockEEG-{uuid.uuid4().hex[:8]}"
    n_channels = 4
    srate = 100.0
    n_samples = 30
    outlet = create_mock_outlet(stream_name, n_channels, srate)

    resolved = pylsl.resolve_byprop("name", stream_name, timeout=10)
    assert resolved, f"failed to resolve mock outlet <{stream_name}> over real LSL"
    stream_info = resolved[0]

    w = XDFWriter(xdf_path, clock_offset_interval_s=1.0)
    w.start()
    sid = w.add_lsl_stream(
        name=stream_info.name(),
        stype=stream_info.type(),
        channel_count=stream_info.channel_count(),
        srate=stream_info.nominal_srate(),
        fmt="float32",
        source_id=stream_info.source_id(),
        key=f"lsl:{stream_info.uid()}",
    )

    logs = []
    rec = LslInletRecorder(
        stream_info=stream_info,
        stream_id=sid,
        xdf_writer=w,
        clock_offset_interval_s=1.0,
        status_cb=lambda msg, loglevel: logs.append((loglevel, msg)),
    )
    # Force the data connection to be live before any samples are pushed
    rec.inlet.open_stream(timeout=5.0)
    rec.start()
    try:
        push_samples(outlet, n_channels, srate, n_samples)
        # Enough time for all pushed samples to arrive plus at least one
        # real time_correction() round trip
        time.sleep(n_samples / srate + 2.0)
    finally:
        rec.stop()
    w.stop()

    eeg = load_by_name(xdf_path)[stream_name]
    assert eeg["time_series"].shape[0] == n_samples
    assert eeg["time_series"].shape[1] == n_channels
    np.testing.assert_allclose(eeg["time_series"][:, 0], np.arange(n_samples, dtype=np.float64))

    # A real time_correction() measurement landed via the offset thread.
    assert eeg["clock_values"], "no clock offset was measured against the real outlet"
    assert not any(loglevel == "WARNING" for loglevel, _ in logs)
