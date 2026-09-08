from __future__ import annotations

from typing import Dict

from PyQt6.QtCore import QObject, QRect, Qt, QThread
from PyQt6.QtGui import QColor, QImage, QPainter, QPixmap

from .camera_worker import CameraWorker, compute_preview_key


def _draw_caption(pix: QPixmap, text: str) -> None:
    """Burn a semi-transparent caption bar containing device label
    into the bottom of a preview frame."""
    if not text:
        return
    painter = QPainter(pix)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bar_height = max(18, int(pix.height() * 0.12))
        bar_rect = QRect(0, pix.height() - bar_height, pix.width(), bar_height)
        painter.fillRect(bar_rect, QColor(0, 0, 0, 160))
        painter.setPen(QColor(255, 255, 255))
        font = painter.font()
        font.setPointSize(max(9, bar_height // 2))
        painter.setFont(font)
        painter.drawText(bar_rect, Qt.AlignmentFlag.AlignCenter, text)
    finally:
        painter.end()


def preview_key(cam_cfg) -> str:
    """preview_manager's view of compute_preview_key
    reading straight from a VideoCamConfig."""
    return compute_preview_key(cam_cfg.DeviceName, cam_cfg.DevNode, cam_cfg.DeviceIndex)


class PreviewManager(QObject):
    def __init__(self, main_window):
        super().__init__()
        self.main = main_window
        self.threads: Dict[str, QThread] = {}
        self.workers: Dict[str, CameraWorker] = {}

    def _request_stop(self, key: str):
        # Pop immediately so a new preview can be registered for the same key.
        worker = self.workers.pop(key, None)
        thread = self.threads.pop(key, None)
        self.main.preview_panel.set_active_cameras(self.workers.keys())
        if not thread:
            return
        if worker:
            worker.stop_preview()
        # Capture specific objects so the finished handler never touches the dict
        # (which may already hold a new worker/thread for the same key by the
        # time the signal fires, causing the new thread to be erroneously deleted)
        _w, _t = worker, thread

        def _cleanup():
            if _w:
                _w.deleteLater()
            _t.deleteLater()

        thread.finished.connect(_cleanup)
        thread.quit()
        # Wait briefly so the old camera releases its device before a new capture
        # for the same key tries to open it.
        thread.wait(2000)

    def start_cam_preview(self, cam_cfg):
        key = preview_key(cam_cfg)
        if key in self.workers:
            return

        worker = CameraWorker(
            cam_index=int(cam_cfg.DeviceIndex),
            devnode=cam_cfg.DevNode,
            label=cam_cfg.Label,
            fps=cam_cfg.FPS,
            size=(cam_cfg.Width, cam_cfg.Height),
            preview_fps=getattr(self.main.cfg.Video, "PreviewFPS", 15),
            brightness=cam_cfg.Brightness,
            hue=cam_cfg.Hue,
            saturation=cam_cfg.Saturation,
            pixel_format=cam_cfg.PixelFormat,
            device_name=cam_cfg.DeviceName,
        )
        thread = QThread()
        worker.moveToThread(thread)

        worker.frameReady.connect(self.on_frame)
        worker.status.connect(self.main.log)
        thread.started.connect(worker.start_preview)

        self.workers[key] = worker
        self.threads[key] = thread
        thread.start()
        self.main.preview_panel.set_active_cameras(self.workers.keys())

    def stop_all_previews(self):
        for key in list(self.workers.keys()):
            self._request_stop(key)
        self.main.preview_panel.set_active_cameras([])

    def stop_cam_preview(self, key: str) -> bool:
        exists = key in self.workers
        if exists:
            self._request_stop(key)
        return exists

    def start_preview_all(self):
        for panel in self.main.cam_panels:
            cam_cfg = panel.to_config()
            if not cam_cfg.Enabled:
                continue
            self.start_cam_preview(cam_cfg)

    def on_frame(self, key: str, frame_bgr):
        # A worker can emit one more frame after stop_preview() sets its
        # _running flag False -- it may already be past that check, mid-loop,
        # when the flag flips. That frame's frameReady signal is queued
        # and can be delivered here after this key has already been torn down
        # or reassigned to a different camera, so it must be dropped rather
        # than rendered. Otherwise, it resurrects a stale preview label
        # showing a frozen last frame.
        worker = self.workers.get(key)
        if worker is None:
            return
        lbl = self.main.preview_panel.ensure_label(key)
        h, w, ch = frame_bgr.shape
        rgb = frame_bgr[:, :, ::-1].copy()
        qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888)

        pix = QPixmap.fromImage(qimg).scaled(
            lbl.width(), lbl.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        _draw_caption(pix, worker.label or key)
        lbl.setPixmap(pix)
        lbl.setText("")
