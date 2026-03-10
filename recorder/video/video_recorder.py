import threading
import time
from typing import Callable

import cv2


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
        self.cap = cv2.VideoCapture(self.cam.DeviceIndex)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cam.Width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cam.Height)
        self.cap.set(cv2.CAP_PROP_FPS, self.cam.FPS)

        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self.writer = cv2.VideoWriter(
            self.output_path,
            fourcc,
            self.cam.FPS,
            (self.cam.Width, self.cam.Height),
        )

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

            ts = time.time()  # wall-clock timestamp
            self.writer.write(frame)

            if self.frame_cb:
                self.frame_cb(ts, self.frame_idx)

            self.frame_idx += 1

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join()

        if self.cap:
            self.cap.release()
        if self.writer:
            self.writer.release()

        self.info(f"VideoRecorder stopped: {self.cam.Label}")
