"""macOS camera capability querying via native AVFoundation (PyObjC).

Replaces former ffmpeg-based capability probing on macOS.
ffmpeg's avfoundation accepts some resolution/fps/pixel_format combinations that don't
correspond to any format the device actually exposes, and silently lets the
driver substitute a different (often lower) frame rate while still reporting
success. Reading AVCaptureDevice.formats() directly reports only
combinations the device itself declares, so it cannot produce that kind of
false positive.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..video.constants import COMMON_FPS_VALUES

# CVPixelFormatType FourCC -> UI pixel-format label mapping;
# an unrecognized FourCC falls back to its raw (uppercased) 4-character code
_FOURCC_TO_PIXEL_FORMAT = {
    "yuvs": "YUYV",
    "yuv2": "YUYV",
    "2vuy": "UYVY",
    "420v": "NV12",
    "420f": "NV12",
    "bgra": "BGRA",
    "jpeg": "MJPG",
    "dmb1": "MJPG",
}


def _decode_fourcc(fourcc_int: int) -> str:
    chars = [chr((fourcc_int >> shift) & 0xFF) for shift in (24, 16, 8, 0)]
    decoded = "".join(c for c in chars if c.isprintable())
    return decoded or f"0x{fourcc_int:08x}"


def pixel_format_label(fourcc_int: int) -> str:
    raw = _decode_fourcc(fourcc_int)
    return _FOURCC_TO_PIXEL_FORMAT.get(raw.lower(), raw.upper())


def _expand_frame_rate_range(min_fps: float, max_fps: float) -> List[int]:
    """Convert one AVFrameRateRange into the discrete fps values to offer.

    Some cameras report every supported rate as their own range
    with min == max : one AVCaptureDeviceFormat per discrete rate.
    Others report one genuine continuous range (e.g. 15-30).
    For a discrete range there is exactly one supported value.
    For a continuous range, only intersect it with the
    human-recognizable COMMON_FPS_VALUES rather than exposing raw float
    bounds or guessing untested intermediate values the camera never
    explicitly reported.
    """
    lo, hi = sorted((int(round(min_fps)), int(round(max_fps))))
    if lo == hi:
        return [lo]
    # Always include the reported upper and lower bounds
    values_to_check = sorted(list(set(COMMON_FPS_VALUES + [lo, hi])))
    values = [f for f in values_to_check if lo <= f <= hi]
    return values or [lo, hi]


def _build_modes_by_format(
    format_entries: Iterable[Tuple[int, int, int, List[Tuple[float, float]]]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Pure transform of (fourcc_int, width, height, frame_rate_ranges)
    tuples per AVCaptureDeviceFormat into the modes_by_format shape used elsewhere."""
    modes: Dict[str, Dict[Tuple[int, int], set]] = {}
    for fourcc_int, width, height, fps_ranges in format_entries:
        if not width or not height:
            continue
        fps_values: set = set()
        for min_fps, max_fps in fps_ranges:
            fps_values.update(_expand_frame_rate_range(min_fps, max_fps))
        if not fps_values:
            continue
        fmt = pixel_format_label(fourcc_int)
        modes.setdefault(fmt, {}).setdefault((width, height), set()).update(fps_values)
    return {
        fmt: [
            {"width": w, "height": h, "fps": sorted(fps_values)}
            for (w, h), fps_values in sorted(res_map.items())
        ]
        for fmt, res_map in modes.items()
    }


def _resolve_device_position(
    names: List[str], device_name: Optional[str], fallback_index: Optional[int]
) -> Optional[int]:
    """Find which position in a device list to use, preferring a name match
    (robust to enumeration order differing between AVFoundation call sites)."""
    if device_name:
        for i, name in enumerate(names):
            if name == device_name:
                return i
    if fallback_index is not None and 0 <= fallback_index < len(names):
        return fallback_index
    return None


def _mode_matches(mode: Dict[str, Any], width, height, fps) -> bool:
    if width is not None and int(mode["width"]) != int(width):
        return False
    if height is not None and int(mode["height"]) != int(height):
        return False
    if fps is not None and int(round(fps)) not in {int(f) for f in mode["fps"]}:
        return False
    return True


def modes_match(
    modes_by_format: Dict[str, List[Dict[str, Any]]],
    width,
    height,
    fps,
    pixel_format: Optional[str] = None,
) -> bool:
    """Checks whether any entry in `modes_by_format` matches
    the exact settings combination."""
    if not modes_by_format:
        # Fail by default if no modes passed
        return False
    candidates = (
        modes_by_format.get(str(pixel_format).upper(), [])
        if pixel_format
        else [m for modes in modes_by_format.values() for m in modes]
    )
    return any(_mode_matches(m, width, height, fps) for m in candidates)


def _discovered_devices() -> list:
    import AVFoundation

    session = AVFoundation.AVCaptureDeviceDiscoverySession.discoverySessionWithDeviceTypes_mediaType_position_(
        [
            AVFoundation.AVCaptureDeviceTypeBuiltInWideAngleCamera,
            AVFoundation.AVCaptureDeviceTypeExternalUnknown,
        ],
        AVFoundation.AVMediaTypeVideo,
        AVFoundation.AVCaptureDevicePositionUnspecified,
    )
    return list(session.devices())


def probe_avfoundation_capabilities(
    device_index: Optional[int], device_name: Optional[str] = None
) -> Dict[str, List[Dict[str, Any]]]:
    """Query every (pixel_format, width, height) -> supported fps values combination
    the device natively exposes, by reading AVCaptureDevice.formats() directly."""
    import AVFoundation

    devices = _discovered_devices()
    names = [str(d.localizedName()) for d in devices]
    position = _resolve_device_position(names, device_name, device_index)
    if position is None:
        return {}
    device = devices[position]

    format_entries = []
    for format_profile in device.formats():
        desc = format_profile.formatDescription()
        fourcc_int = AVFoundation.CMFormatDescriptionGetMediaSubType(desc)
        dims = AVFoundation.CMVideoFormatDescriptionGetDimensions(desc)
        width, height = int(dims.width), int(dims.height)
        ranges = [
            (fps_range.minFrameRate(), fps_range.maxFrameRate())
            for fps_range in format_profile.videoSupportedFrameRateRanges()
        ]
        format_entries.append((fourcc_int, width, height, ranges))

    return _build_modes_by_format(format_entries)


def mode_supported(
    device_index: Optional[int],
    width: Optional[int],
    height: Optional[int],
    fps,
    pixel_format: Optional[str] = None,
    device_name: Optional[str] = None,
) -> bool:
    """Returns True if this exact (width, height, fps[, pixel_format])
    combination matches a natively reported AVCaptureDeviceFormat."""
    modes_by_format = probe_avfoundation_capabilities(
        device_index=device_index,
        device_name=device_name,
    )
    return modes_match(
        modes_by_format=modes_by_format,
        width=width,
        height=height,
        fps=fps,
        pixel_format=pixel_format,
    )
