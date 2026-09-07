from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import cv2
from pylsl import StreamInfo, StreamOutlet, local_clock
from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from ..video.color_adjust import apply_color_adjustments
from ..video.constants import (DEFAULT_BRIGHTNESS, DEFAULT_HUE,
                               DEFAULT_SATURATION)
from ..video.devices import resolve_cv2_device_index

# LSL stream type published by CameraWorker's preview outlet
CAMERA_PREVIEW_STREAM_TYPE = "VideoFrame"


@dataclass
class RecordParams:
    out_path: str
    fps: int
    size: Tuple[int, int]
    codec: str = "mp4v"


class CameraWorker(QObject):
    # preview frames to GUI
    frameReady = pyqtSignal(int, object)
    status = pyqtSignal(str)

    def __init__(
        self,
        cam_index: int,
        devnode: str,
        label: str,
        fps: int,
        size: Tuple[int, int],
        preview_fps: int = 15,
        brightness: Optional[int] = None,
        hue: Optional[int] = None,
        saturation: Optional[int] = None,
        pixel_format: Optional[str] = None,
        device_name: Optional[str] = None,
    ):
        super().__init__()
        self.cam_index = int(cam_index)
        self.devnode = devnode
        self.label = label
        self.device_name = device_name
        self.fps = int(fps)
        self.w, self.h = int(size[0]), int(size[1])
        self.preview_fps = max(1, int(preview_fps))
        self.brightness = brightness
        self.hue = hue
        self.saturation = saturation
        self.pixel_format = pixel_format
        # Color (brightness, hue, saturation) adjustments for MacOS only
        # On Linux, color settings are controllable via V4L2
        self._apply_color_adjustments = (
            sys.platform == "darwin"
            and (
                (self.brightness is not None and int(self.brightness) != DEFAULT_BRIGHTNESS)
                or (self.hue is not None and int(self.hue) != DEFAULT_HUE)
                or (self.saturation is not None and int(self.saturation) != DEFAULT_SATURATION)
            )
        )

        self._running = False
        self.cap: Optional[cv2.VideoCapture] = None
        self.writer: Optional[cv2.VideoWriter] = None
        self._frame_idx = 0

        # Recording requests (set from GUI thread; handled inside worker loop)
        self._record_request: Optional[RecordParams] = None
        self._stop_record_request: bool = False

        sname = f"VideoFrames_cam-{self.cam_index:02d}_role-{self.label}"
        info = StreamInfo(
            name=sname,
            type=CAMERA_PREVIEW_STREAM_TYPE,
            channel_count=2,
            nominal_srate=self.fps,
            channel_format="double64",
            source_id=f"{self.devnode}:{self.label}",
        )
        self.outlet = StreamOutlet(info, chunk_size=0, max_buffered=360)

    def _open_cap(self):
        if sys.platform == "darwin":  # MacOS
            cv2_index = resolve_cv2_device_index(self.device_name, self.cam_index)
            self.cap = cv2.VideoCapture(cv2_index, cv2.CAP_AVFOUNDATION)
        elif sys.platform.startswith("linux"):
            source = self.devnode if self.devnode else self.cam_index
            self.cap = cv2.VideoCapture(source, cv2.CAP_V4L2)
        elif sys.platform.startswith("win"):  # Windows
            from ..video.dshow_capture import open_windows_capture

            self.cap = open_windows_capture(
                device_index=self.cam_index,
                width=self.w,
                height=self.h,
                pixel_format=self.pixel_format or None,
                fps=float(self.fps) or None,
                log_cb=lambda msg, level="WARNING": self.status.emit(f"{level}: {msg}"),
            )
        else:
            self.cap = cv2.VideoCapture(self.cam_index)

        if not self.cap.isOpened():
            raise RuntimeError(
                f"Could not open camera index={self.cam_index} ({self.devnode})"
            )

        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(self.w))
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self.h))
        self.cap.set(cv2.CAP_PROP_FPS, float(self.fps))

    def _close_cap(self):
        if self.cap:
            try:
                self.cap.release()
            except Exception:
                pass
        self.cap = None

    def _open_writer(self, rp: RecordParams):
        os.makedirs(os.path.dirname(rp.out_path), exist_ok=True)
        fourcc = cv2.VideoWriter_fourcc(*rp.codec)
        self.writer = cv2.VideoWriter(rp.out_path, fourcc, rp.fps, rp.size)
        if not self.writer.isOpened():
            self.writer = None
            raise RuntimeError(f"Could not open VideoWriter: {rp.out_path}")

    def _close_writer(self):
        if self.writer:
            try:
                self.writer.release()
            except Exception:
                pass
        self.writer = None

    # These are intentionally cheap: they only set flags/requests.
    # The actual writer open/close happens inside the worker loop.
    @pyqtSlot(object)
    def request_start_recording(self, rp: RecordParams):
        self._record_request = rp
        self._stop_record_request = False

    @pyqtSlot()
    def request_stop_recording(self):
        self._stop_record_request = True

    @pyqtSlot()
    def start_preview(self):
        if self._running:
            return
        self._running = True
        try:
            self._open_cap()
        except Exception as e:
            self.status.emit(str(e))
            self._running = False
            return

        self.status.emit(f"Preview ON cam {self.cam_index} ({self.label})")
        next_preview = 0.0
        preview_interval = 1.0 / self.preview_fps

        while self._running:
            ok, frame = self.cap.read() if self.cap else (False, None)
            if not ok or frame is None:
                time.sleep(0.005)
                continue
            if self._apply_color_adjustments:
                frame = apply_color_adjustments(frame, self.brightness, self.hue, self.saturation)

            # ---- handle recording requests inside worker thread ----
            if self._stop_record_request and self.writer is not None:
                self._close_writer()
                self.status.emit(f"Recording OFF cam {self.cam_index}")
                self._stop_record_request = False

            if self._record_request is not None and self.writer is None:
                rp = self._record_request
                self._record_request = None
                try:
                    # close any existing writer first (paranoia)
                    self._close_writer()
                    self._open_writer(rp)
                    self.status.emit(f"Recording ON cam {self.cam_index}: {rp.out_path}")
                except Exception as e:
                    self.status.emit(str(e))
                    self._close_writer()
            # --------------------------------------------------------

            # LSL frame sync
            t_mono = time.monotonic()
            self.outlet.push_sample([float(self._frame_idx), float(t_mono)], timestamp=local_clock())
            self._frame_idx += 1

            # Write to file if recording enabled
            if self.writer is not None:
                try:
                    self.writer.write(frame)
                except Exception as e:
                    self.status.emit(f"Video write error cam {self.cam_index}: {e}")
                    self._close_writer()

            # Throttled preview
            now = time.monotonic()
            if now >= next_preview:
                self.frameReady.emit(self.cam_index, frame.copy())
                next_preview = now + preview_interval

        # cleanup
        self._close_writer()
        self._close_cap()
        self.status.emit(f"Preview OFF cam {self.cam_index} ({self.label})")

    @pyqtSlot()
    def stop_preview(self):
        self._running = False
