from __future__ import annotations

import re
from typing import Any, Dict, List

from ..utils.utils import run_capture_cmd
from ..video.constants import IS_LINUX, IS_MAC, IS_WINDOWS
from ..video.dshow_capture import list_windows_video_devices


def _parse_mac_avfoundation_video_devices(text: str) -> List[Dict[str, Any]]:
    devices: List[Dict[str, Any]] = []
    in_video_section = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if "AVFoundation video devices:" in line:
            in_video_section = True
            continue
        if "AVFoundation audio devices:" in line:
            break
        if not in_video_section:
            continue
        m = re.search(r"\[(\d+)\]\s+(.+)$", line)
        if not m:
            continue
        idx = int(m.group(1))
        name = m.group(2).strip()
        devices.append(
            {
                "index": idx,
                "name": name,
                "devnode": str(idx),
            }
        )
    return devices


def _parse_linux_v4l2_devices(text: str) -> List[Dict[str, Any]]:
    devices: List[Dict[str, Any]] = []
    current_label = ""
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.endswith(":") and not stripped.startswith("/dev/"):
            current_label = stripped[:-1].strip()
            continue
        m = re.search(r"(/dev/video(\d+))", stripped)
        if not m:
            continue
        devnode = m.group(1)
        index = int(m.group(2))
        name = current_label or f"Video Device {index}"
        devices.append(
            {
                "index": index,
                "name": name,
                "devnode": devnode,
            }
        )
    return devices


def list_video_devices() -> List[Dict[str, Any]]:
    if IS_MAC:
        text, _ = run_capture_cmd(
            ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            check=False,
        )
        return _parse_mac_avfoundation_video_devices(text)

    if IS_LINUX:
        text, _ = run_capture_cmd(["v4l2-ctl", "--list-devices"], check=False)
        return _parse_linux_v4l2_devices(text)

    if IS_WINDOWS:
        return list_windows_video_devices()

    return []
