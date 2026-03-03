from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QLineEdit,
                             QPushButton, QSpinBox, QTextEdit,
                             QVBoxLayout, QWidget)

from ..config import VideoCamConfig
from ..video.camera_settings import apply_camera_controls, summarize_control_application


class CameraPanel(QWidget):
    log = pyqtSignal(str)

    def __init__(self, cam_cfg: VideoCamConfig, parent: Optional[QWidget] = None):
        super().__init__(parent)

        self.enabled = QCheckBox("Enabled (starts preview)")
        self.enabled.setChecked(bool(cam_cfg.Enabled))

        self.device_index = QSpinBox()
        self.device_index.setRange(0, 32)
        self.device_index.setValue(cam_cfg.DeviceIndex)

        self.devnode = QLineEdit(cam_cfg.DevNode)
        self.label = QLineEdit(cam_cfg.Label)

        self.fps = QSpinBox(); self.fps.setRange(1, 240); self.fps.setValue(cam_cfg.FPS)
        self.width = QSpinBox(); self.width.setRange(1, 7680); self.width.setValue(cam_cfg.Width)
        self.height = QSpinBox(); self.height.setRange(1, 4320); self.height.setValue(cam_cfg.Height)
        self.brightness = QSpinBox(); self.brightness.setRange(-100, 100); self.brightness.setValue(int(cam_cfg.Brightness or 0))
        self.hue = QSpinBox(); self.hue.setRange(-180, 180); self.hue.setValue(int(cam_cfg.Hue or 0))
        self.saturation = QSpinBox(); self.saturation.setRange(0, 200); self.saturation.setValue(int(cam_cfg.Saturation or 100))

        self.pixel_format = QComboBox()
        self.pixel_format.addItems(["MJPG", "YUYV"])
        pf = (getattr(cam_cfg, "PixelFormat", "") or "MJPG").upper()
        i = self.pixel_format.findText(pf)
        self.pixel_format.setCurrentIndex(i if i >= 0 else 0)

        self.auto_exposure = QCheckBox("On")
        self.auto_exposure.setChecked(bool(cam_cfg.AutoExposure))
        self.auto_focus = QCheckBox("On")
        self.auto_focus.setChecked(bool(cam_cfg.AutoFocus))

        form = QFormLayout()
        form.addRow(self.enabled)
        form.addRow("DeviceIndex", self.device_index)
        form.addRow("DevNode", self.devnode)
        form.addRow("Label", self.label)
        form.addRow("FPS", self.fps)
        form.addRow("Width", self.width)
        form.addRow("Height", self.height)
        form.addRow("Brightness", self.brightness)
        form.addRow("Hue", self.hue)
        form.addRow("Saturation", self.saturation)
        form.addRow("Pixel format", self.pixel_format)
        form.addRow("Auto exposure", self.auto_exposure)
        form.addRow("Auto focus", self.auto_focus)

        self.btn_apply = QPushButton("Apply settings")
        self.text = QTextEdit(); self.text.setReadOnly(True)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(self.btn_apply)
        layout.addWidget(self.text)
        self.setLayout(layout)

        self.btn_apply.clicked.connect(self.on_apply)

    def to_config(self) -> VideoCamConfig:
        c = VideoCamConfig()
        c.Enabled = self.enabled.isChecked()
        c.DeviceIndex = int(self.device_index.value())
        c.DevNode = self.devnode.text().strip()
        c.Label = self.label.text().strip()
        c.FPS = int(self.fps.value())
        c.Width = int(self.width.value())
        c.Height = int(self.height.value())
        c.Brightness = int(self.brightness.value())
        c.Hue = int(self.hue.value())
        c.Saturation = int(self.saturation.value())
        c.PixelFormat = self.pixel_format.currentText()
        c.AutoExposure = self.auto_exposure.isChecked()
        c.AutoFocus = self.auto_focus.isChecked()
        return c

    def on_apply(self):
        dev = self.devnode.text().strip()

        # Gather controls from the UI
        cfg = self.to_config()
        controls: dict[str, Any] = {}
        if cfg.Width:
            controls["width"] = cfg.Width
        if cfg.Height:
            controls["height"] = cfg.Height
        if cfg.FPS:
            controls["fps"] = cfg.FPS
        controls["brightness"] = cfg.Brightness
        controls["hue"] = cfg.Hue
        controls["saturation"] = cfg.Saturation
        controls["pixel_format"] = cfg.PixelFormat
        controls["auto_exposure"] = 1 if cfg.AutoExposure else 0
        controls["auto_focus"] = 1 if cfg.AutoFocus else 0

        if not controls:
            self.text.append("No controls to apply")
            return

        rep = apply_camera_controls(dev, controls)
        # Print summary of successfully applied and failed settings
        summary = summarize_control_application(
            dev,
            rep['applied'],
            rep['failed'],
        )
        self.text.append(summary)
