import sys
import threading
import time
from typing import Callable

import cv2
from pylsl import local_clock


class VideoRecorder:
    def __init__(
        self,
        cam_cfg,
        output_path: str,
        status_cb: Callable = None,
        frame_cb: Callable = None,
    ):
        self.cam = cam_cfg
        self.output_path = output_path
        self.status_cb = status_cb
        self.frame_cb = frame_cb

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

    def log(self, msg: str, loglevel: str = "INFO"):
        if self.status_cb:
            self.status_cb(msg, loglevel)

    def info(self, msg: str):
        self.log(msg, loglevel="INFO")

    def warning(self, msg: str):
        self.log(msg, loglevel="WARNING")

    def error(self, msg: str):
        self.log(msg, loglevel="ERROR")

    def start(self):
        if sys.platform.startswith("linux") and getattr(self.cam, "DevNode", ""):
            self.cap = cv2.VideoCapture(self.cam.DevNode)
        else:
            self.cap = cv2.VideoCapture(self.cam.DeviceIndex)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cam.Width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cam.Height)
        self.cap.set(cv2.CAP_PROP_FPS, self.cam.FPS)

        reported_fps = float(self.cap.get(cv2.CAP_PROP_FPS) or 0.0)
        if reported_fps > 0 and abs(reported_fps - float(self.cam.FPS)) > 0.1:
            self.warning(
                f"Camera FPS mismatch: requested={self.cam.FPS} reported={reported_fps:.3f}"
            )
        self.writer_fps = None
        if reported_fps > 0:
            self.info(f"Camera reported FPS: {reported_fps:.3f}")

        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

        self.info(f"VideoRecorder started: {self.cam.Label}")

    def _loop(self):
        while self.running:
            ret, frame = self.cap.read()
            if not ret:
                time.sleep(0.001)
                continue

            if self.writer is None:
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

            ts = local_clock()  # LSL clock timestamp (aligns with audio)
            self.writer.write(frame)

            if self.frame_cb:
                self.frame_cb(ts, self.frame_idx)

            self.frame_idx += 1
            now = time.perf_counter()
            if self._start_ts is None:
                self._start_ts = now
                self._last_log_ts = now
                self._last_log_frame_idx = self.frame_idx
            elif self._last_log_ts is not None and (now - self._last_log_ts) >= 2.0:
                dt = now - self._last_log_ts
                frames = self.frame_idx - self._last_log_frame_idx
                if dt > 0:
                    inst_fps = frames / dt
                    self.info(f"Capture FPS (last {dt:.1f}s): {inst_fps:.2f}")
                self._last_log_ts = now
                self._last_log_frame_idx = self.frame_idx

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join()

        if self.cap:
            self.cap.release()
        if self.writer:
            self.writer.release()

        if self._start_ts is not None:
            total_dt = time.perf_counter() - self._start_ts
            if total_dt > 0:
                avg_fps = self.frame_idx / total_dt
                self.info(f"Capture FPS (avg): {avg_fps:.2f}")

        self.info(f"VideoRecorder stopped: {self.cam.Label}")
