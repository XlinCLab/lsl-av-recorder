import sys
import threading
import time
import traceback
from typing import Callable, Optional

import cv2
from pylsl import local_clock

from .color_adjust import apply_color_adjustments
from .constants import DEFAULT_BRIGHTNESS, DEFAULT_HUE, DEFAULT_SATURATION


class VideoRecorder:
    def __init__(
        self,
        cam_cfg,
        output_path: str,
        status_cb: Callable = None,
        frame_cb: Callable = None,
        preview_cb: Optional[Callable] = None,
        preview_fps: Optional[float] = None,
    ):
        self.cam = cam_cfg
        self.output_path = output_path
        self.status_cb = status_cb
        self.frame_cb = frame_cb
        self.preview_cb = preview_cb
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
        self._fps_probe_max_wait = 2.0
        self._probe_logged = False
        self._read_fail_count = 0
        self._last_read_fail_log = None
        self._fps_warn_interval = 5.0
        self._fps_warn_rel = 0.10
        self._fps_warn_abs = 0.5
        self._last_fps_warn_ts = None
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
        if self.writer_fps is not None:
            return float(self.writer_fps)
        if self._reported_fps is not None:
            return float(self._reported_fps)
        return float(self.cam.FPS or 0.0)

    def _maybe_warn_fps(self, inst_fps: float, now: float):
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
                "Capture FPS deviation: "
                f"expected≈{expected:.2f}, observed={inst_fps:.2f}"
            )
            self._last_fps_warn_ts = now

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
            self.cap = cv2.VideoCapture(self.cam.DeviceIndex, cv2.CAP_AVFOUNDATION)
        elif sys.platform.startswith("linux"):
            source = self.cam.DevNode if getattr(self.cam, "DevNode", "") else self.cam.DeviceIndex
            self.cap = cv2.VideoCapture(source, cv2.CAP_V4L2)
        elif sys.platform.startswith("win"):
            self.cap = cv2.VideoCapture(self.cam.DeviceIndex, cv2.CAP_DSHOW)
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
            self.thread.join()
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
