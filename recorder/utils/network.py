from __future__ import annotations

import socket
import sys
from typing import Optional

LOOPBACK_HOST = "127.0.0.1"


def get_local_ip() -> Optional[str]:
    """This machine's primary (default-route) IPv4 address, or None if it has no
    usable non-loopback one. Connecting a UDP socket sends no packets; it only
    makes the OS pick the outgoing interface, whose address we then read back."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip


def default_labrecorder_host(platform: Optional[str] = None) -> str:
    """Default LabRecorder RCS host. On Linux a locally running LabRecorder was
    found to accept connections on the machine's own IP address (as printed by
    `hostname -I`) rather than on 127.0.0.1, so use that there; other platforms
    keep the loopback address."""
    platform = sys.platform if platform is None else platform
    if platform.startswith("linux"):
        return get_local_ip() or LOOPBACK_HOST
    return LOOPBACK_HOST
