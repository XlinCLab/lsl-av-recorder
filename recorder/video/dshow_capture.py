"""Windows video device enumeration, capability probing, and frame capture via
DirectShow.

Talks to DirectShow directly through COM (via the `pygrabber` package) rather than
shelling out to ffmpeg's dshow input. ffmpeg's dshow device-address parsing turned
out to have a bug (at least as of ffmpeg 8.1.2) where any device name or path
containing spaces or characters like `&`/`#`/`{}` -- i.e. almost every real camera --
could not be addressed at all, making device enumeration and capability probing
silently return nothing. Talking to DirectShow's COM interfaces directly sidesteps
that string-parsing layer entirely: devices are addressed by their enumeration
index, matching what `cv2.VideoCapture(index, cv2.CAP_DSHOW)` used to use to open
the camera for recording.

Frame capture also happens through this module now (`WindowsDShowVideoCapture`)
rather than `cv2.VideoCapture(..., cv2.CAP_DSHOW)`. Configuring the device's format
via `IAMStreamConfig::SetFormat` on one (temporary) filter graph and then opening a
*separate* graph via `cv2.VideoCapture` does not reliably carry the configured
format over on some drivers -- the device silently falls back to its default (often
a low-fps uncompressed) mode regardless of what was requested. Capturing frames
through the SAME graph that has the format applied avoids that handoff entirely.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Callable, Dict, List, Optional


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


def summarize_formats(formats: List[Dict[str, Any]], limit: int = 20) -> str:
    """Render a stream-caps list as a short, log-friendly summary of the unique
    (pixel format, resolution) combinations actually found on the device."""
    if not formats:
        return "none found (device may be busy/unreachable)"
    combos = sorted({f"{f['media_type_str']} {f['width']}x{f['height']}" for f in formats})
    shown = combos[:limit]
    suffix = f", ... ({len(combos) - limit} more)" if len(combos) > limit else ""
    return ", ".join(shown) + suffix


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


def _make_frame_callback(on_frame: Callable[[Any], None]):
    """Build an ISampleGrabberCB COM callback object that forwards *every*
    delivered buffer to `on_frame` as a BGR uint8 numpy array.

    Defined inside a function so the COM/numpy imports stay lazy (this module
    must stay importable on non-Windows platforms). pygrabber's own
    SampleGrabberCallback only fires on an explicit grab_frame() request (it's
    built for periodic snapshots); continuous video capture needs every frame.
    """
    from comtypes import COMObject
    import numpy as np
    from pygrabber.dshow_core import qedit

    class _ContinuousFrameCallback(COMObject):
        _com_interfaces_ = [qedit.ISampleGrabberCB]

        def __init__(self):
            self.width = 0
            self.height = 0
            super().__init__()

        def SampleCB(self, this, SampleTime, pSample):
            return 0

        def BufferCB(self, this, SampleTime, pBuffer, BufferLen):
            if self.width and self.height:
                try:
                    img = np.ctypeslib.as_array(pBuffer, shape=(self.height, self.width, 3))
                    # DirectShow RGB24 buffers are stored bottom-up; flip to
                    # top-down and reverse channels (RGB -> BGR) to match cv2's
                    # convention. Copy out before returning -- DirectShow reuses
                    # this buffer for the next frame.
                    img = np.ascontiguousarray(np.flip(img, axis=0)[:, :, ::-1])
                    on_frame(img)
                except Exception:
                    pass
            return 0

    return _ContinuousFrameCallback()


class WindowsDShowVideoCapture:
    """Continuous video capture on Windows via a persistent DirectShow filter
    graph (through `pygrabber`), used in place of `cv2.VideoCapture(..., CAP_DSHOW)`.

    Implements the small subset of cv2.VideoCapture's interface this app actually
    uses (isOpened/read/get/set/release) so it can be swapped in as a drop-in
    replacement for `self.cap` on Windows.

    Known caveats (untested on real hardware beyond initial verification):
    - The SampleGrabber callback is invoked directly by DirectShow's internal
      capture thread. This works in pygrabber's own shipped examples without an
      explicit Windows message pump, but if frames never arrive, COM apartment
      marshaling is the first thing to investigate.
    - `IGraphBuilder.Connect()` is expected to preserve the source pin's
      explicitly-selected format (set via IAMStreamConfig::SetFormat) and insert
      a decoder filter to bridge to the sample grabber's requested RGB24 output
      (e.g. Windows' built-in MJPEG decoder) -- this is standard DirectShow
      behavior but depends on an appropriate decoder being registered.
    """

    def __init__(
        self,
        device_index: int,
        width: int,
        height: int,
        pixel_format: Optional[str] = None,
        fps: Optional[float] = None,
        log_cb: Optional[Callable[[str, str], None]] = None,
    ):
        self._opened = False
        self._graph = None
        self._callback = None
        self._com_initialized = False
        self._frame_lock = threading.Lock()
        self._latest_frame = None
        self._frame_available = threading.Event()
        self.actual_width = int(width)
        self.actual_height = int(height)
        self._log_cb = log_cb

        import comtypes

        comtypes.CoInitialize()
        self._com_initialized = True
        try:
            from pygrabber.dshow_graph import FilterGraph, FilterType

            graph = FilterGraph()
            graph.add_video_input_device(device_index)

            if pixel_format:
                video_input = graph.get_input_device()
                formats = video_input.get_formats()
                match_index = _find_format_index(formats, width, height, pixel_format, fps)
                if match_index is not None:
                    video_input.set_format(match_index)
                else:
                    self._log(
                        "WARNING",
                        f"Could not select DirectShow format {pixel_format} "
                        f"{width}x{height}; falling back to driver default. "
                        f"Formats actually available: {summarize_formats(formats)}",
                    )

            # Reuse pygrabber's own boilerplate (adds the filter, requests RGB24
            # output) then swap in our continuous callback in place of its
            # grab-on-request one.
            graph.add_sample_grabber(lambda frame: None)
            sample_grabber = graph.filters[FilterType.sample_grabber]
            callback = _make_frame_callback(self._on_frame)
            sample_grabber.set_callback(callback, 1)

            graph.add_null_render()
            graph.prepare_preview_graph()

            self.actual_width, self.actual_height = sample_grabber.get_resolution()
            callback.width, callback.height = self.actual_width, self.actual_height
            self._callback = callback  # keep alive for the graph's lifetime

            graph.run()
            self._graph = graph
            self._opened = True
        except Exception:
            self.release()
            raise

    def _log(self, level: str, msg: str):
        if self._log_cb:
            try:
                self._log_cb(msg, level)
            except Exception:
                pass

    def _on_frame(self, frame):
        with self._frame_lock:
            self._latest_frame = frame
        self._frame_available.set()

    def isOpened(self) -> bool:
        return self._opened

    def read(self, timeout: float = 1.0):
        if not self._opened:
            return False, None
        if not self._frame_available.wait(timeout):
            return False, None
        with self._frame_lock:
            frame = self._latest_frame
            self._frame_available.clear()
        if frame is None:
            return False, None
        return True, frame

    def get(self, prop_id) -> float:
        import cv2

        if prop_id == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self.actual_width)
        if prop_id == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self.actual_height)
        return 0.0

    def set(self, prop_id, value) -> bool:
        # Resolution/FPS/pixel format are all configured once at construction
        # via the DirectShow stream config; there's nothing to change afterward.
        return False

    def release(self):
        if self._graph is not None:
            try:
                self._graph.stop()
            except Exception:
                pass
            try:
                self._graph.remove_filters()
            except Exception:
                pass
            self._graph = None
        self._callback = None
        if self._com_initialized:
            import comtypes

            try:
                comtypes.CoUninitialize()
            except Exception:
                pass
            self._com_initialized = False
        self._opened = False
