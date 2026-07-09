from __future__ import annotations

import re
from typing import Any, Dict, List

from ..utils.utils import run_capture_cmd
from ..video.constants import IS_LINUX, IS_MAC, IS_WINDOWS


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


def _parse_windows_dshow_video_devices(text: str) -> List[Dict[str, Any]]:
    devices: List[Dict[str, Any]] = []
    in_video_section = False
    current: Dict[str, Any] | None = None
    for raw_line in text.splitlines():
        # Strip the "[dshow @ 0x...]" prefix ffmpeg prepends to every line.
        line = re.sub(r"^\[dshow @ [^\]]+\]\s*", "", raw_line.strip())
        if "DirectShow video devices" in line:
            in_video_section = True
            continue
        if "DirectShow audio devices" in line:
            break
        if not in_video_section:
            continue
        alt_match = re.search(r'Alternative name\s+"(.+)"', line)
        if alt_match and current is not None:
            current["alt_name"] = alt_match.group(1)
            continue
        name_match = re.match(r'^"(.+)"$', line)
        if name_match:
            if current is not None:
                devices.append(current)
            current = {"name": name_match.group(1)}
    if current is not None:
        devices.append(current)

    result: List[Dict[str, Any]] = []
    for idx, dev in enumerate(devices):
        # Prefer the unique DirectShow "alternative name" (device path) as the devnode
        # since friendly names are not guaranteed unique across identical cameras.
        # ffmpeg expects unquoted device paths but quoted friendly names for "-i video=...".
        alt_name = dev.get("alt_name")
        devnode = alt_name if alt_name else f'"{dev["name"]}"'
        result.append(
            {
                "index": idx,
                "name": dev["name"],
                "devnode": devnode,
            }
        )
    return result


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
        text, _ = run_capture_cmd(
            ["ffmpeg", "-f", "dshow", "-list_devices", "true", "-i", "dummy"],
            check=False,
        )
        return _parse_windows_dshow_video_devices(text)

    return []
