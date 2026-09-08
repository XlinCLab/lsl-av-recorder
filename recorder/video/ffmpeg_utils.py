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
    """Parse ffmpeg's forced-error "Supported modes" dump into one entry per
    resolution, with every reported fps value for that resolution merged
    together.

    AVFoundation devices report this in more than one shape:
      - a single line per resolution with a genuine-looking range, e.g.
        "1280x720@[15.000000 30.000000]fps"
      - multiple lines for the same resolution, each a single-value "range"
        (min == max) -- one line per discrete rate the device actually supports,
        e.g. "176x144@[30.000030 30.000030]fps", "176x144@[24.000038 24.000038]fps"
        This probe doesn't expose which pixel format each line belongs to,
        so the same resolution can also appear once per pixel format that supports it.
    """
    fps_by_resolution: dict[tuple[int, int], set[int]] = {}
    order: list[tuple[int, int]] = []
    for line in text.splitlines():
        m = re.search(r"(\d+)x(\d+)@\[(.+)\]fps", line)
        if not m:
            continue
        resolution = (int(m.group(1)), int(m.group(2)))
        if resolution not in fps_by_resolution:
            fps_by_resolution[resolution] = set()
            order.append(resolution)
        fps_by_resolution[resolution].update(
            int(round(float(f)))
            for f in re.findall(r"[\d.]+", m.group(3))
            if float(f) > 0
        )
    return [(w, h, sorted(fps_by_resolution[(w, h)])) for w, h in order]


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


def _probe_mac_modes_by_format(
    device: str,
    candidate_modes: List[Tuple[int, int, list[int]]] | None = None,
    progress_cb=None,
) -> dict[str, List[Tuple[int, int, list[int]]]]:
    """Exhaustively verify every (resolution, fps) candidate against every
    pixel format, by actually attempting to open the device at that exact
    combination, and return only the combinations ffmpeg confirms it can
    open, grouped by pixel format.
    """
    candidate_modes = (
        candidate_modes if candidate_modes is not None else _get_supported_modes(device)
    )
    candidates: list[tuple[int | None, int | None, int]] = []
    if candidate_modes:
        for width, height, fps_values in candidate_modes:
            for fps in (fps_values or [DEFAULT_CAMERA_FPS]):
                candidates.append((width, height, fps))
    else:
        candidates.append((None, None, DEFAULT_CAMERA_FPS))

    # Report progress per individual probe attempt (format x candidate), since
    # every candidate must be tested for every format; there is no shortcut
    # that can skip any of them without risking a false "supported" result
    total_probes = max(1, len(PIXEL_FORMAT_MAP) * len(candidates))
    completed = 0
    verified: dict[str, dict[tuple[int | None, int | None], set[int]]] = {}
    for ui_fmt, ff_fmt in PIXEL_FORMAT_MAP.items():
        for width, height, fps in candidates:
            args = _build_mode_args(width, height, fps) + ["-pixel_format", ff_fmt]
            ok, text = _ffmpeg_avfoundation_probe(device, args)
            completed += 1
            if progress_cb:
                try:
                    progress_cb(completed, total_probes, ui_fmt)
                except Exception:
                    pass
            if _pixel_format_probe_succeeded(text, ok):
                verified.setdefault(ui_fmt, {}).setdefault((width, height), set()).add(fps)

    return {
        fmt: [
            (w, h, sorted(fps_values))
            for (w, h), fps_values in sorted(
                res_map.items(), key=lambda item: (item[0][0] or 0, item[0][1] or 0)
            )
        ]
        for fmt, res_map in verified.items()
    }


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
