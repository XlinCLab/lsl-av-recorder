from __future__ import annotations
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QFormLayout, QLineEdit, QSpinBox, QCheckBox, QHBoxLayout,
    QPushButton, QTextEdit, QGroupBox, QVBoxLayout
)
from PyQt6.QtCore import pyqtSignal

from ..config import VideoCamConfig
from ..video.v4l2_controls import apply_controls

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

        form = QFormLayout()
        form.addRow(self.enabled)
        form.addRow("DeviceIndex", self.device_index)
        form.addRow("DevNode", self.devnode)
        form.addRow("Label", self.label)
        form.addRow("FPS", self.fps)
        form.addRow("Width", self.width)
        form.addRow("Height", self.height)

        self.btn_apply = QPushButton("Apply (basic)")
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
        return c

    def on_apply(self):
        # keep minimal here; full control UI was in v0.2; you can re-add later
        dev = self.devnode.text().strip()
        rep = apply_controls(dev, {})
        self.text.append(f"Apply -> applied={rep['applied']} failed={rep['failed']}")
