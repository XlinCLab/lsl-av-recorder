import re
import subprocess

from ..video.constants import (DEFAULT_FPS_CANDIDATES, FFMPEG_PIXEL_FORMATS,
                               FFMPEG_PROBE_DURATION_SEC,
                               INV_MAC_PIXEL_FORMAT_MAP, MAC_PIXEL_FORMAT_MAP)


def _ffmpeg_avfoundation_probe(device: str, extra_args: list[str]) -> tuple[bool, str]:
    cmd = [
        "ffmpeg",
        "-v", "warning",
        "-f", "avfoundation",
        *extra_args,
        "-i", f"{device}:none",
        "-t", str(FFMPEG_PROBE_DURATION_SEC),
        "-f", "null",
        "-",
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    text = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, text


def _parse_supported_pixel_formats(text: str) -> list[str]:
    pixel_formats: list[str] = []
    in_pf_block = False
    for line in text.splitlines():
        if "Supported pixel formats:" in line:
            in_pf_block = True
            continue
        if not in_pf_block:
            continue
        m = re.search(r"\b([a-z0-9]{4,})\b$", line.strip())
        if m:
            mapped = INV_MAC_PIXEL_FORMAT_MAP.get(m.group(1).lower())
            if mapped and mapped not in pixel_formats:
                pixel_formats.append(mapped)
            continue
        if "Overriding selected pixel format" in line or "Could not" in line:
            break
    return pixel_formats


def _probe_mac_supported_ui_formats(device: str) -> list[str]:
    # AVFoundation prints supported formats only when the requested format
    # is valid for ffmpeg but unsupported by the camera.
    for candidate in FFMPEG_PIXEL_FORMATS:
        _, text = _ffmpeg_avfoundation_probe(device, ["-pixel_format", candidate])
        parsed = _parse_supported_pixel_formats(text)
        if parsed:
            return sorted(set(parsed))

    # Fallback: directly test only UI-supported format options.
    supported: list[str] = []
    for ui_fmt, ff_fmt in MAC_PIXEL_FORMAT_MAP.items():
        ok, _ = _ffmpeg_avfoundation_probe(device, ["-pixel_format", ff_fmt])
        if ok:
            supported.append(ui_fmt)
    return sorted(set(supported))


def _probe_mac_supported_fps(device: str, supported_ui_formats: list[str]) -> list[int]:
    probe_pf = None
    for ui_fmt in supported_ui_formats:
        probe_pf = MAC_PIXEL_FORMAT_MAP.get(ui_fmt)
        if probe_pf:
            break

    fps_supported: list[int] = []
    for fps in DEFAULT_FPS_CANDIDATES:
        args = ["-framerate", str(fps)]
        if probe_pf:
            args = ["-pixel_format", probe_pf] + args
        ok, _ = _ffmpeg_avfoundation_probe(device, args)
        if ok:
            fps_supported.append(fps)
    return fps_supported
