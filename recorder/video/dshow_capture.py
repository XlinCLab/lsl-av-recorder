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

import logging
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, List, Optional

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


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


def _normalize_formats(formats: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fix up pygrabber's `min_framerate`/`max_framerate` fields, which are
    computed as `10_000_000 / MinFrameInterval` and `10_000_000 /
    MaxFrameInterval` respectively. Since frame *rate* is inversely related to
    frame *interval*, dividing by the smallest interval gives the *highest*
    achievable rate -- so `min_framerate` actually holds the highest rate and
    `max_framerate` the lowest, the reverse of what the names suggest."""
    normalized = []
    for fmt in formats:
        lo = min(float(fmt["min_framerate"]), float(fmt["max_framerate"]))
        hi = max(float(fmt["min_framerate"]), float(fmt["max_framerate"]))
        fmt = dict(fmt)
        fmt["min_framerate"] = lo
        fmt["max_framerate"] = hi
        normalized.append(fmt)
    return normalized


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


# Common, human-recognizable frame rates to snap noisy measurements to, so
# repeated probes of the same device converge on a stable, reproducible value
# instead of e.g. 29.1 vs 30.6 depending on measurement jitter.
_COMMON_FPS_VALUES = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 20, 24, 25, 30, 50, 60, 90, 120]


def _snap_to_common_fps(value: float) -> int:
    return min(_COMMON_FPS_VALUES, key=lambda f: abs(f - value))


def _measure_achievable_fps(
    device_index: int,
    width: int,
    height: int,
    pixel_format: str,
    target_fps: float,
    warmup: float = 2.0,
    duration: float = 2.0,
    retries: int = 2,
    retry_delay: float = 1.0,
) -> Optional[float]:
    """Briefly open a real capture at the given format/resolution/fps and
    measure the actual delivered frame rate.

    DirectShow's IAMStreamConfig::GetStreamCaps has been observed to declare an
    optimistic maximum (e.g. 60fps) that the device doesn't actually sustain in
    practice (measured ~30fps) -- capability probing shouldn't just trust that
    declaration, the same way macOS probing doesn't just trust AVFoundation's
    self-reported modes without confirming each one actually opens.

    `warmup` is discarded (not counted) before measuring: auto-exposure/
    bandwidth throttling can take a moment to kick in, and an initial burst of
    buffered frames right after opening could otherwise inflate the measurement.

    Retries on failure to open or deliver any frames: the device may still be
    releasing from whatever previously had it open (e.g. the live preview,
    typically at whatever resolution/format was just in use) by the time the
    first combination is probed -- observed in practice as exactly the
    currently-configured resolution silently keeping its optimistic declared
    max uncorrected, while every other resolution corrected fine.
    """
    for attempt in range(retries):
        if attempt > 0:
            time.sleep(retry_delay)
        try:
            cap = WindowsDShowVideoCapture(device_index, width, height, pixel_format, target_fps)
        except Exception:
            continue
        try:
            if not cap.isOpened():
                continue
            warmup_end = time.monotonic() + warmup
            while time.monotonic() < warmup_end:
                cap.read(timeout=0.5)
            count = 0
            start = time.monotonic()
            while time.monotonic() - start < duration:
                ok, _ = cap.read(timeout=0.5)
                if ok:
                    count += 1
            elapsed = time.monotonic() - start
            if elapsed <= 0 or count == 0:
                continue
            return count / elapsed
        finally:
            cap.release()
    return None


def _verify_max_framerates(
    device_index: int,
    formats: List[Dict[str, Any]],
    progress_cb: Optional[Callable[[int, str], None]] = None,
) -> List[Dict[str, Any]]:
    """Correct each unique (pixel format, resolution) combination's declared
    maximum fps against what's actually measured, clamping it down if the
    driver's declaration is optimistic -- so the GUI never offers, and users
    never select, a rate the camera can't really sustain."""
    unique_combos: dict[tuple[str, int, int], float] = {}
    for fmt in formats:
        key = (str(fmt["media_type_str"]).upper(), int(fmt["width"]), int(fmt["height"]))
        unique_combos[key] = max(unique_combos.get(key, 0.0), float(fmt["max_framerate"]))

    corrections: dict[tuple[str, int, int], float] = {}
    total = len(unique_combos)
    for i, ((pf, w, h), declared_max) in enumerate(unique_combos.items(), start=1):
        if progress_cb:
            try:
                progress_cb(
                    int(100 * i / max(1, total)),
                    f"Verifying achievable FPS ({i}/{total}): {pf} {w}x{h}",
                )
            except Exception:
                pass
        measured = _measure_achievable_fps(device_index, w, h, pf, declared_max)
        if measured is None:
            logger.info(
                f"FPS verify: {pf} {w}x{h} declared_max={declared_max} "
                "-> measurement FAILED (device busy/unreachable), leaving declared value as-is"
            )
            continue
        snapped = _snap_to_common_fps(measured)
        # Only correct on a real gap, not measurement noise around the
        # declared value -- but tight enough to still catch a partial (not
        # just total) shortfall, e.g. a declared 60fps that only reaches ~50.
        will_correct = snapped < declared_max * 0.85
        logger.info(
            f"FPS verify: {pf} {w}x{h} declared_max={declared_max} "
            f"measured={measured:.2f} snapped={snapped} "
            f"-> {'CORRECTING to ' + str(snapped) if will_correct else 'keeping declared value (within tolerance)'}"
        )
        if will_correct:
            corrections[(pf, w, h)] = float(snapped)

    if not corrections:
        logger.info("FPS verify: no corrections applied to any (format, resolution) combination")
        return formats

    logger.info(f"FPS verify: applying corrections to {len(corrections)} combination(s): {corrections}")
    corrected_formats = []
    for fmt in formats:
        key = (str(fmt["media_type_str"]).upper(), int(fmt["width"]), int(fmt["height"]))
        if key in corrections:
            fmt = dict(fmt)
            fmt["max_framerate"] = min(float(fmt["max_framerate"]), corrections[key])
        corrected_formats.append(fmt)
    return corrected_formats


def get_windows_camera_capabilities(
    device_index: int,
    progress_cb: Optional[Callable[[int, str], None]] = None,
) -> Dict[str, Any]:
    with _com_session():
        from pygrabber.dshow_graph import FilterGraph

        graph = FilterGraph()
        graph.add_video_input_device(device_index)
        formats = _normalize_formats(graph.get_input_device().get_formats())
        del graph  # release COM references before CoUninitialize runs

    formats = _verify_max_framerates(device_index, formats, progress_cb=progress_cb)
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


def _set_format_with_fps(video_input, format_index: int, fps: Optional[float]) -> None:
    """Select a DirectShow stream-caps entry by index, overwriting the media
    type's embedded frame interval with the exact requested fps first.

    `VideoInput.set_format()` (pygrabber) calls `SetFormat` with the media type
    exactly as returned by `GetStreamCaps`, whose embedded `avg_time_per_frame`
    is that entry's own nominal rate -- observed in practice to be the entry's
    declared *maximum* -- not necessarily the fps actually requested. Without
    overwriting it, every rate within an entry's declared [min, max] range ends
    up recording at that same default instead of the one actually selected.
    """
    from ctypes import POINTER, cast
    from pygrabber.dshow_core import IAMStreamConfig, VIDEOINFOHEADER

    stream_config = video_input.get_out().QueryInterface(IAMStreamConfig)
    media_type, _ = stream_config.GetStreamCaps(format_index)
    if fps:
        header = cast(media_type.contents.pbFormat, POINTER(VIDEOINFOHEADER))
        header.contents.avg_time_per_frame = int(round(10_000_000 / fps))
    stream_config.SetFormat(media_type)


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
                    # DirectShow's "RGB24" buffers are stored bottom-up AND in
                    # BGR byte order in memory (the classic Windows DIB/bitmap
                    # convention) -- which already matches cv2's own BGR
                    # convention, so only the vertical flip is needed, not a
                    # channel reversal (that would swap red/blue). Copy out
                    # before returning -- DirectShow reuses this buffer for the
                    # next frame.
                    img = np.ascontiguousarray(np.flip(img, axis=0))
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
                formats = _normalize_formats(video_input.get_formats())
                match_index = _find_format_index(formats, width, height, pixel_format, fps)
                if match_index is not None:
                    _set_format_with_fps(video_input, match_index, fps)
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
