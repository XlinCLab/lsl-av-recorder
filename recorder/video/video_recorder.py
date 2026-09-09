import sys
import threading
import time
import traceback
from typing import Callable, Optional

import cv2
from pylsl import local_clock

from ..video.constants import IS_MAC
from .avfoundation_capture import force_active_format
from .color_adjust import apply_color_adjustments
from .constants import DEFAULT_BRIGHTNESS, DEFAULT_HUE, DEFAULT_SATURATION
from .devices import resolve_cv2_device_index


class VideoRecorder:
    def __init__(
        self,
        cam_cfg,
        output_path: str,
        status_cb: Callable = None,
        frame_cb: Callable = None,
        preview_cb: Optional[Callable] = None,
        preview_fps: Optional[float] = None,
        divergence_cb: Optional[Callable[[str, str], bool]] = None,
    ):
        self.cam = cam_cfg
        self.output_path = output_path
        self.status_cb = status_cb
        self.frame_cb = frame_cb
        self.preview_cb = preview_cb
        # Called (title, message) -> bool when a measured setting diverges
        # from what was configured; True = accept and keep recording, False = abort
        self.divergence_cb = divergence_cb
        # Set once the operator accepts an observed fps as the new baseline,
        # so _expected_fps() stops comparing against the original configured value
        self._accepted_fps_override: Optional[float] = None
        self._size_divergence_prompted = False
        self._preview_interval = None
        self._next_preview_ts = None
        if self.preview_cb and preview_fps:
            try:
                fps_val = float(preview_fps)
                if fps_val > 0:
                    self._preview_interval = 1.0 / fps_val
            except Exception as exc:
                self.warning(f"Error setting preview FPS: {exc}")
                self._preview_interval = None

        self.cap = None
        self.writer = None
        self.running = False
        self.thread = None
        self.frame_idx = 0
        self.writer_fps = None
        self.writer_size = None
        self._start_ts = None
        self._last_log_ts = None
        self._last_log_frame_idx = 0
        self._reported_fps = None
        self._fps_probe_start = None
        self._fps_probe_times = []
        self._fps_probe_frames = []
        self._fps_probe_min_frames = 10
        self._fps_probe_min_duration = 0.5
        # Frames captured in this initial window are still buffered/written
        # normally (nothing is lost), but excluded from the FPS measurement
        # to avoid biasing based on the first few samples, which may have lower FPS
        self._fps_probe_warmup = 1.0
        self._fps_probe_max_wait = 3.5
        self._probe_logged = False
        self._read_fail_count = 0
        self._last_read_fail_log = None
        self._fps_warn_interval = 5.0
        self._fps_warn_rel = 0.10
        self._fps_warn_abs = 0.5
        self._last_fps_warn_ts = None
        # How long to wait after the VideoWriter opens before the deviation
        # check is allowed to warn at all: right after startup (especially
        # with multiple cameras capturing simultaneously), capture rate can
        # genuinely dip for a few seconds while things settle. A false
        # warning here is worse than a real one arriving a few seconds late.
        self._fps_warn_grace_period = 10.0
        self._writer_opened_at: Optional[float] = None
        self._brightness = self.cam.Brightness
        self._hue = self.cam.Hue
        self._saturation = self.cam.Saturation
        # Color (brightness, hue, saturation) adjustments for MacOS only
        # On Linux, color settings are controllable via V4L2
        self._apply_color_adjustments = (
            sys.platform == "darwin"
            and (
                (self._brightness is not None and int(self._brightness) != DEFAULT_BRIGHTNESS)
                or (self._hue is not None and int(self._hue) != DEFAULT_HUE)
                or (self._saturation is not None and int(self._saturation) != DEFAULT_SATURATION)
            )
        )

    def log(self, msg: str, loglevel: str = "INFO"):
        if self.status_cb:
            self.status_cb(msg, loglevel)

    def info(self, msg: str):
        self.log(msg, loglevel="INFO")

    def warning(self, msg: str):
        self.log(msg, loglevel="WARNING")

    def error(self, msg: str):
        self.log(msg, loglevel="ERROR")

    def debug(self, msg: str):
        self.log(msg, loglevel="DEBUG")

    def _expected_fps(self) -> float:
        if self._accepted_fps_override is not None:
            return float(self._accepted_fps_override)
        if self.cam.FPS:
            return float(self.cam.FPS)
        if self._reported_fps is not None:
            return float(self._reported_fps)
        if self.writer_fps is not None:
            return float(self.writer_fps)
        return 0.0

    def _maybe_warn_fps(self, inst_fps: float, now: float):
        if (
            self._writer_opened_at is not None
            and (now - self._writer_opened_at) < self._fps_warn_grace_period
        ):
            return
        expected = self._expected_fps()
        if expected <= 0:
            return
        delta = abs(inst_fps - expected)
        threshold = max(self._fps_warn_abs, expected * self._fps_warn_rel)
        if delta < threshold:
            return
        if (
            self._last_fps_warn_ts is None
            or (now - self._last_fps_warn_ts) >= self._fps_warn_interval
        ):
            self.warning(
                f"Capture FPS deviation ({self.cam.Label}): "
                f"expected≈{expected:.2f}, observed={inst_fps:.2f}"
            )
            self._last_fps_warn_ts = now
            self._prompt_fps_divergence(expected, inst_fps)

    def _prompt_fps_divergence(self, expected: float, observed: float):
        if not self.divergence_cb:
            return
        accepted = self.divergence_cb(
            f"Camera FPS deviation: {self.cam.Label}",
            f"Camera {self.cam.Label} is configured to record at {expected:.2f} fps, "
            f"but {observed:.2f} fps is actually being captured.\n\n"
            "Accept the observed frame rate as the new expected frame rate "
            "and continue recording, or abort the recording and adjust settings?",
        )
        if accepted:
            # Set current observed rate as the new accepted baseline;
            # further deviation from THIS value will still warn again
            self._accepted_fps_override = observed
        # If rejected, divergence_cb has already triggered the abort itself

    def _prompt_size_divergence(self, actual_w: int, actual_h: int):
        if not self.divergence_cb or self._size_divergence_prompted:
            return
        self._size_divergence_prompted = True
        self.divergence_cb(
            f"Camera frame size mismatch: {self.cam.Label}",
            f"Camera {self.cam.Label} is configured for "
            f"{self.cam.Width}x{self.cam.Height}, but is actually delivering "
            f"{actual_w}x{actual_h}.\n\n"
            "Accept the actual size and continue recording, or abort the "
            "recording and adjust settings?",
        )

    def _maybe_emit_preview(self, frame):
        if not self.preview_cb or self._preview_interval is None:
            return
        now = time.monotonic()
        if self._next_preview_ts is None or now >= self._next_preview_ts:
            try:
                self.preview_cb(frame.copy())
            except Exception as exc:
                self.warning(f"Preview callback failed: {exc}")
            self._next_preview_ts = now + self._preview_interval

    def start(self):
        if sys.platform == "darwin":
            cv2_index = resolve_cv2_device_index(
                getattr(self.cam, "DeviceName", None), self.cam.DeviceIndex
            )
            self.cap = cv2.VideoCapture(cv2_index, cv2.CAP_AVFOUNDATION)
        elif sys.platform.startswith("linux"):
            source = self.cam.DevNode if getattr(self.cam, "DevNode", "") else self.cam.DeviceIndex
            self.cap = cv2.VideoCapture(source, cv2.CAP_V4L2)
        elif sys.platform.startswith("win"):
            from .dshow_capture import open_windows_capture

            try:
                self.cap = open_windows_capture(
                    device_index=int(self.cam.DeviceIndex),
                    width=int(self.cam.Width),
                    height=int(self.cam.Height),
                    pixel_format=str(getattr(self.cam, "PixelFormat", "") or "") or None,
                    fps=float(self.cam.FPS or 0) or None,
                    log_cb=self.log,
                )
            except Exception as exc:
                self.error(f"Could not open DirectShow capture: {exc}")
                self.cap = None
                return False
        else:
            self.cap = cv2.VideoCapture(self.cam.DeviceIndex)
        if not self.cap.isOpened():
            self.error(f"Could not open camera: {self.cam.Label}")
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None
            return False

        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cam.Width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cam.Height)
        self.cap.set(cv2.CAP_PROP_FPS, self.cam.FPS)

        # Workaround for setting pixel format on MacOS
        if IS_MAC and self.cam.FPS:
            # cv2's AVFoundation backend never selects among a device's native
            # AVCaptureDeviceFormats itself, rather only adjusts frame duration
            # within whatever format the OS already happens to have active.
            # For a continuous frame-rate range (e.g. 15-30fps), cv2's
            # CAP_PROP_FPS handling pins BOTH min and max duration to the
            # range's fastest rate rather than the one actually requested.
            # This MUST run after cv2's CAP_PROP_FPS call above.
            pixel_format = getattr(self.cam, "PixelFormat", None)
            forced = force_active_format(
                device_index=self.cam.DeviceIndex,
                width=int(self.cam.Width),
                height=int(self.cam.Height),
                fps=float(self.cam.FPS),
                pixel_format=pixel_format,
                device_name=getattr(self.cam, "DeviceName", None),
            )
            if forced:
                self.info(f"Force-set active pixel format to {pixel_format} for {self.cam.Label}")
            if not forced:
                self.warning(
                    f"Could not force native capture pixel format {pixel_format} for {self.cam.Label}; "
                    "the achieved frame rate may fall back to whatever "
                    "AVFoundation's default active format allows."
                )

        reported_fps = float(self.cap.get(cv2.CAP_PROP_FPS) or 0.0)
        if reported_fps > 0 and abs(reported_fps - float(self.cam.FPS)) > 0.1:
            self.warning(
                f"Camera FPS mismatch: requested={self.cam.FPS} reported={reported_fps:.3f}"
            )
        self.writer_fps = None
        self._reported_fps = reported_fps if reported_fps > 0 else None
        if self._reported_fps is not None:
            self.info(f"Camera reported FPS: {reported_fps:.3f}")

        requested_fps = float(self.cam.FPS or 0.0)
        if requested_fps > 0:
            self._fps_probe_min_frames = max(5, int(round(requested_fps * 0.25)))

        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

        self.info(f"VideoRecorder started: {self.cam.Label}")
        return True

    def _loop(self):
        try:
            while self.running:
                ret, frame = self.cap.read()
                if not ret or frame is None:
                    self._read_fail_count += 1
                    now = time.perf_counter()
                    if (
                        self._last_read_fail_log is None
                        or (now - self._last_read_fail_log) >= 2.0
                    ):
                        self.warning(
                            "Camera read failed "
                            f"({self._read_fail_count} consecutive failures)"
                        )
                        self._last_read_fail_log = now
                    time.sleep(0.001)
                    continue

                if not self.running:
                    break

                if self._apply_color_adjustments:
                    frame = apply_color_adjustments(
                        frame, self._brightness, self._hue, self._saturation
                    )

                self._maybe_emit_preview(frame)

                self._read_fail_count = 0
                now = time.perf_counter()

                if self.writer is None:
                    if not self._probe_logged:
                        self.debug("Probing capture FPS before opening VideoWriter")
                        self._probe_logged = True
                    if self._fps_probe_start is None:
                        self._fps_probe_start = now
                    self._fps_probe_frames.append(frame)
                    if (now - self._fps_probe_start) >= self._fps_probe_warmup:
                        self._fps_probe_times.append(now)

                    if self.writer_fps is None:
                        if len(self._fps_probe_times) >= self._fps_probe_min_frames:
                            dt = self._fps_probe_times[-1] - self._fps_probe_times[0]
                            if dt >= self._fps_probe_min_duration:
                                fps = (
                                    (len(self._fps_probe_times) - 1) / dt
                                    if dt > 0
                                    else 0.0
                                )
                                if fps > 0:
                                    self.writer_fps = fps
                                    self.info(
                                        f"Measured capture FPS: {self.writer_fps:.2f}"
                                    )
                        if (
                            self.writer_fps is None
                            and self._fps_probe_start is not None
                            and (now - self._fps_probe_start) >= self._fps_probe_max_wait
                        ):
                            if self._reported_fps is not None:
                                self.writer_fps = self._reported_fps
                                self.warning(
                                    "Falling back to reported FPS: "
                                    f"{self.writer_fps:.2f}"
                                )
                            else:
                                fallback = float(self.cam.FPS or 0.0)
                                if fallback <= 0:
                                    fallback = 30.0
                                self.writer_fps = fallback
                                self.warning(
                                    "Falling back to requested FPS: "
                                    f"{self.writer_fps:.2f}"
                                )

                    if self.writer_fps is None:
                        # Keep buffering until we have a usable FPS estimate.
                        ts = local_clock()
                        if self.frame_cb:
                            self.frame_cb(ts, self.frame_idx)
                        self.frame_idx += 1
                        if self._start_ts is None:
                            self._start_ts = now
                            self._last_log_ts = now
                            self._last_log_frame_idx = self.frame_idx
                        elif (
                            self._last_log_ts is not None
                            and (now - self._last_log_ts) >= 2.0
                        ):
                            dt = now - self._last_log_ts
                            frames = self.frame_idx - self._last_log_frame_idx
                            if dt > 0:
                                inst_fps = frames / dt
                                self.debug(
                                    f"Capture FPS (last {dt:.1f}s): {inst_fps:.2f}"
                                )
                                self._maybe_warn_fps(inst_fps, now)
                            self._last_log_ts = now
                            self._last_log_frame_idx = self.frame_idx
                        continue

                    actual_h, actual_w = frame.shape[:2]
                    self.writer_size = (actual_w, actual_h)
                    if (actual_w, actual_h) != (self.cam.Width, self.cam.Height):
                        self.warning(
                            f"Camera frame size mismatch: requested={self.cam.Width}x{self.cam.Height} "
                            f"actual={actual_w}x{actual_h}"
                        )
                        self._prompt_size_divergence(actual_w, actual_h)

                    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                    self.writer = cv2.VideoWriter(
                        self.output_path,
                        fourcc,
                        float(self.writer_fps),
                        (actual_w, actual_h),
                    )
                    if not self.writer.isOpened():
                        self.writer = None
                        self.error(f"Could not open VideoWriter: {self.output_path}")
                        self.running = False
                        break

                    self.info(
                        f"VideoWriter opened: fps={self.writer_fps:.3f} size={actual_w}x{actual_h}"
                    )

                    for buffered_frame in self._fps_probe_frames:
                        self.writer.write(buffered_frame)
                    self._fps_probe_frames = []
                    self._fps_probe_times = []

                    # Restart the deviation-check window here rather than
                    # leaving it dating back to _start_ts (set at the very
                    # first captured frame): otherwise the first post-open
                    # 2s window spans the probing/startup ramp-up period,
                    # when frames arrive slower than steady-state, and
                    # falsely reports a deviation right as the writer opens.
                    self._last_log_ts = now
                    self._last_log_frame_idx = self.frame_idx
                    self._writer_opened_at = now
                else:
                    self.writer.write(frame)

                ts = local_clock()  # LSL clock timestamp (aligns with audio)
                if self.frame_cb:
                    self.frame_cb(ts, self.frame_idx)

                self.frame_idx += 1
                if self._start_ts is None:
                    self._start_ts = now
                    self._last_log_ts = now
                    self._last_log_frame_idx = self.frame_idx
                elif self._last_log_ts is not None and (now - self._last_log_ts) >= 2.0:
                    dt = now - self._last_log_ts
                    frames = self.frame_idx - self._last_log_frame_idx
                    if dt > 0:
                        inst_fps = frames / dt
                        self.debug(f"Capture FPS (last {dt:.1f}s): {inst_fps:.2f}")
                        self._maybe_warn_fps(inst_fps, now)
                    self._last_log_ts = now
                    self._last_log_frame_idx = self.frame_idx
        except Exception:
            self.error("VideoRecorder crashed:")
            self.error(traceback.format_exc())
            self.running = False

    def stop(self):
        self.running = False
        if self.thread:
            self.debug(f"VideoRecorder stopping (join): {self.cam.Label}")
            # Bounded: if this camera's own worker thread is meanwhile
            # blocked inside divergence_cb waiting on a DIFFERENT camera's
            # still-open dialog, an unbounded join here (called from the
            # GUI thread while handling that other camera's abort) would
            # freeze the app forever -- the GUI thread would never get back
            # to its event loop to show this camera's own dialog. A normal
            # stop always finishes far under this, so it changes nothing in
            # the common case.
            self.thread.join(timeout=10.0)
            if self.thread.is_alive():
                self.warning(
                    f"VideoRecorder thread for {self.cam.Label} did not stop "
                    "within 10s (likely waiting on a settings-divergence "
                    "prompt for another camera); continuing teardown anyway."
                )
            self.debug(f"VideoRecorder joined: {self.cam.Label}")

        if self.writer:
            try:
                self.debug(f"VideoRecorder releasing writer: {self.cam.Label}")
                self.writer.release()
                self.debug(f"VideoRecorder released writer: {self.cam.Label}")
            except Exception:
                pass
            self.writer = None
        if self.cap:
            try:
                self.debug(f"VideoRecorder releasing camera: {self.cam.Label}")
                self.cap.release()
                self.debug(f"VideoRecorder released camera: {self.cam.Label}")
            except Exception:
                pass
            self.cap = None

        if self._start_ts is not None:
            total_dt = time.perf_counter() - self._start_ts
            if total_dt > 0:
                avg_fps = self.frame_idx / total_dt
                self.info(f"Capture FPS (avg): {avg_fps:.2f}")

        self.info(f"VideoRecorder stopped: {self.cam.Label}")
