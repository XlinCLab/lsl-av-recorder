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
                               DEFAULT_PIXEL_FORMAT, HUE_RANGE,
                               PIXEL_FORMAT_OPTIONS, SATURATION_RANGE,
                               V4L2_AUTO_EXPOSURE_MODE, V4L2_AUTO_FOCUS_MODE,
                               V4L2_MANUAL_EXPOSURE_MODE,
                               V4L2_MANUAL_FOCUS_MODE)
from ..video.devices import list_video_devices


class CameraPanel(QWidget):
    log = pyqtSignal(str)
    previewConfigChanged = pyqtSignal()
    removeRequested = pyqtSignal(object)

    def __init__(self, cam_cfg: VideoCamConfig, parent: Optional[QWidget] = None):
        super().__init__(parent)

        self._default_resolution = (int(cam_cfg.Width), int(cam_cfg.Height))
        self._modes: list[tuple[int, int, set[int]]] = []

        self.enabled = QCheckBox("Enabled (starts preview)")
        self.enabled.setChecked(bool(cam_cfg.Enabled))

        self.device_name = QComboBox()
        self.device_index = QSpinBox()
        self.device_index.setRange(0, 32)
        self.device_index.setValue(cam_cfg.DeviceIndex)
        self.device_index.setVisible(False)

        self.devnode = QLineEdit(cam_cfg.DevNode)
        self.devnode.setVisible(False)
        self.label = QLineEdit(cam_cfg.Label)

        self.fps = self._init_fps(int(cam_cfg.FPS))
        self.resolution = self._init_resolution(self._default_resolution)
        self.brightness = self._init_brightness(int(cam_cfg.Brightness or 0))
        self.hue = self._init_hue(int(cam_cfg.Hue or 0))
        self.saturation = self._init_saturation(int(cam_cfg.Saturation or 100))
        self.pixel_format = self._init_pixel_format(getattr(cam_cfg, "PixelFormat", "") or DEFAULT_PIXEL_FORMAT)

        self.auto_exposure = QCheckBox("On")
        self.auto_exposure.setChecked(bool(cam_cfg.AutoExposure))
        self.auto_focus = QCheckBox("On")
        self.auto_focus.setChecked(bool(cam_cfg.AutoFocus))
        self._auto_exposure_values = {
            "auto": V4L2_AUTO_EXPOSURE_MODE,
            "manual": V4L2_MANUAL_EXPOSURE_MODE,
        }

        form = QFormLayout()
        form.addRow(self.enabled)
        form.addRow("Device", self.device_name)
        form.addRow("Label", self.label)
        form.addRow("FPS", self.fps)
        form.addRow("Resolution", self.resolution)
        form.addRow("Brightness", self.brightness)
        form.addRow("Hue", self.hue)
        form.addRow("Saturation", self.saturation)
        form.addRow("Pixel format", self.pixel_format)
        form.addRow("Auto-exposure", self.auto_exposure)
        form.addRow("Auto-focus", self.auto_focus)

        self.btn_refresh_devices = QPushButton("Refresh video devices")
        self.btn_refresh_caps = QPushButton("Refresh device capabilities")
        self.btn_apply = QPushButton("Apply settings")
        self.btn_remove = QPushButton("Remove camera")
        self.text = QTextEdit()
        self.text.setReadOnly(True)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(self.btn_refresh_devices)
        layout.addWidget(self.btn_refresh_caps)
        layout.addWidget(self.btn_apply)
        layout.addWidget(self.text)
        layout.addWidget(self.btn_remove)
        self.setLayout(layout)

        self._video_devices: list[dict[str, Any]] = []
        self._populate_video_devices(cam_cfg.DeviceIndex, cam_cfg.DevNode)

        self.btn_refresh_devices.clicked.connect(self.refresh_video_devices)
        self.btn_refresh_caps.clicked.connect(self.refresh_capabilities)
        self.btn_apply.clicked.connect(self.on_apply)
        self.btn_remove.clicked.connect(lambda: self.removeRequested.emit(self))
        self.device_name.currentIndexChanged.connect(self._on_device_name_selected)
        self.enabled.toggled.connect(lambda _: self.previewConfigChanged.emit())
        self.fps.currentIndexChanged.connect(self._on_fps_changed)
        self.resolution.currentIndexChanged.connect(lambda _: self.previewConfigChanged.emit())

        self.refresh_capabilities()

    def _init_fps(self, fps: int) -> QComboBox:
        widget = QComboBox()
        self.fps = widget
        self._set_fps_choices([fps], fps)
        return widget

    def _init_resolution(self, resolution: tuple[int, int]) -> QComboBox:
        widget = QComboBox()
        self.resolution = widget
        self._set_resolution_choices([resolution], resolution)
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

    def set_remove_enabled(self, enabled: bool):
        self.btn_remove.setEnabled(enabled)

    def _set_fps_choices(self, fps_values: list[int], current_fps: int):
        fps_sorted = sorted({int(x) for x in fps_values if int(x) > 0})
        if not fps_sorted:
            fps_sorted = [max(1, int(current_fps))]
        self.fps.blockSignals(True)
        self.fps.clear()
        for f in fps_sorted:
            self.fps.addItem(str(f), f)
        idx = self.fps.findData(int(current_fps))
        self.fps.setCurrentIndex(idx if idx >= 0 else 0)
        self.fps.blockSignals(False)

    def _resolution_label(self, resolution: tuple[int, int]) -> str:
        width, height = resolution
        return f"{width}x{height}"

    def _sort_resolutions_desc(self, resolutions: list[tuple[int, int]]) -> list[tuple[int, int]]:
        return sorted(set(resolutions), key=lambda r: (r[0] * r[1], r[0], r[1]), reverse=True)

    def _set_resolution_choices(
        self,
        resolutions: list[tuple[int, int]],
        selected_resolution: tuple[int, int] | None = None,
    ):
        ordered = self._sort_resolutions_desc(resolutions)
        if not ordered:
            ordered = [self._default_resolution]
        if selected_resolution not in ordered:
            selected_resolution = ordered[0]

        self.resolution.blockSignals(True)
        self.resolution.clear()
        for r in ordered:
            self.resolution.addItem(self._resolution_label(r), r)
        idx = self.resolution.findData(selected_resolution)
        self.resolution.setCurrentIndex(idx if idx >= 0 else 0)
        self.resolution.blockSignals(False)

    def _selected_resolution(self) -> tuple[int, int]:
        data = self.resolution.currentData()
        if isinstance(data, tuple) and len(data) == 2:
            width, height = data
            return (int(width), int(height))
        return self._default_resolution

    def _update_resolution_choices_for_selected_fps(self, prefer_current: bool):
        fps = int(self.fps.currentData() or DEFAULT_CAMERA_FPS)
        compatible = [
            (w, h)
            for w, h, fps_values in self._modes
            if fps in fps_values
        ]

        if compatible:
            current = self._selected_resolution() if prefer_current else None
            self._set_resolution_choices(compatible, selected_resolution=current)
            self.resolution.setEnabled(True)
            return

        self._set_resolution_choices([self._default_resolution], selected_resolution=self._default_resolution)
        self.resolution.setEnabled(False)

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

    def _populate_video_devices(self, preferred_index: int, preferred_devnode: str):
        self.device_name.clear()
        self._video_devices = list_video_devices()

        selected_row = -1
        for row, dev in enumerate(self._video_devices):
            self.device_name.addItem(
                f"[{dev['index']}] {dev['name']}",
                dev,
            )
            if selected_row < 0:
                devnode_matches = preferred_devnode and str(dev.get("devnode")) == str(preferred_devnode)
                index_matches = int(dev.get("index", -1)) == int(preferred_index)
                if devnode_matches or index_matches:
                    selected_row = row

        if self.device_name.count() == 0:
            self.device_name.addItem(
                f"[{preferred_index}] Manual device",
                {"index": int(preferred_index), "devnode": preferred_devnode, "name": "Manual device"},
            )
            selected_row = 0

        self.device_name.setCurrentIndex(max(0, selected_row))
        self._sync_device_fields_from_combo()

    def _sync_device_fields_from_combo(self):
        dev = self.device_name.currentData()
        if not isinstance(dev, dict):
            return
        idx = int(dev.get("index", self.device_index.value()))
        devnode = str(dev.get("devnode") or self.devnode.text().strip())
        self.device_index.blockSignals(True)
        self.device_index.setValue(idx)
        self.device_index.blockSignals(False)
        self.devnode.blockSignals(True)
        self.devnode.setText(devnode)
        self.devnode.blockSignals(False)

    def _on_device_name_selected(self):
        self._sync_device_fields_from_combo()
        self.refresh_capabilities()
        self.previewConfigChanged.emit()

    def refresh_video_devices(self):
        dev = self.device_name.currentData()
        if isinstance(dev, dict):
            preferred_index = int(dev.get("index", self.device_index.value()))
            preferred_devnode = str(dev.get("devnode") or self.devnode.text().strip())
        else:
            preferred_index = int(self.device_index.value())
            preferred_devnode = self.devnode.text().strip()
        self._populate_video_devices(preferred_index, preferred_devnode)

    def _on_fps_changed(self):
        self._update_resolution_choices_for_selected_fps(prefer_current=False)
        self.previewConfigChanged.emit()

    def refresh_capabilities(self):
        dev = self.devnode.text().strip()
        idx = int(self.device_index.value())
        current_fps = int(self.fps.currentData() or DEFAULT_CAMERA_FPS)
        current_pf = self.pixel_format.currentText()
        caps = get_camera_capabilities(dev, idx)

        raw_modes = caps.get("modes") or []
        self._modes = []
        for m in raw_modes:
            try:
                width = int(m["width"])
                height = int(m["height"])
                fps_values = {int(v) for v in (m.get("fps") or []) if int(v) > 0}
                if width > 0 and height > 0 and fps_values:
                    self._modes.append((width, height, fps_values))
            except Exception:
                continue

        if self._modes:
            fps_values = sorted({fps for _, _, fpss in self._modes for fps in fpss})
            self._set_fps_choices(fps_values, current_fps)
            self.fps.setEnabled(True)
            self._update_resolution_choices_for_selected_fps(prefer_current=False)
        else:
            fps_values = caps.get("fps") or [current_fps]
            self._set_fps_choices(list(fps_values), current_fps)
            self.fps.setEnabled(bool(caps.get("fps")))
            self._set_resolution_choices([self._default_resolution], selected_resolution=self._default_resolution)
            self.resolution.setEnabled(False)

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
        if auto_exposure_ok:
            auto_val = caps.get("exposure_auto_auto")
            manual_val = caps.get("exposure_auto_manual")
            self._auto_exposure_values = {
                "auto": auto_val if auto_val is not None else V4L2_AUTO_EXPOSURE_MODE,
                "manual": manual_val if manual_val is not None else V4L2_MANUAL_EXPOSURE_MODE,
            }

    def to_config(self) -> VideoCamConfig:
        c = VideoCamConfig()
        c.Enabled = self.enabled.isChecked()
        c.DeviceIndex = int(self.device_index.value())
        c.DevNode = self.devnode.text().strip()
        c.Label = self.label.text().strip()
        c.FPS = int(self.fps.currentData() or DEFAULT_CAMERA_FPS)
        width, height = self._selected_resolution()
        c.Width = int(width)
        c.Height = int(height)
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
            controls["auto_exposure"] = (
                self._auto_exposure_values["auto"]
                if cfg.AutoExposure
                else self._auto_exposure_values["manual"]
            )
        if self.auto_focus.isEnabled():
            controls["auto_focus"] = V4L2_AUTO_FOCUS_MODE if cfg.AutoFocus else V4L2_MANUAL_FOCUS_MODE

        if not controls:
            self.text.append("No controls to apply")
            return

        rep = apply_camera_controls(dev, controls)
        # Print summary of successfully applied and failed settings
        summary = summarize_control_application(
            dev,
            rep["applied"],
            rep["failed"],
        )
        self.text.append(summary)
