from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QStandardItemModel
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QLineEdit,
                             QPushButton, QSpinBox, QTextEdit, QVBoxLayout,
                             QWidget)

from ..config import VideoCamConfig
from ..video.camera_settings import (apply_camera_controls,
                                     get_camera_capabilities,
                                     summarize_control_application)
from ..video.constants import (BRIGHTNESS_RANGE, DEFAULT_CAMERA_FPS,
                               DEFAULT_PIXEL_FORMAT, HEIGHT_RANGE, HUE_RANGE,
                               PIXEL_FORMAT_OPTIONS, SATURATION_RANGE,
                               WIDTH_RANGE)


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

        self.fps = self._init_fps(int(cam_cfg.FPS))
        self.width = self._init_width(int(cam_cfg.Width))
        self.height = self._init_height(int(cam_cfg.Height))
        self.brightness = self._init_brightness(int(cam_cfg.Brightness or 0))
        self.hue = self._init_hue(int(cam_cfg.Hue or 0))
        self.saturation = self._init_saturation(int(cam_cfg.Saturation or 100))
        self.pixel_format = self._init_pixel_format(getattr(cam_cfg, "PixelFormat", "") or DEFAULT_PIXEL_FORMAT)

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
        form.addRow("Auto-exposure", self.auto_exposure)
        form.addRow("Auto-focus", self.auto_focus)

        self.btn_refresh_caps = QPushButton("Refresh device capabilities")
        self.btn_apply = QPushButton("Apply settings")
        self.text = QTextEdit()
        self.text.setReadOnly(True)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(self.btn_refresh_caps)
        layout.addWidget(self.btn_apply)
        layout.addWidget(self.text)
        self.setLayout(layout)

        self.btn_refresh_caps.clicked.connect(self.refresh_capabilities)
        self.btn_apply.clicked.connect(self.on_apply)
        self.device_index.valueChanged.connect(lambda _: self.refresh_capabilities())
        self.devnode.editingFinished.connect(self.refresh_capabilities)
        self.refresh_capabilities()

    def _init_fps(self, fps: int) -> QComboBox:
        widget = QComboBox()
        self.fps = widget
        self._set_fps_choices([fps], fps)
        return widget

    def _init_width(self, value: int) -> QSpinBox:
        widget = QSpinBox()
        widget.setRange(*WIDTH_RANGE)
        widget.setValue(value)
        return widget

    def _init_height(self, value: int) -> QSpinBox:
        widget = QSpinBox()
        widget.setRange(*HEIGHT_RANGE)
        widget.setValue(value)
        return widget

    def _init_brightness(self, value: int) -> QSpinBox:
        widget = QSpinBox()
        widget.setRange(*BRIGHTNESS_RANGE)
        widget.setValue(value)
        widget.setEnabled(False)
        return widget

    def _init_hue(self, value: int) -> QSpinBox:
        widget = QSpinBox()
        widget.setRange(*HUE_RANGE)
        widget.setValue(value)
        widget.setEnabled(False)
        return widget

    def _init_saturation(self, value: int) -> QSpinBox:
        widget = QSpinBox()
        widget.setRange(*SATURATION_RANGE)
        widget.setValue(value)
        widget.setEnabled(False)
        return widget

    def _init_pixel_format(self, value: str) -> QComboBox:
        widget = QComboBox()
        widget.addItems(list(PIXEL_FORMAT_OPTIONS))
        pf = value.upper()
        idx = widget.findText(pf)
        widget.setCurrentIndex(idx if idx >= 0 else 0)
        return widget

    def _set_fps_choices(self, fps_values: list[int], current_fps: int):
        fps_sorted = sorted({int(x) for x in fps_values if int(x) > 0})
        if not fps_sorted:
            fps_sorted = [max(1, int(current_fps))]
        self.fps.clear()
        for f in fps_sorted:
            self.fps.addItem(str(f), f)
        idx = self.fps.findData(int(current_fps))
        self.fps.setCurrentIndex(idx if idx >= 0 else 0)

    def _set_pixel_format_enabled(self, supported_formats: list[str], current_pf: str):
        supported = {s.upper() for s in supported_formats}
        model = self.pixel_format.model()
        if isinstance(model, QStandardItemModel):
            for row in range(self.pixel_format.count()):
                item = model.item(row)
                if item is None:
                    continue
                label = self.pixel_format.itemText(row).upper()
                item.setEnabled(label in supported)
        current = current_pf.upper()
        idx = self.pixel_format.findText(current)
        if idx >= 0 and current in supported:
            self.pixel_format.setCurrentIndex(idx)
            return
        for row in range(self.pixel_format.count()):
            label = self.pixel_format.itemText(row).upper()
            if label in supported:
                self.pixel_format.setCurrentIndex(row)
                return
        self.pixel_format.setEnabled(False)

    def refresh_capabilities(self):
        dev = self.devnode.text().strip()
        idx = int(self.device_index.value())
        current_fps = int(self.fps.currentData() or DEFAULT_CAMERA_FPS)
        current_pf = self.pixel_format.currentText()
        caps = get_camera_capabilities(dev, idx)

        fps_values = caps.get("fps") or [current_fps]
        self._set_fps_choices(list(fps_values), current_fps)
        self.fps.setEnabled(bool(caps.get("fps")))

        pixel_formats = list(caps.get("pixel_formats") or [])
        self.pixel_format.setEnabled(bool(pixel_formats))
        if pixel_formats:
            self._set_pixel_format_enabled(pixel_formats, current_pf)
        else:
            self.text.append("INFO: Could not determine supported pixel formats for this device")

        brightness_range = caps.get("brightness_range")
        hue_range = caps.get("hue_range")
        saturation_range = caps.get("saturation_range")
        self.brightness.setEnabled(bool(brightness_range))
        self.hue.setEnabled(bool(hue_range))
        self.saturation.setEnabled(bool(saturation_range))
        if brightness_range:
            self.brightness.setRange(brightness_range[0], brightness_range[1])
            self.brightness.setValue(min(max(self.brightness.value(), brightness_range[0]), brightness_range[1]))
        if hue_range:
            self.hue.setRange(hue_range[0], hue_range[1])
            self.hue.setValue(min(max(self.hue.value(), hue_range[0]), hue_range[1]))
        if saturation_range:
            self.saturation.setRange(saturation_range[0], saturation_range[1])
            self.saturation.setValue(min(max(self.saturation.value(), saturation_range[0]), saturation_range[1]))

        auto_exposure_ok = bool(caps.get("supports_auto_exposure"))
        auto_focus_ok = bool(caps.get("supports_auto_focus"))
        self.auto_exposure.setEnabled(auto_exposure_ok)
        self.auto_focus.setEnabled(auto_focus_ok)
        if not auto_exposure_ok:
            self.auto_exposure.setChecked(False)
        if not auto_focus_ok:
            self.auto_focus.setChecked(False)

    def to_config(self) -> VideoCamConfig:
        c = VideoCamConfig()
        c.Enabled = self.enabled.isChecked()
        c.DeviceIndex = int(self.device_index.value())
        c.DevNode = self.devnode.text().strip()
        c.Label = self.label.text().strip()
        c.FPS = int(self.fps.currentData() or DEFAULT_CAMERA_FPS)
        c.Width = int(self.width.value())
        c.Height = int(self.height.value())
        c.Brightness = int(self.brightness.value()) if self.brightness.isEnabled() else None
        c.Hue = int(self.hue.value()) if self.hue.isEnabled() else None
        c.Saturation = int(self.saturation.value()) if self.saturation.isEnabled() else None
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
        if self.brightness.isEnabled():
            controls["brightness"] = cfg.Brightness
        if self.hue.isEnabled():
            controls["hue"] = cfg.Hue
        if self.saturation.isEnabled():
            controls["saturation"] = cfg.Saturation
        if self.pixel_format.isEnabled():
            controls["pixel_format"] = cfg.PixelFormat
        if self.auto_exposure.isEnabled():
            controls["auto_exposure"] = 1 if cfg.AutoExposure else 0
        if self.auto_focus.isEnabled():
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
