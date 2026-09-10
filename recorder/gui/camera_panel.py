from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QLineEdit,
                             QMessageBox, QPushButton, QSpinBox, QTextEdit,
                             QVBoxLayout, QWidget)

from ..config import VideoCamConfig
from ..video.camera_settings import (apply_camera_controls,
                                     get_camera_capabilities,
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
    progress = pyqtSignal(int, str)

    def __init__(
        self,
        devnode: str,
        device_index: int,
        device_name: Optional[str],
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._devnode = devnode
        self._device_index = device_index
        self._device_name = device_name

    def run(self):
        try:
            caps = get_camera_capabilities(
                self._devnode,
                self._device_index,
                device_name=self._device_name,
                progress_cb=lambda pct, msg: self.progress.emit(int(pct), str(msg)),
            )
            self.finished_caps.emit(caps)
        except Exception as exc:
            self.failed.emit(str(exc))


class _ApplyControlsThread(QThread):
    finished_apply = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(
        self,
        devnode: str,
        controls: dict[str, Any],
        device_name: Optional[str] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._devnode = devnode
        self._controls = controls
        self._device_name = device_name

    def run(self):
        try:
            rep = apply_camera_controls(
                devnode=self._devnode,
                controls=self._controls,
                device_name=self._device_name,
            )
            self.finished_apply.emit(rep)
        except Exception as exc:
            self.failed.emit(str(exc))


class CameraPanel(QWidget):
    log = pyqtSignal(str, str)
    previewConfigChanged = pyqtSignal()
    removeRequested = pyqtSignal(object)
    applyStarted = pyqtSignal()
    applyFinished = pyqtSignal()
    capabilitiesLoadStarted = pyqtSignal()
    capabilitiesLoadFinished = pyqtSignal()
    capabilitiesLoadProgress = pyqtSignal(int, str)

    _UNSELECTED_DEVICE_LABEL = "Select a camera..."
    _UNSET_VALUE_LABEL = "Not set"

    def __init__(
        self,
        cam_cfg: VideoCamConfig,
        parent: Optional[QWidget] = None,
        preselect_device: bool = True,
    ):
        super().__init__(parent)

        self._default_resolution = (int(cam_cfg.Width), int(cam_cfg.Height))
        self._modes_by_format: dict[str, list[tuple[int, int, set[int]]]] = {}
        # Flattened (pixel_format, width, height, fps) verified combinations
        self._combos: list[tuple[str, int, int, int]] = []
        self._combos_set: set[tuple[str, int, int, int]] = set()
        self._caps_loading = False
        self._caps_thread: Optional[_CapabilitiesThread] = None
        self._caps_from_cache = False
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
        # _tag()'s fallback for a since-cleared label field: whatever label
        # this panel actually started with (normally already a non-blank
        # default assigned by MainWindow._add_camera_panel before this panel
        # was even constructed)
        self._default_label = cam_cfg.Label.strip() or "?"

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

        self.btn_mode_help = QPushButton("? Supported combinations")
        self.btn_mode_help.setToolTip(
            "Show every FPS/resolution/pixel-format combination confirmed to "
            "work on this camera."
        )
        form.addRow("", self.btn_mode_help)

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

        # Controls that are meaningless without a selected device; grayed out until one is chosen
        self._device_dependent_widgets = [
            self.enabled,
            self.fps,
            self.resolution,
            self.pixel_format,
            self.btn_mode_help,
            self.auto_exposure,
            self.auto_focus,
            self.btn_refresh_caps,
            self.btn_apply,
        ]

        self._video_devices: list[dict[str, Any]] = []
        self._device_name: Optional[str] = cam_cfg.DeviceName
        self._populate_video_devices(
            preferred_index=cam_cfg.DeviceIndex,
            preferred_devnode=cam_cfg.DevNode,
            preferred_name=cam_cfg.DeviceName,
            allow_preselect=preselect_device,
        )
        self._set_device_dependent_controls_enabled(
            isinstance(self.device_name.currentData(), dict)
        )

        self.btn_refresh_devices.clicked.connect(self.refresh_video_devices)
        self.btn_refresh_caps.clicked.connect(self.refresh_capabilities)
        self.btn_apply.clicked.connect(self.on_apply)
        self.btn_mode_help.clicked.connect(self._show_supported_combinations)
        self.btn_remove.clicked.connect(lambda: self.removeRequested.emit(self))
        self.device_name.currentIndexChanged.connect(self._on_device_name_selected)
        self._selected_device_key = self._device_key(self.device_name.currentData())
        self.enabled.toggled.connect(self._on_enabled_toggled)
        self.fps.currentIndexChanged.connect(self._on_fps_changed)
        self.resolution.currentIndexChanged.connect(self._on_resolution_changed)
        self.pixel_format.currentIndexChanged.connect(self._on_pixel_format_changed)

    def _set_device_dependent_controls_enabled(self, enabled: bool):
        """Gray out (or restore) every control that is meaningless without a
        selected device. Restoring just returns them to interactive mode."""
        for widget in self._device_dependent_widgets:
            widget.setEnabled(enabled)

    def _tag(self) -> str:
        """Short prefix identifying which camera panel a log message is
        about, since several panels can share the same shared log stream."""
        return f"Camera <{self.label.text().strip() or self._default_label}> (index={self.device_index.value()})"

    def _log(self, msg: str, loglevel: str = "INFO"):
        """Log to this panel's own local text box and emit it up
        to MainWindow so it also reaches the persistent app/run logs."""
        tagged = f"{self._tag()}: {msg}"
        self.text.append(f"{loglevel}: {tagged}")
        self.log.emit(tagged, loglevel)

    def _on_enabled_toggled(self, checked: bool):
        self._log(f"Enabled = {checked}")
        self.previewConfigChanged.emit()

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
        self.pixel_format = widget
        pf = value.upper() if value else None
        self._set_pixel_format_choices([pf] if pf else [], pf)
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
        missing = [
            name
            for name, value in (
                ("pixel format", self._selected_pixel_format()),
                ("resolution", self._selected_resolution()),
                ("FPS", self._selected_fps()),
            )
            if value is None
        ]
        if missing:
            messages.append(
                f"Please select a {', '.join(missing)} for this camera before continuing."
            )
            return messages
        if not self._combos:
            return messages
        pf = self._selected_pixel_format()
        width, height = self._selected_resolution()
        fps = self._selected_fps()
        if (pf, width, height, fps) not in self._combos_set:
            messages.append(
                f"{width}x{height} @ {fps}fps with pixel format {pf} is not a "
                "confirmed-supported combination for this camera."
            )
        return messages

    def set_remove_enabled(self, enabled: bool):
        self.btn_remove.setEnabled(enabled)

    def set_settings_controls_enabled(self, enabled: bool):
        """Enable/disable Apply settings and Refresh device capabilities --
        both probe or reconfigure this camera's device, which must not run
        while a recording is active, since RunController's own capture may
        be holding that same device open."""
        has_device = isinstance(self.device_name.currentData(), dict)
        final = enabled and has_device
        self.btn_apply.setEnabled(final)
        self.btn_refresh_caps.setEnabled(final)

    def _set_fps_choices(self, fps_values: list[int], current_fps: int | None):
        fps_sorted = sorted({int(x) for x in fps_values if int(x) > 0})
        self.fps.blockSignals(True)
        self.fps.clear()
        self.fps.addItem(self._UNSET_VALUE_LABEL, None)
        for f in fps_sorted:
            self.fps.addItem(str(f), f)
        idx = self.fps.findData(int(current_fps)) if current_fps is not None else 0
        self.fps.setCurrentIndex(idx if idx >= 0 else 0)
        self.fps.blockSignals(False)

    def _selected_fps(self) -> Optional[int]:
        data = self.fps.currentData()
        return int(data) if data is not None else None

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
        self.resolution.blockSignals(True)
        self.resolution.clear()
        self.resolution.addItem(self._UNSET_VALUE_LABEL, None)
        selected_idx = 0
        for i, r in enumerate(ordered, start=1):
            self.resolution.addItem(self._resolution_label(r), r)
            # QComboBox.findData() does not reliably match tuple item data by
            # value in PyQt6 (only by object identity), so track the match
            # ourselves with a plain Python "==" while inserting instead.
            if selected_resolution is not None and r == selected_resolution:
                selected_idx = i
        self.resolution.setCurrentIndex(selected_idx)
        self.resolution.blockSignals(False)

    def _selected_resolution(self) -> Optional[tuple[int, int]]:
        data = self.resolution.currentData()
        if isinstance(data, tuple) and len(data) == 2:
            width, height = data
            return (int(width), int(height))
        return None

    def _set_pixel_format_choices(self, formats: list[str], selected: str | None):
        ordered = sorted({str(f).upper() for f in formats if f})
        self.pixel_format.blockSignals(True)
        self.pixel_format.clear()
        self.pixel_format.addItem(self._UNSET_VALUE_LABEL, None)
        selected_idx = 0
        for i, fmt in enumerate(ordered, start=1):
            self.pixel_format.addItem(fmt, fmt)
            if selected is not None and fmt == selected:
                selected_idx = i
        self.pixel_format.setCurrentIndex(selected_idx)
        self.pixel_format.blockSignals(False)

    def _selected_pixel_format(self) -> Optional[str]:
        data = self.pixel_format.currentData()
        return str(data).upper() if data else None

    def _build_combos(self) -> None:
        """Flatten _modes_by_format into the (pixel_format, width, height, fps)
        triples used to drive cascading selection and validation."""
        combos: list[tuple[str, int, int, int]] = []
        for fmt, modes in self._modes_by_format.items():
            for w, h, fps_values in modes:
                for f in fps_values:
                    combos.append((fmt, int(w), int(h), int(f)))
        self._combos = combos
        self._combos_set = set(combos)

    def _combos_matching(
        self,
        pixel_format: Optional[str] = None,
        resolution: Optional[tuple[int, int]] = None,
        fps: Optional[int] = None,
    ) -> list[tuple[str, int, int, int]]:
        return [
            c for c in self._combos
            if (pixel_format is None or c[0] == pixel_format)
            and (resolution is None or (c[1], c[2]) == resolution)
            and (fps is None or c[3] == fps)
        ]

    def _refresh_combo_choices(self, changed: Optional[str]) -> None:
        """Repopulate the FPS/resolution/pixel-format combos to show exactly
        the values compatible with whatever is currently selected, per the
        verified combination set.

        `changed` names the control the user just explicitly set, if any;
        its new value is treated as fixed and never re-examined here.
        The other two are then each recomputed in a fixed order
        (pixel format, then resolution, then FPS, skipping whichever one is `changed`),
        each filtered only by whichever of the three are already settled at
        that point. 
        At each step, the control's previous selection is kept only if
        it is still present among the newly computed options;
        otherwise it resets to "Not set" rather than jumping to an arbitrary fallback value.

        `changed=None` (initial load from a saved config) has no already
        fixed control, so the same walk starts with nothing settled
        and progressively accumulates constraints from whichever earlier
        controls in the fixed order still validly resolve.
        """
        if not self._combos:
            return

        getters = {
            "pixel_format": self._selected_pixel_format,
            "resolution": self._selected_resolution,
            "fps": self._selected_fps,
        }
        setters = {
            "pixel_format": lambda opts, sel: self._set_pixel_format_choices(opts, sel),
            "resolution": lambda opts, sel: self._set_resolution_choices(opts, sel),
            "fps": lambda opts, sel: self._set_fps_choices(opts, sel),
        }
        extractors = {
            "pixel_format": lambda c: c[0],
            "resolution": lambda c: (c[1], c[2]),
            "fps": lambda c: c[3],
        }

        settled: dict[str, object] = {}
        if changed is not None:
            settled[changed] = getters[changed]()

        for dim in ("pixel_format", "resolution", "fps"):
            if dim == changed:
                continue
            current = getters[dim]()
            options = sorted({extractors[dim](c) for c in self._combos_matching(**settled)})
            setters[dim](options, current if current in options else None)
            settled[dim] = getters[dim]()

        self.pixel_format.setEnabled(True)
        self.resolution.setEnabled(True)
        self.fps.setEnabled(True)

    def _show_supported_combinations(self):
        if not self._modes_by_format:
            QMessageBox.information(
                self,
                "Supported combinations",
                "No verified modes available yet. Refresh device capabilities first.",
            )
            return
        lines = []
        for fmt in sorted(self._modes_by_format.keys()):
            lines.append(fmt + ":")
            for w, h, fps_values in sorted(self._modes_by_format[fmt], key=lambda m: (m[0], m[1])):
                fps_str = ", ".join(str(f) for f in sorted(fps_values))
                lines.append(f"    {w}x{h}: {fps_str} fps")
        QMessageBox.information(self, "Supported combinations", "\n".join(lines))

    def _populate_video_devices(
        self,
        preferred_index: int,
        preferred_devnode: str,
        preferred_name: Optional[str],
        allow_preselect: bool = True,
    ):
        """Populate the device combo."""
        self.device_name.clear()
        self._video_devices = list_video_devices()

        self.device_name.addItem(self._UNSELECTED_DEVICE_LABEL, None)
        selected_row = 0

        for row, dev in enumerate(self._video_devices, start=1):
            self.device_name.addItem(
                f"[{dev['index']}] {dev['name']}",
                dev,
            )
            if selected_row == 0:
                name_matches = bool(preferred_name) and str(dev.get("name")) == str(preferred_name)
                devnode_matches = (
                    allow_preselect and preferred_devnode and str(dev.get("devnode")) == str(preferred_devnode)
                )
                index_matches = allow_preselect and int(dev.get("index", -1)) == int(preferred_index)
                if name_matches or devnode_matches or index_matches:
                    selected_row = row

        if not self._video_devices and allow_preselect:
            self.device_name.addItem(
                f"[{preferred_index}] Manual device",
                {"index": int(preferred_index), "devnode": preferred_devnode, "name": "Manual device"},
            )
            selected_row = self.device_name.count() - 1

        self.device_name.setCurrentIndex(selected_row)
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

    @staticmethod
    def _device_key(dev) -> Optional[tuple]:
        """Stable identity for a device combo entry, used to tell a real device
        change from a device-list refresh that re-selects the same camera.
        Returns None for a non-device entry (e.g. the transient empty selection
        emitted while the combo is being cleared during a refresh)."""
        if not isinstance(dev, dict):
            return None
        return (
            str(dev.get("name") or ""),
            int(dev.get("index", -1)),
            str(dev.get("devnode") or ""),
        )

    def _on_device_name_selected(self):
        dev = self.device_name.currentData()
        self._sync_device_fields_from_combo()
        new_key = self._device_key(dev)
        if new_key is None:
            # "Select a camera..." placeholder
            # nothing to preview/probe/apply settings for until a real
            # device is chosen
            self._selected_device_key = None
            self._set_device_dependent_controls_enabled(False)
        elif new_key != self._selected_device_key:
            # Only wipe the brightness/hue/saturation latches when the selected device actually changed
            self._selected_device_key = new_key
            self._reset_control_defaults_state()
            self._set_device_dependent_controls_enabled(True)
            self._notify_device_changed(dev)
        self.previewConfigChanged.emit()

    def _notify_device_changed(self, dev):
        name = str(dev.get("name") or "?") if isinstance(dev, dict) else "?"
        index = int(self.device_index.value())
        self._log(f"Device changed to [{index}] {name}")

        box = QMessageBox(self)
        box.setWindowTitle("Camera device changed")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            f"Camera device changed to [{index}] {name}.\n\n"
            "Refresh device capabilities now to load its supported modes?"
        )
        refresh_btn = box.addButton("Refresh capabilities", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Skip", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(refresh_btn)
        box.exec()
        if box.clickedButton() is refresh_btn:
            self.refresh_capabilities()
        else:
            self._log(f"Skipped capability refresh for [{index}] {name}")

    def _reset_control_defaults_state(self):
        """Clear the "explicitly configured"/"already auto-defaulted" latches
        for brightness/hue/saturation on a device change.
        """
        self._brightness_configured = False
        self._brightness_auto_defaulted = False
        self._hue_configured = False
        self._hue_auto_defaulted = False
        self._saturation_configured = False
        self._saturation_auto_defaulted = False

    def refresh_video_devices(self, quiet: bool = False):
        """Re-resolve this panel's device against a fresh enumeration.

        `quiet=True` suppresses the "device changed, refresh capabilities?"
        popup that would otherwise fire whenever the resolved index shifts
        (even for the same device name, e.g. another camera being
        removed)."""
        dev = self.device_name.currentData()
        if isinstance(dev, dict):
            preferred_index = int(dev.get("index", self.device_index.value()))
            preferred_devnode = str(dev.get("devnode") or self.devnode.text().strip())
            preferred_name = str(dev.get("name") or self._device_name or "").strip() or None
        else:
            preferred_index = int(self.device_index.value())
            preferred_devnode = self.devnode.text().strip()
            preferred_name = self._device_name
        if quiet:
            self.device_name.blockSignals(True)
        try:
            self._populate_video_devices(
                preferred_index=preferred_index,
                preferred_devnode=preferred_devnode,
                preferred_name=preferred_name,
                # Only allow falling back to index/devnode matching when a real
                # device has already been selected
                allow_preselect=isinstance(dev, dict),
            )
        finally:
            if quiet:
                self.device_name.blockSignals(False)
                self._selected_device_key = self._device_key(self.device_name.currentData())

    def _on_fps_changed(self):
        self._log(f"FPS changed to {self.fps.currentData()}")
        self._refresh_combo_choices(changed="fps")
        self.previewConfigChanged.emit()

    def _on_resolution_changed(self):
        self._log(f"Resolution changed to {self.resolution.currentText()}")
        self._refresh_combo_choices(changed="resolution")
        self.previewConfigChanged.emit()

    def _on_pixel_format_changed(self):
        self._log(f"Pixel format changed to {self.pixel_format.currentText()}")
        self._refresh_combo_choices(changed="pixel_format")
        self.previewConfigChanged.emit()

    def _resolve_device_identity_by_name(
        self,
        name: Optional[str],
        fallback_index: int,
        fallback_devnode: str,
    ) -> tuple[int, str]:
        """On macOS, resolve the current index/devnode for a device by name
        against `devices` (this panel's cached device list from the last
        populate/refresh), rather than trusting self.device_index/self.devnode as-is.

        Falls back to the given values if no device with this name is found
        in `devices` (or off Mac)."""
        if not IS_MAC or not name:
            # Skip if non-MacOS or no device name provided 
            return fallback_index, fallback_devnode
        for dev in self._video_devices:
            if str(dev.get("name")) == name:
                return int(dev.get("index", fallback_index)), str(dev.get("devnode") or fallback_devnode)
        return fallback_index, fallback_devnode

    def refresh_capabilities(self):
        if self._caps_loading:
            return
        dev = self.devnode.text().strip()
        idx = int(self.device_index.value())
        dev_info = self.device_name.currentData()
        device_name = None
        if isinstance(dev_info, dict):
            device_name = str(dev_info.get("name") or "").strip() or None
        if not device_name:
            device_name = self._device_name
        idx, dev = self._resolve_device_identity_by_name(device_name, idx, dev)
        self._caps_loading = True
        # Logged as its own event before the probe starts,
        # so if the app goes down mid-probe,
        # the log still shows exactly which device/step was being probed.
        self._log(f"Starting capability refresh for [{idx}] {device_name or '?'} (devnode={dev})")
        self.capabilitiesLoadStarted.emit()

        thread = _CapabilitiesThread(dev, idx, device_name, parent=self)
        self._caps_thread = thread
        thread.finished_caps.connect(self._on_capabilities_ready)
        thread.failed.connect(self._on_capabilities_error)
        thread.progress.connect(self._on_capabilities_progress)
        thread.start()

    def _on_capabilities_ready(self, caps: dict):
        self.setUpdatesEnabled(False)
        self._caps_from_cache = bool(caps.pop("_from_cache", False))

        self._modes_by_format = {}
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
        self._build_combos()

        if self._combos:
            # _refresh_combo_choices reads whatever is still selected in each
            # combo (left over from construction or the previous refresh) and
            # keeps it if the newly-verified data still supports it;
            # otherwise, reset to "Not set" otherwise
            self._refresh_combo_choices(changed=None)
        else:
            fps_values = sorted({int(v) for v in (caps.get("fps") or []) if int(v) > 0})
            current_fps = self._selected_fps()
            self._set_fps_choices(fps_values, current_fps if current_fps in fps_values else None)
            self.fps.setEnabled(bool(fps_values))
            self._set_resolution_choices([], None)
            self.resolution.setEnabled(False)
            self._set_pixel_format_choices([], None)
            self.pixel_format.setEnabled(False)
            self._log("Could not determine supported modes for this device", loglevel="WARNING")

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

        self.setUpdatesEnabled(True)
        self._log(
            "Capabilities loaded "
            f"(from_cache={self._caps_from_cache}): {len(self._combos)} verified combination(s), "
            f"pixel_formats={sorted(self._modes_by_format.keys())}, "
            f"fps={sorted({int(v) for v in (caps.get('fps') or []) if int(v) > 0})}, "
            f"auto_exposure={bool(caps.get('supports_auto_exposure'))}, "
            f"auto_focus={bool(caps.get('supports_auto_focus'))}"
        )
        self._finish_capabilities_load()

    def _on_capabilities_error(self, msg: str):
        self._log(f"Failed to refresh capabilities: {msg}", loglevel="ERROR")
        self._finish_capabilities_load()

    def _on_capabilities_progress(self, pct: int, msg: str):
        self.capabilitiesLoadProgress.emit(int(pct), str(msg))

    def _finish_capabilities_load(self):
        self._caps_loading = False
        self.capabilitiesLoadFinished.emit()
        if self._caps_thread:
            try:
                self._caps_thread.quit()
                self._caps_thread.deleteLater()
            except Exception:
                pass
            self._caps_thread = None

    def to_config(self) -> VideoCamConfig:
        c = VideoCamConfig()
        mode_selected = (
            self._selected_fps() is not None
            and self._selected_resolution() is not None
            and self._selected_pixel_format() is not None
        )
        c.Enabled = self.enabled.isChecked() and self._device_name is not None and mode_selected
        c.DeviceIndex = int(self.device_index.value())
        c.DevNode = self.devnode.text().strip()
        c.DeviceName = self._device_name
        c.DeviceIndex, c.DevNode = self._resolve_device_identity_by_name(
            c.DeviceName, c.DeviceIndex, c.DevNode
        )
        c.Label = self.label.text().strip()
        # Width/Height/FPS/PixelFormat are non-optional on VideoCamConfig, so
        # a still-unselected control needs some concrete placeholder value
        # here, but it is never acted on: c.Enabled above is already False
        # whenever any of them is unset, and every consumer (live preview,
        # recording) skips a disabled camera before ever reading these
        # fields, so an unsupported-by-the-device placeholder cannot leak into
        # an actual capture or get persisted as if it were a real selection.
        c.FPS = int(self._selected_fps() or DEFAULT_CAMERA_FPS)
        width, height = self._selected_resolution() or self._default_resolution
        c.Width = int(width)
        c.Height = int(height)
        c.Brightness = int(self.brightness.value()) if self.brightness.isEnabled() else None
        c.Hue = int(self.hue.value()) if self.hue.isEnabled() else None
        c.Saturation = int(self.saturation.value()) if self.saturation.isEnabled() else None
        c.PixelFormat = self._selected_pixel_format() or DEFAULT_PIXEL_FORMAT
        c.AutoExposure = self.auto_exposure.isChecked()
        c.AutoFocus = self.auto_focus.isChecked()
        return c

    def on_apply(self):
        messages = self.validate_settings()
        if messages:
            QMessageBox.warning(self, "Camera Settings Incomplete", "\n".join(messages))
            return

        dev = self.devnode.text().strip()
        # Gather controls from the UI
        controls = self.build_controls()

        if not controls:
            self._log("No controls to apply")
            return

        if self._apply_loading:
            return
        self._apply_loading = True
        self._log(f"Applying settings: {controls}")
        self.applyStarted.emit()

        thread = _ApplyControlsThread(
            devnode=dev,
            controls=controls,
            device_name=self._device_name,
            parent=self,
        )
        self._apply_thread = thread
        thread.finished_apply.connect(lambda rep: self._on_apply_done(dev, rep))
        thread.failed.connect(self._on_apply_error)
        thread.start()

    def _on_apply_done(self, dev: str, rep: dict):
        summary = summarize_control_application(
            dev,
            rep.get("applied", {}),
            rep.get("failed", {}),
            rep.get("unverified", {}),
        )
        for loglevel, msg in summary:
            self._log(msg, loglevel=loglevel)
        failed = rep.get("failed", {})
        if failed:
            failed_items = "\n".join(f"• {k} = {v}" for k, v in failed.items())
            body = (
                "Some camera settings could not be applied.\n\n"
                "Please adjust these values and try again:\n"
                f"{failed_items}"
            )
            QMessageBox.warning(self, "Camera Settings Warning", body)
        self._finish_apply()

    def _on_apply_error(self, msg: str):
        self._log(f"Failed to apply settings: {msg}", loglevel="ERROR")
        self._finish_apply()

    def _finish_apply(self):
        self._apply_loading = False
        self.applyFinished.emit()
        if self._apply_thread:
            try:
                self._apply_thread.quit()
                self._apply_thread.deleteLater()
            except Exception:
                pass
            self._apply_thread = None
