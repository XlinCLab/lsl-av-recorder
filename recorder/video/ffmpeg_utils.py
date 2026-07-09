import re
import subprocess
from typing import List, Tuple

from ..video.constants import (DEFAULT_CAMERA_FPS, FFMPEG_PROBE_DURATION_SEC,
                               FFMPEG_PROBE_TIMEOUT_SEC, PIXEL_FORMAT_MAP)


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
    try:
        proc = subprocess.run(
            cmd, check=False, capture_output=True, text=True,
            timeout=FFMPEG_PROBE_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return False, "probe timed out"
    text = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, text


def _parse_supported_modes(text: str) -> List[Tuple[int, int, list[int]]]:
    modes: List[Tuple[int, int, list[int]]] = []
    for line in text.splitlines():
        m = re.search(r"(\d+)x(\d+)@\[(.+)\]fps", line)
        if not m:
            continue
        width = int(m.group(1))
        height = int(m.group(2))
        fps_values = sorted(
            {
                int(round(float(f)))
                for f in re.findall(r"[\d.]+", m.group(3))
                if float(f) > 0
            }
        )
        modes.append((width, height, fps_values))
    return modes


def _get_supported_modes(device: str) -> List[Tuple[int, int, list[int]]]:
    # Force an unsupported framerate so AVFoundation prints supported modes.
    _, text = _ffmpeg_avfoundation_probe(device, ["-framerate", "1000"])
    return _parse_supported_modes(text)


def _pixel_format_probe_succeeded(text: str, ok: bool) -> bool:
    if not ok:
        return False
    lowered = text.lower()
    if "overriding selected pixel format" in lowered:
        return False
    if "pixel format" in lowered and "not supported" in lowered:
        return False
    return True


def _build_mode_args(width: int | None, height: int | None, fps: int) -> list[str]:
    args: list[str] = []
    if width is not None and height is not None:
        args += ["-video_size", f"{width}x{height}"]
    args += ["-framerate", str(fps)]
    return args


def _probe_mac_supported_ui_formats(
    device: str,
    modes: List[Tuple[int, int, list[int]]] | None = None,
    progress_cb=None,
) -> list[str]:
    modes = modes if modes is not None else _get_supported_modes(device)
    mode_probe_args: list[list[str]] = []
    if modes:
        for width, height, fps_values in modes:
            for fps in (fps_values or [DEFAULT_CAMERA_FPS]):
                mode_probe_args.append(_build_mode_args(width, height, fps))
    else:
        mode_probe_args.append(_build_mode_args(None, None, DEFAULT_CAMERA_FPS))

    supported: list[str] = []
    # Report progress per individual probe attempt (format x mode), not just
    # once per pixel format, since a single format can require looping over
    # many mode/fps combinations before it can be marked supported or not.
    total_probes = max(1, len(PIXEL_FORMAT_MAP) * len(mode_probe_args))
    completed = 0
    for ui_fmt, ff_fmt in PIXEL_FORMAT_MAP.items():
        for mode_args in mode_probe_args:
            ok, text = _ffmpeg_avfoundation_probe(
                device,
                mode_args + ["-pixel_format", ff_fmt],
            )
            completed += 1
            if progress_cb:
                try:
                    progress_cb(completed, total_probes, ui_fmt)
                except Exception:
                    pass
            if _pixel_format_probe_succeeded(text, ok):
                supported.append(ui_fmt)
                break
    return sorted(set(supported))


def _probe_mac_supported_fps(modes: List[Tuple[int, int, list[int]]]) -> list[int]:
    if not modes:
        return []
    return sorted({fps for _, _, fps_values in modes for fps in fps_values})


def probe_avfoundation_mode(
    device: str,
    width: int | None,
    height: int | None,
    fps: int,
    pixel_format: str | None = None,
) -> bool:
    args = _build_mode_args(width, height, fps)
    if pixel_format:
        args += ["-pixel_format", pixel_format]
    ok, text = _ffmpeg_avfoundation_probe(device, args)
    if pixel_format:
        return _pixel_format_probe_succeeded(text, ok)
    return ok


# DirectShow reports one line per supported (format, resolution) pair, e.g.:
#   vcodec=mjpeg  min s=640x480 fps=5 max s=640x480 fps=30
#   pixel_format=yuyv422  min s=640x480 fps=5 max s=640x480 fps=30
_DSHOW_OPTION_RE = re.compile(
    r"(?:vcodec|pixel_format)=(?P<fmt>\S+)\s+"
    r"min\s+s=(?P<minw>\d+)x(?P<minh>\d+)\s+fps=(?P<minfps>[\d.]+)\s+"
    r"max\s+s=(?P<maxw>\d+)x(?P<maxh>\d+)\s+fps=(?P<maxfps>[\d.]+)"
)


def _ffmpeg_dshow_list_options(device: str) -> tuple[bool, str]:
    cmd = [
        "ffmpeg",
        "-v", "warning",
        "-f", "dshow",
        "-list_options", "true",
        "-i", f"video={device}",
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    text = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, text


def _parse_dshow_options(text: str) -> List[dict]:
    """Parse `ffmpeg -f dshow -list_options true` output into per-mode entries."""
    entries: List[dict] = []
    for line in text.splitlines():
        m = _DSHOW_OPTION_RE.search(line)
        if not m:
            continue
        fps_values = sorted(
            {int(round(float(m.group("minfps")))), int(round(float(m.group("maxfps"))))}
        )
        fps_values = [f for f in fps_values if f > 0]
        sizes = {(int(m.group("minw")), int(m.group("minh"))), (int(m.group("maxw")), int(m.group("maxh")))}
        for width, height in sizes:
            entries.append(
                {
                    "format": m.group("fmt").lower(),
                    "width": width,
                    "height": height,
                    "fps": fps_values,
                }
            )
    return entries
