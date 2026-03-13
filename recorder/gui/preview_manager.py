from __future__ import annotations

import os
from typing import Dict

from PyQt6.QtCore import QObject, Qt, QThread
from PyQt6.QtGui import QImage, QPixmap

from ..naming import video_filename
from .camera_worker import CameraWorker, RecordParams


class PreviewManager(QObject):
    def __init__(self, main_window):
        super().__init__()
        self.main = main_window
        self.threads: Dict[int, QThread] = {}
        self.workers: Dict[int, CameraWorker] = {}

    def start_cam_preview(self, cam_cfg):
        idx = int(cam_cfg.DeviceIndex)
        if idx in self.workers:
            return

        worker = CameraWorker(
            cam_index=idx,
            devnode=cam_cfg.DevNode,
            label=cam_cfg.Label,
            fps=cam_cfg.FPS,
            size=(cam_cfg.Width, cam_cfg.Height),
            preview_fps=getattr(self.main.cfg.Video, "PreviewFPS", 15),
        )
        thread = QThread()
        worker.moveToThread(thread)

        worker.frameReady.connect(self.on_frame)
        worker.status.connect(self.main.log)
        thread.started.connect(worker.start_preview)

        self.workers[idx] = worker
        self.threads[idx] = thread
        thread.start()
        self.main.preview_panel.set_active_cameras(self.workers.keys())

    def stop_all_previews(self):
        for w in self.workers.values():
            w.stop_preview()
        for t in self.threads.values():
            t.quit()
            t.wait(1000)
        self.workers.clear()
        self.threads.clear()
        self.main.preview_panel.set_active_cameras([])

    def start_recording_all(self, out_dir: str, base_name: str, video_container: str, codec: str):
        for panel in self.main.cam_panels:
            cam_cfg = panel.to_config()
            if not cam_cfg.Enabled:
                continue
            self.start_cam_preview(cam_cfg)
            idx = int(cam_cfg.DeviceIndex)
            w = self.workers.get(idx)
            if not w:
                continue
            out_path = os.path.join(out_dir, video_filename(base_name, idx, cam_cfg.Label, video_container))
            rp = RecordParams(out_path=out_path, fps=cam_cfg.FPS, size=(cam_cfg.Width, cam_cfg.Height), codec=codec)
            w.request_start_recording(rp)

    def stop_recording_all(self):
        for w in self.workers.values():
            w.request_stop_recording()

    def on_frame(self, cam_index: int, frame_bgr):
        lbl = self.main.preview_panel.ensure_label(int(cam_index))
        h, w, ch = frame_bgr.shape
        rgb = frame_bgr[:, :, ::-1].copy()
        qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888)

        pix = QPixmap.fromImage(qimg).scaled(
            lbl.width(), lbl.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        lbl.setPixmap(pix)
        lbl.setText("")
