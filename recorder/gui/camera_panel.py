from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QLineEdit,
                             QPushButton, QSpinBox, QTextEdit, QVBoxLayout,
                             QWidget)

from ..config import VideoCamConfig
from ..video.camera_settings import (apply_camera_controls,
                                     get_camera_capabilities,
                                     probe_mac_mode_support,
                                     summarize_control_application)
from ..video.constants import (BRIGHTNESS_RANGE, DEFAULT_BRIGHTNESS,
                               DEFAULT_CAMERA_FPS, DEFAULT_HUE,
                               DEFAULT_PIXEL_FORMAT, DEFAULT_SATURATION,
                               HUE_RANGE, IS_MAC, SATURATION_RANGE,
                               V4L2_AUTO_EXPOSURE_MODE, V4L2_AUTO_FOCUS_MODE,
                               V4L2_MANUAL_EXPOSURE_MODE,
                               V4L2_MANUAL_FOCUS_MODE)
from ..video.devices import list_video_devices


class _CapabilitiesThread(QThread):
    finished_caps = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, devnode: str, device_index: int, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._devnode = devnode
        self._device_index = device_index

    def run(self):
        try:
            caps = get_camera_capabilities(self._devnode, self._device_index)
            self.finished_caps.emit(caps)
        except Exception as exc:
            self.failed.emit(str(exc))


class _ApplyControlsThread(QThread):
    finished_apply = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, devnode: str, controls: dict[str, Any], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._devnode = devnode
        self._controls = controls

    def run(self):
        try:
            rep = apply_camera_controls(self._devnode, self._controls)
            self.finished_apply.emit(rep)
        except Exception as exc:
            self.failed.emit(str(exc))


