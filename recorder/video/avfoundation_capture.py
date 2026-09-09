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


def _select_frame_rate_range(ranges: List[Tuple[float, float]], fps: float) -> Optional[int]:
    """Pick the index of the range (from however many AVCaptureDeviceFormat
    objects/frame-rate-ranges cover a given resolution/pixel_format) that
    covers `fps`, preferring the tightest one (lowest max fps) that still
    reaches it, e.g. for a camera reporting a separate discrete-rate format
    per fps, this picks the exact-match one rather than an unnecessarily
    higher-capability sibling. Returns None if nothing covers `fps`."""
    best_idx = None
    best_max = None
    for i, (lo, hi) in enumerate(ranges):
        if lo - 0.5 <= fps <= hi + 0.5:
            if best_max is None or hi < best_max:
                best_max = hi
                best_idx = i
    return best_idx


def _find_native_format_and_range(
        device,
        width: int,
        height: int,
        fps: float,
        pixel_format: Optional[str],
    ):
    """Among this device's native AVCaptureDeviceFormats, find the one
    matching width/height[/pixel_format] together with the specific
    AVFrameRateRange (of possibly several such formats) that best covers
    `fps`. Returns (format, frame_rate_range) or None."""
    import AVFoundation

    candidates = []
    for fmt in device.formats():
        desc = fmt.formatDescription()
        dims = AVFoundation.CMVideoFormatDescriptionGetDimensions(desc)
        if int(dims.width) != width or int(dims.height) != height:
            continue
        if pixel_format is not None:
            fourcc_int = AVFoundation.CMFormatDescriptionGetMediaSubType(desc)
            if pixel_format_label(fourcc_int) != str(pixel_format).upper():
                continue
        for frame_rate_range in fmt.videoSupportedFrameRateRanges():
            candidates.append((fmt, frame_rate_range))

    ranges = [(r.minFrameRate(), r.maxFrameRate()) for _, r in candidates]
    idx = _select_frame_rate_range(ranges=ranges, fps=fps)
    return candidates[idx] if idx is not None else None


def force_active_format(
    device_index: Optional[int],
    width: int,
    height: int,
    fps: float,
    pixel_format: Optional[str] = None,
    device_name: Optional[str] = None,
) -> bool:
    """Force the physical camera onto the exact native AVCaptureDeviceFormat
    matching width/height/fps[/pixel_format], bypassing cv2's own capture
    negotiation. Returns True if the format was found and applied.

    Must be called AFTER cv2.VideoCapture has already opened the device and
    its capture session has started running -- calling this before the
    session starts has no effect, since AVCaptureSession renegotiates the
    active format according to its own preset as soon as it starts running.

    This works because AVCaptureDevice.activeFormat is a property of the
    physical device, not of whichever AVCaptureSession happens to be
    attached to it; cv2's AVFoundation backend never calls setActiveFormat
    itself after opening (it only adjusts min/max frame duration within
    whatever format is already active, and hardcodes delivered frames to
    BGRA regardless of the native format), so forcing it via this second,
    independent device reference persists for the rest of cv2's capture
    session.
    """
    devices = _discovered_devices()
    names = [str(d.localizedName()) for d in devices]
    position = _resolve_device_position(
        names=names,
        device_name=device_name,
        fallback_index=device_index
    )
    if position is None:
        return False
    device = devices[position]

    found = _find_native_format_and_range(
        device=device,
        width=width,
        height=height,
        fps=fps,
        pixel_format=pixel_format,
    )
    if found is None:
        return False
    target_format, _target_range = found

    # Build the frame duration from the requested fps directly
    import AVFoundation
    desired_duration = AVFoundation.CMTimeMake(1, int(round(fps)))

    ok, _err = device.lockForConfiguration_(None)
    if not ok:
        return False
    try:
        device.setActiveFormat_(target_format)
        device.setActiveVideoMinFrameDuration_(desired_duration)
        device.setActiveVideoMaxFrameDuration_(desired_duration)
    finally:
        device.unlockForConfiguration()
    return True
