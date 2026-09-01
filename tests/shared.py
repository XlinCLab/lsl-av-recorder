"""Constants, functions, or classes shared by multiple pytest modules."""

import pyxdf

from recorder.lsl import lsl_inlet_recorder
from recorder.xdf.xdf_writer import XDFWriter

MARKER_STREAM_NAME = "Dummy Marker Stream"

def _patch_inlet(monkeypatch, inlet):
    """Make LslInletRecorder.__init__ build `inlet` instead of a live one."""
    monkeypatch.setattr(lsl_inlet_recorder, "StreamInlet", lambda *a, **k: inlet)


def add_eeg_stream(writer: XDFWriter, key=None):
    return writer.add_lsl_stream(
        name="EEG",
        stype="EEG",
        channel_count=1,
        srate=250.0,
        fmt="float32",
        source_id="eeg",
        key=key,
    )

def load_by_name(path: str, **kwargs) -> dict:
    """Load an XDF file and return {stream_name: stream_dict}.

    Defaults to the *raw* view (no clock sync / dejitter) so tests can inspect
    the clock offsets and unmodified timestamps the writer actually produced.
    """
    kwargs.setdefault("synchronize_clocks", False)
    kwargs.setdefault("dejitter_timestamps", False)
    streams, _ = pyxdf.load_xdf(path, **kwargs)
    return {s["info"]["name"][0]: s for s in streams}
