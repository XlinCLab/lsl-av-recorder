from __future__ import annotations

from typing import Any, Dict, List

import sounddevice as sd


def list_input_devices() -> List[Dict[str, Any]]:
    devs = sd.query_devices()
    out = []
    for i, d in enumerate(devs):
        if d.get("max_input_channels", 0) > 0:
            out.append({"index": i, "name": d.get("name"), "hostapi": d.get("hostapi")})
    return out

def default_input_device_index():
    try:
        di = sd.default.device[0]
        # PortAudio uses -1 (rather than None) to signal "no default device",
        # which notably happens on some Windows machines with no configured
        # default recording device.
        return di if di is not None and di >= 0 else None
    except Exception:
        return None
