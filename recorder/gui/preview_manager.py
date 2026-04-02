from __future__ import annotations

from typing import Dict

from PyQt6.QtCore import QObject, Qt, QThread
from PyQt6.QtGui import QImage, QPixmap

from .camera_worker import CameraWorker


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
            brightness=cam_cfg.Brightness,
            hue=cam_cfg.Hue,
            saturation=cam_cfg.Saturation,
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

    def stop_cam_preview(self, cam_index: int) -> bool:
        idx = int(cam_index)
        worker = self.workers.pop(idx, None)
        thread = self.threads.pop(idx, None)
        if worker:
            worker.stop_preview()
        if thread:
            thread.quit()
            thread.wait(1000)
        self.main.preview_panel.set_active_cameras(self.workers.keys())
        return worker is not None

    def start_preview_all(self):
        for panel in self.main.cam_panels:
            cam_cfg = panel.to_config()
            if not cam_cfg.Enabled:
                continue
            self.start_cam_preview(cam_cfg)

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
