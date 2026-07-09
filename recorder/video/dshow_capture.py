"""Windows video device enumeration and capability probing via DirectShow.

Talks to DirectShow directly through COM (via the `pygrabber` package) rather than
shelling out to ffmpeg's dshow input. ffmpeg's dshow device-address parsing turned
out to have a bug (at least as of ffmpeg 8.1.2) where any device name or path
containing spaces or characters like `&`/`#`/`{}` -- i.e. almost every real camera --
could not be addressed at all, making device enumeration and capability probing
silently return nothing. Talking to DirectShow's COM interfaces directly sidesteps
that string-parsing layer entirely: devices are addressed by their enumeration
index, matching what `cv2.VideoCapture(index, cv2.CAP_DSHOW)` already uses to open
the camera for recording.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, List


@contextmanager
def _com_session():
    """Ensure COM is initialized on the calling thread for the duration of the block.

    comtypes only auto-initializes COM for whichever thread first imports it
    (normally the main thread, at app startup). Capability probing runs on a
    background QThread, which needs its own explicit CoInitialize/CoUninitialize.
    """
    import comtypes

    comtypes.CoInitialize()
    try:
        yield
    finally:
        comtypes.CoUninitialize()


def _build_device_list(names: List[str]) -> List[Dict[str, Any]]:
    return [{"index": i, "name": name, "devnode": str(i)} for i, name in enumerate(names)]


def list_windows_video_devices() -> List[Dict[str, Any]]:
    with _com_session():
        from pygrabber.dshow_graph import FilterGraph

        graph = FilterGraph()
        names = graph.get_input_devices()
        del graph  # release COM references before CoUninitialize runs
    return _build_device_list(names)


def _build_capabilities_from_formats(formats: List[Dict[str, Any]]) -> Dict[str, Any]:
    caps: Dict[str, Any] = {
        "pixel_formats": [],
        "fps": [],
        "modes": [],
        # Brightness/hue/saturation and auto-exposure/auto-focus would need
        # IAMVideoProcAmp/IAMCameraControl (not exposed by pygrabber); camera
        # control application is not yet implemented on Windows at all (see
        # set_camera_controls/set_frame_rate), so these stay at their unsupported
        # defaults for now.
        "supports_auto_exposure": False,
        "supports_auto_focus": False,
        "brightness_range": None,
        "brightness_default": None,
        "hue_range": None,
        "hue_default": None,
        "saturation_range": None,
        "saturation_default": None,
    }
    mode_fps: dict[tuple[int, int], set[int]] = {}
    mode_fps_by_format: dict[str, dict[tuple[int, int], set[int]]] = {}
    pixel_formats: set[str] = set()

    for fmt in formats:
        width, height = int(fmt["width"]), int(fmt["height"])
        if width <= 0 or height <= 0:
            continue
        mode = (width, height)
        fps_values = {int(round(fmt["min_framerate"])), int(round(fmt["max_framerate"]))}
        fps_values = {f for f in fps_values if f > 0}
        pixel_format = str(fmt["media_type_str"])
        pixel_formats.add(pixel_format)
        mode_fps.setdefault(mode, set()).update(fps_values)
        mode_fps_by_format.setdefault(pixel_format, {}).setdefault(mode, set()).update(fps_values)

    caps["pixel_formats"] = sorted(pixel_formats)
    caps["fps"] = sorted({f for values in mode_fps.values() for f in values})
    caps["modes"] = [
        {"width": w, "height": h, "fps": sorted(values)}
        for (w, h), values in sorted(mode_fps.items())
        if values
    ]
    if mode_fps_by_format:
        caps["modes_by_format"] = {
            fmt: [
                {"width": w, "height": h, "fps": sorted(values)}
                for (w, h), values in sorted(modes.items())
                if values
            ]
            for fmt, modes in mode_fps_by_format.items()
        }
    return caps


def get_windows_camera_capabilities(device_index: int) -> Dict[str, Any]:
    with _com_session():
        from pygrabber.dshow_graph import FilterGraph

        graph = FilterGraph()
        graph.add_video_input_device(device_index)
        formats = graph.get_input_device().get_formats()
        del graph  # release COM references before CoUninitialize runs
    return _build_capabilities_from_formats(formats)


def _find_format_index(
    formats: List[Dict[str, Any]],
    width: int,
    height: int,
    pixel_format: str,
    fps: float | None = None,
) -> int | None:
    candidates = [
        fmt
        for fmt in formats
        if int(fmt["width"]) == int(width)
        and int(fmt["height"]) == int(height)
        and str(fmt["media_type_str"]).upper() == str(pixel_format).upper()
    ]
    if not candidates:
        return None
    if fps:
        for fmt in candidates:
            if fmt["min_framerate"] <= fps <= fmt["max_framerate"]:
                return int(fmt["index"])
    return int(candidates[0]["index"])


def set_windows_camera_format(
    device_index: int,
    width: int,
    height: int,
    pixel_format: str,
    fps: float | None = None,
) -> bool:
    """Configure the DirectShow capture pin's format directly via IAMStreamConfig,
    before OpenCV opens the device -- mirroring how `v4l2-ctl` pre-configures the
    device on Linux ahead of `cv2.VideoCapture`.

    Note: `fps` is only used to pick between multiple stream-caps entries that
    share the same resolution/pixel format but report different frame-rate ranges;
    the rate actually applied is whichever nominal rate is embedded in the matched
    entry's media type (typically that entry's maximum), not necessarily the exact
    requested value.
    """
    with _com_session():
        from pygrabber.dshow_graph import FilterGraph

        graph = FilterGraph()
        graph.add_video_input_device(device_index)
        video_input = graph.get_input_device()
        formats = video_input.get_formats()
        match_index = _find_format_index(formats, width, height, pixel_format, fps)
        if match_index is None:
            del graph
            return False
        video_input.set_format(match_index)
        del graph  # release COM references before CoUninitialize runs
    return True