class CameraPanel(QWidget):
    log = pyqtSignal(str)
    previewConfigChanged = pyqtSignal()
    removeRequested = pyqtSignal(object)
    applyStarted = pyqtSignal()
    applyFinished = pyqtSignal()
    capabilitiesLoadStarted = pyqtSignal()
    capabilitiesLoadFinished = pyqtSignal()

    def __init__(self, cam_cfg: VideoCamConfig, parent: Optional[QWidget] = None):
        super().__init__(parent)

        self._default_resolution = (int(cam_cfg.Width), int(cam_cfg.Height))
        self._modes: list[tuple[int, int, set[int]]] = []
        self._modes_by_format: dict[str, list[tuple[int, int, set[int]]]] = {}
        self._mode_support_cache: dict[tuple[int, int, int, str], bool] = {}
        self._caps_loading = False
        self._caps_thread: Optional[_CapabilitiesThread] = None
        self._apply_loading = False
        self._apply_thread: Optional[_ApplyControlsThread] = None

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
        self._brightness_configured = cam_cfg.Brightness is not None
        self._brightness_auto_defaulted = False
        brightness_value = cam_cfg.Brightness if cam_cfg.Brightness is not None else DEFAULT_BRIGHTNESS
        self.brightness = self._init_brightness(int(brightness_value))
        self._hue_configured = cam_cfg.Hue is not None
        self._hue_auto_defaulted = False
        hue_value = cam_cfg.Hue if cam_cfg.Hue is not None else DEFAULT_HUE
        self.hue = self._init_hue(int(hue_value))
        self._saturation_configured = cam_cfg.Saturation is not None
        self._saturation_auto_defaulted = False
        saturation_value = cam_cfg.Saturation if cam_cfg.Saturation is not None else DEFAULT_SATURATION
        self.saturation = self._init_saturation(int(saturation_value))
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
        self._device_name: Optional[str] = cam_cfg.DeviceName
        self._populate_video_devices(cam_cfg.DeviceIndex, cam_cfg.DevNode, cam_cfg.DeviceName)

        self.btn_refresh_devices.clicked.connect(self.refresh_video_devices)
        self.btn_refresh_caps.clicked.connect(self.refresh_capabilities)
        self.btn_apply.clicked.connect(self.on_apply)
        self.btn_remove.clicked.connect(lambda: self.removeRequested.emit(self))
        self.device_name.currentIndexChanged.connect(self._on_device_name_selected)
        self.enabled.toggled.connect(lambda _: self.previewConfigChanged.emit())
        self.fps.currentIndexChanged.connect(self._on_fps_changed)
        self.resolution.currentIndexChanged.connect(self._on_resolution_changed)
        self.pixel_format.currentIndexChanged.connect(self._on_pixel_format_changed)

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
        pf = value.upper()
        widget.addItems([pf] if pf else [])
        idx = widget.findText(pf) if pf else -1
        widget.setCurrentIndex(idx if idx >= 0 else 0)
        return widget

    def build_controls(self) -> dict[str, Any]:
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
        return controls

    def validate_settings(self) -> list[str]:
        messages: list[str] = []
        if not self._modes and not self._modes_by_format:
            return messages
        fps = int(self.fps.currentData() or DEFAULT_CAMERA_FPS)
        width, height = self._selected_resolution()
        pf = self.pixel_format.currentText().strip().upper()
        fmt_info = f" with pixel format {pf}" if pf else ""
        if self._modes_by_format and pf and pf not in self._modes_by_format:
            messages.append(f"Pixel format {pf} is not supported for this camera.")
            return messages
        if self._modes:
            if not any(mw == width and mh == height for mw, mh, _ in self._modes):
                messages.append(f"Resolution {width}x{height} is not supported{fmt_info}.")
                return messages
            supported_fps = self._fps_for_resolution((width, height))
            if fps not in supported_fps:
                if supported_fps:
                    fps_list = ", ".join(str(v) for v in supported_fps)
                    messages.append(
                        f"FPS {fps} is not supported for {width}x{height}{fmt_info}. "
                        f"Supported FPS: {fps_list}."
                    )
                else:
                    messages.append(f"FPS {fps} is not supported for {width}x{height}{fmt_info}.")
        return messages

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
        compatible = []
        for w, h, _fps_values in self._modes:
            if fps in self._fps_for_resolution((w, h)):
                compatible.append((w, h))

        if compatible:
            current = self._selected_resolution() if prefer_current else None
            self._set_resolution_choices(compatible, selected_resolution=current)
            self.resolution.setEnabled(True)
            return

        if not self._modes:
            self._set_resolution_choices([self._default_resolution], selected_resolution=self._default_resolution)
            self.resolution.setEnabled(False)
            return

        # Fallback to a valid mode if the selected FPS has no compatible resolutions.
        w, h, fps_values = self._modes[0]
        fps_values = sorted({int(f) for f in fps_values if int(f) > 0})
        fallback_fps = int(fps_values[0]) if fps_values else DEFAULT_CAMERA_FPS
        compatible = [
            (mw, mh)
            for mw, mh, mfps in self._modes
            if fallback_fps in mfps
        ]
        self._set_resolution_choices(compatible or [(w, h)], selected_resolution=(w, h))
        self.resolution.setEnabled(True)
        self._set_fps_choices(sorted({int(f) for f in fps_values if int(f) > 0}) or [fallback_fps], fallback_fps)
        self.fps.setEnabled(True)

    def _fps_for_resolution(self, resolution: tuple[int, int]) -> list[int]:
        if not self._modes:
            return []
        w, h = resolution
        for mw, mh, fps_values in self._modes:
            if mw == w and mh == h:
                fps_list = sorted({int(f) for f in fps_values if int(f) > 0})
                if not IS_MAC or not fps_list:
                    return fps_list
                pf = self.pixel_format.currentText().strip()
                return [f for f in fps_list if self._is_mode_supported(w, h, f, pf)]
        return []

    def _set_modes_for_pixel_format(self, pixel_format: str) -> bool:
        fmt = str(pixel_format).upper()
        if not fmt:
            return False
        fmt_modes = self._modes_by_format.get(fmt)
        if fmt_modes:
            self._modes = fmt_modes
            return True
        if self._modes_by_format:
            # Fallback to first available format if current is unsupported.
            first_fmt = sorted(self._modes_by_format.keys())[0]
            self._modes = self._modes_by_format[first_fmt]
            self.pixel_format.blockSignals(True)
            idx = self.pixel_format.findText(first_fmt)
            if idx >= 0:
                self.pixel_format.setCurrentIndex(idx)
            self.pixel_format.blockSignals(False)
            return True
        self._modes = []
        return False

    def _is_mode_supported(self, width: int, height: int, fps: int, pixel_format: str) -> bool:
        if not IS_MAC:
            return True
        key = (int(width), int(height), int(fps), str(pixel_format).upper())
        cached = self._mode_support_cache.get(key)
        if cached is not None:
            return cached
        devnode = self.devnode.text().strip()
        device_index = int(self.device_index.value())
        ok = probe_mac_mode_support(
            devnode=devnode,
            device_index=device_index,
            width=int(width),
            height=int(height),
            fps=int(fps),
            pixel_format=str(pixel_format),
        )
        self._mode_support_cache[key] = ok
        return ok

    def _update_fps_choices_for_selected_resolution(self, prefer_current: bool):
        resolution = self._selected_resolution()
        fps_values = self._fps_for_resolution(resolution)
        if not fps_values:
            if not self._modes:
                return
            # Current resolution not supported: fall back to the first available mode.
            w, h, fallback_fps_values = self._modes[0]
            fallback_fps_values = sorted({int(f) for f in fallback_fps_values if int(f) > 0})
            if not fallback_fps_values:
                return
            self._set_resolution_choices([(w, h)], selected_resolution=(w, h))
            self.resolution.setEnabled(True)
            self._set_fps_choices(fallback_fps_values, fallback_fps_values[0])
            self.fps.setEnabled(True)
            return
        current_fps = int(self.fps.currentData() or DEFAULT_CAMERA_FPS)
        selected = current_fps if prefer_current else None
        if selected is None or selected not in fps_values:
            selected = fps_values[0]
        self._set_fps_choices(fps_values, selected)
        self.fps.setEnabled(True)

    def _populate_video_devices(
        self,
        preferred_index: int,
        preferred_devnode: str,
        preferred_name: Optional[str],
    ):
        self.device_name.clear()
        self._video_devices = list_video_devices()

        selected_row = -1
        for row, dev in enumerate(self._video_devices):
            self.device_name.addItem(
                f"[{dev['index']}] {dev['name']}",
                dev,
            )
            if selected_row < 0:
                name_matches = bool(preferred_name) and str(dev.get("name")) == str(preferred_name)
                devnode_matches = preferred_devnode and str(dev.get("devnode")) == str(preferred_devnode)
                index_matches = int(dev.get("index", -1)) == int(preferred_index)
                if name_matches or devnode_matches or index_matches:
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
        self._device_name = str(dev.get("name") or self._device_name or "").strip() or None
        self.device_index.blockSignals(True)
        self.device_index.setValue(idx)
        self.device_index.blockSignals(False)
        self.devnode.blockSignals(True)
        self.devnode.setText(devnode)
        self.devnode.blockSignals(False)

    def _on_device_name_selected(self):
        self._sync_device_fields_from_combo()
        self.text.append("INFO: Device changed. Click 'Refresh device capabilities' to load supported modes.")
        self.previewConfigChanged.emit()

    def refresh_video_devices(self):
        dev = self.device_name.currentData()
        if isinstance(dev, dict):
            preferred_index = int(dev.get("index", self.device_index.value()))
            preferred_devnode = str(dev.get("devnode") or self.devnode.text().strip())
            preferred_name = str(dev.get("name") or self._device_name or "").strip() or None
        else:
            preferred_index = int(self.device_index.value())
            preferred_devnode = self.devnode.text().strip()
            preferred_name = self._device_name
        self._populate_video_devices(preferred_index, preferred_devnode, preferred_name)

    def _on_fps_changed(self):
        self._update_resolution_choices_for_selected_fps(prefer_current=False)
        self.previewConfigChanged.emit()

    def _on_resolution_changed(self):
        if self._modes:
            self._update_fps_choices_for_selected_resolution(prefer_current=True)
        self.previewConfigChanged.emit()

    def _on_pixel_format_changed(self):
        if self._modes or self._modes_by_format:
            self._mode_support_cache.clear()
            self._set_modes_for_pixel_format(self.pixel_format.currentText())
            self._update_fps_choices_for_selected_resolution(prefer_current=True)
            self._update_resolution_choices_for_selected_fps(prefer_current=True)
        self.previewConfigChanged.emit()

    def refresh_capabilities(self):
        if self._caps_loading:
            return
        dev = self.devnode.text().strip()
        idx = int(self.device_index.value())
        self._caps_loading = True
        self.capabilitiesLoadStarted.emit()

        thread = _CapabilitiesThread(dev, idx, parent=self)
        self._caps_thread = thread
        thread.finished_caps.connect(self._on_capabilities_ready)
        thread.failed.connect(self._on_capabilities_error)
        thread.start()

    def _on_capabilities_ready(self, caps: dict):
        self._mode_support_cache.clear()

        raw_modes = caps.get("modes") or []
        self._modes = []
        self._modes_by_format = {}
        for m in raw_modes:
            try:
                width = int(m["width"])
                height = int(m["height"])
                fps_values = {int(v) for v in (m.get("fps") or []) if int(v) > 0}
                if width > 0 and height > 0 and fps_values:
                    self._modes.append((width, height, fps_values))
            except Exception:
                continue
        raw_modes_by_format = caps.get("modes_by_format") or {}
        for fmt, modes in raw_modes_by_format.items():
            try:
                fmt_modes = []
                for m in modes:
                    width = int(m["width"])
                    height = int(m["height"])
                    fps_values = {int(v) for v in (m.get("fps") or []) if int(v) > 0}
                    if width > 0 and height > 0 and fps_values:
                        fmt_modes.append((width, height, fps_values))
                if fmt_modes:
                    self._modes_by_format[str(fmt).upper()] = fmt_modes
            except Exception:
                continue

        current_fps = int(self.fps.currentData() or DEFAULT_CAMERA_FPS)
        current_pf = self.pixel_format.currentText()
        if self._modes:
            if self._modes_by_format:
                self._set_modes_for_pixel_format(current_pf)
            self._update_resolution_choices_for_selected_fps(prefer_current=False)
            self._update_fps_choices_for_selected_resolution(prefer_current=True)
        else:
            fps_values = caps.get("fps") or [current_fps]
            self._set_fps_choices(list(fps_values), current_fps)
            self.fps.setEnabled(bool(caps.get("fps")))
            self._set_resolution_choices([self._default_resolution], selected_resolution=self._default_resolution)
            self.resolution.setEnabled(False)

        pixel_formats = list(caps.get("pixel_formats") or [])
        if not pixel_formats and self._modes_by_format:
            pixel_formats = sorted(self._modes_by_format.keys())
        if pixel_formats:
            self.pixel_format.setEnabled(True)
            self.pixel_format.blockSignals(True)
            self.pixel_format.clear()
            for fmt in sorted({str(f).upper() for f in pixel_formats}):
                self.pixel_format.addItem(fmt)
            idx = self.pixel_format.findText(str(current_pf).upper())
            self.pixel_format.setCurrentIndex(idx if idx >= 0 else 0)
            self.pixel_format.blockSignals(False)
            if self._modes_by_format:
                if self._set_modes_for_pixel_format(self.pixel_format.currentText()):
                    self._update_resolution_choices_for_selected_fps(prefer_current=True)
                    self._update_fps_choices_for_selected_resolution(prefer_current=True)
        else:
            self.pixel_format.setEnabled(False)
            self.text.append("INFO: Could not determine supported pixel formats for this device")

        brightness_range = caps.get("brightness_range")
        brightness_default = caps.get("brightness_default")
        hue_range = caps.get("hue_range")
        hue_default = caps.get("hue_default")
        saturation_range = caps.get("saturation_range")
        saturation_default = caps.get("saturation_default")
        self.brightness.setEnabled(bool(brightness_range))
        self.hue.setEnabled(bool(hue_range))
        self.saturation.setEnabled(bool(saturation_range))
        if brightness_range:
            self.brightness.setRange(brightness_range[0], brightness_range[1])
            if (
                not self._brightness_configured
                and not self._brightness_auto_defaulted
                and brightness_default is not None
            ):
                new_value = min(max(int(brightness_default), brightness_range[0]), brightness_range[1])
                self.brightness.setValue(new_value)
                self._brightness_auto_defaulted = True
            else:
                self.brightness.setValue(
                    min(max(self.brightness.value(), brightness_range[0]), brightness_range[1])
                )
        if hue_range:
            self.hue.setRange(hue_range[0], hue_range[1])
            if (
                not self._hue_configured
                and not self._hue_auto_defaulted
                and hue_default is not None
            ):
                new_value = min(max(int(hue_default), hue_range[0]), hue_range[1])
                self.hue.setValue(new_value)
                self._hue_auto_defaulted = True
            else:
                self.hue.setValue(min(max(self.hue.value(), hue_range[0]), hue_range[1]))
        if saturation_range:
            self.saturation.setRange(saturation_range[0], saturation_range[1])
            if (
                not self._saturation_configured
                and not self._saturation_auto_defaulted
                and saturation_default is not None
            ):
                new_value = min(max(int(saturation_default), saturation_range[0]), saturation_range[1])
                self.saturation.setValue(new_value)
                self._saturation_auto_defaulted = True
            else:
                self.saturation.setValue(
                    min(max(self.saturation.value(), saturation_range[0]), saturation_range[1])
                )

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

        self._finish_capabilities_load()

    def _on_capabilities_error(self, msg: str):
        self.text.append(f"ERROR: Failed to refresh capabilities: {msg}")
        self._finish_capabilities_load()

    def _finish_capabilities_load(self):
        self._caps_loading = False
        self.capabilitiesLoadFinished.emit()
        if self._caps_thread:
            try:
                self._caps_thread.quit()
                self._caps_thread.wait(1000)
            except Exception:
                pass
            self._caps_thread = None

    def to_config(self) -> VideoCamConfig:
        c = VideoCamConfig()
        c.Enabled = self.enabled.isChecked()
        c.DeviceIndex = int(self.device_index.value())
        c.DevNode = self.devnode.text().strip()
        c.DeviceName = self._device_name
        if IS_MAC and c.DeviceName:
            devices = list_video_devices()
            for dev in devices:
                if str(dev.get("name")) == c.DeviceName:
                    c.DeviceIndex = int(dev.get("index", c.DeviceIndex))
                    c.DevNode = str(dev.get("devnode") or c.DevNode)
                    break
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
        controls = self.build_controls()

        if not controls:
            self.text.append("No controls to apply")
            return

        if self._apply_loading:
            return
        self._apply_loading = True
        self.applyStarted.emit()

        thread = _ApplyControlsThread(dev, controls, parent=self)
        self._apply_thread = thread
        thread.finished_apply.connect(lambda rep: self._on_apply_done(dev, rep))
        thread.failed.connect(self._on_apply_error)
        thread.start()

    def _on_apply_done(self, dev: str, rep: dict):
        summary = summarize_control_application(
            dev,
            rep.get("applied", {}),
            rep.get("failed", {}),
        )
        self.text.append(summary)
        self._finish_apply()

    def _on_apply_error(self, msg: str):
        self.text.append(f"ERROR: Failed to apply settings: {msg}")
        self._finish_apply()

    def _finish_apply(self):
        self._apply_loading = False
        self.applyFinished.emit()
        if self._apply_thread:
            try:
                self._apply_thread.quit()
                self._apply_thread.wait(1000)
            except Exception:
                pass
            self._apply_thread = None
