from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import List, Optional

from pylsl import StreamInfo
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
                             QComboBox, QDoubleSpinBox, QFileDialog,
                             QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                             QMainWindow, QMessageBox, QProgressDialog,
                             QPushButton, QSpinBox, QSplitter, QTableWidget,
                             QTableWidgetItem, QTabWidget, QTextEdit,
                             QVBoxLayout, QWidget)

from ..audio.devices import (default_input_device_index,
                             get_audio_device_capabilities,
                             is_input_config_supported, list_input_devices)
from ..config import AppConfig, VideoCamConfig, load_cfg
from ..lsl.labrecorder_rcs import LabRecorderRCS
from ..video.camera_settings import apply_camera_controls
from ..video.devices import list_video_devices
from .camera_panel import CameraPanel
from .preview_manager import PreviewManager
from .preview_panel import PreviewPanel
from .run_controller import RunController


class _ApplyAllThread(QThread):
    finished_apply = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, items: list[dict], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._items = items

    def run(self):
        failed_msgs: list[str] = []
        try:
            for item in self._items:
                label = item.get("label", "Camera")
                devnode = item.get("devnode", "")
                controls = item.get("controls") or {}
                if not controls:
                    continue
                rep = apply_camera_controls(devnode, controls)
                failed = rep.get("failed", {})
                if failed:
                    failed_items = ", ".join(f"{k}={v}" for k, v in failed.items())
                    failed_msgs.append(f"Camera {label}: failed to apply {failed_items}")
            self.finished_apply.emit(failed_msgs)
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    log_signal = pyqtSignal(str)
    preview_frame_signal = pyqtSignal(int, object)

    def __init__(self, cfg_path: Optional[str] = None):
        super().__init__()
        self.setWindowTitle("LSL AV Recorder (Audio via LSL, LabRecorder XDF)")
        self._recording_active = False
        self.logbox = QTextEdit()
        self.logbox.setReadOnly(True)
        self._show_debug = os.getenv("LSL_AV_RECORDER_DEBUG", "").strip().lower() in {
            "1",
            "true",
        }
        self.debug_logs = QCheckBox("Show debug logs")
        self.debug_logs.setChecked(self._show_debug)
        self.debug_logs.stateChanged.connect(self._on_debug_logs_changed)
        self.log_signal.connect(self._append_log)
        self._log_file = None
        self._log_lock = threading.Lock()
        self._log_path = None
        self.cfg: AppConfig = load_cfg(cfg_path) if cfg_path else load_cfg("example.cfg")
        self.controller: RunController = None

        form = QFormLayout()
        self.experiment = QLineEdit(self.cfg.Prompts.ExperimentName)
        self.subject = QLineEdit(self.cfg.Prompts.Subject)
        self.session = QLineEdit(self.cfg.Prompts.Session)
        self.block = QLineEdit(self.cfg.Prompts.Block)
        self.acq = QLineEdit(self.cfg.Prompts.Acquisition)
        self.run = QLineEdit(self.cfg.Prompts.Run)

        form.addRow("ExperimentName", self.experiment)
        form.addRow("Subject (%p)", self.subject)
        form.addRow("Session (%s)", self.session)
        form.addRow("Block/Task (%b)", self.block)
        form.addRow("Acquisition (%a)", self.acq)
        form.addRow("Run (%r)", self.run)

        btn_row = QHBoxLayout()
        self.btn_load = QPushButton("Load config")
        self.btn_add_camera = QPushButton("Add camera")
        self.btn_start = QPushButton("Start")
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setEnabled(False)
        btn_row.addWidget(self.btn_load)
        btn_row.addWidget(self.btn_add_camera)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_stop)

        self.tabs = QTabWidget()
        self.btn_remove_camera = QPushButton("Remove camera")
        self.labrec_rcs: Optional[LabRecorderRCS] = None

        # Audio tab
        audio_widget = QWidget()
        af = QFormLayout()
        self.audio_enabled = QCheckBox("Enable audio (LSL stream, no WAV)")
        self.audio_enabled.setChecked(bool(self.cfg.Audio.Enabled))
        self.audio_device = QComboBox()
        self.audio_sr = QComboBox()
        self.audio_bit = QComboBox()
        self.audio_ch = QSpinBox(); self.audio_ch.setRange(1, 16); self.audio_ch.setValue(self.cfg.Audio.Channels)
        self._audio_caps: dict = {}
        self._populate_audio_devices()
        self._refresh_audio_capabilities()
        self.audio_device.currentIndexChanged.connect(self._on_audio_device_changed)

        self.audio_stream_name = QLineEdit(getattr(self.cfg.Audio, "StreamName", "Audio") or "Audio")

        af.addRow(self.audio_enabled)
        af.addRow("Input device", self.audio_device)
        af.addRow("Sample rate", self.audio_sr)
        af.addRow("Bit depth", self.audio_bit)
        af.addRow("Channels", self.audio_ch)
        af.addRow("LSL stream name", self.audio_stream_name)
        audio_widget.setLayout(af)
        self.tabs.addTab(audio_widget, "Audio")

        # Buffering tab (writer thread settings)
        buffering_widget = QWidget()
        bf = QFormLayout()
        self.audio_buffer_seconds = QDoubleSpinBox()
        self.audio_buffer_seconds.setRange(0.0, 10.0)
        self.audio_buffer_seconds.setSingleStep(0.05)
        self.audio_buffer_seconds.setDecimals(3)
        self.audio_buffer_seconds.setValue(float(getattr(self.cfg.Buffering, "AudioBufferSeconds", 0.0)))
        self.video_buffer_frames = QSpinBox()
        self.video_buffer_frames.setRange(0, 10000)
        self.video_buffer_frames.setValue(int(getattr(self.cfg.Buffering, "VideoBufferFrames", 0)))
        self.writer_queue_size = QSpinBox()
        self.writer_queue_size.setRange(1, 100000)
        self.writer_queue_size.setValue(int(getattr(self.cfg.Buffering, "WriterQueueSize", 256)))
        self.writer_drop_policy = QComboBox()
        self.writer_drop_policy.addItem("Drop oldest (recommended)", "drop_oldest")
        self.writer_drop_policy.addItem("Drop newest (incoming)", "drop_newest")
        self.writer_drop_policy.addItem("Block capture thread", "block")
        policy = getattr(self.cfg.Buffering, "WriterDropPolicy", "drop_oldest")
        idx = self.writer_drop_policy.findData(policy)
        if idx >= 0:
            self.writer_drop_policy.setCurrentIndex(idx)
        bf.addRow("Audio buffer seconds", self.audio_buffer_seconds)
        bf.addRow("Video buffer frames", self.video_buffer_frames)
        bf.addRow("Writer queue size", self.writer_queue_size)
        bf.addRow("When full", self.writer_drop_policy)
        buffering_widget.setLayout(bf)
        self.tabs.addTab(buffering_widget, "Buffering")

        # LabRecorder tab
        labrec_widget = QWidget()
        lf = QFormLayout()
        self.labrec_enabled = QCheckBox("Enable LabRecorder RCS")
        self.labrec_enabled.setChecked(bool(self.cfg.LabRecorder.Enabled))
        self.labrec_host = QLineEdit(self.cfg.LabRecorder.Host)
        self.labrec_port = QSpinBox()
        self.labrec_port.setRange(1, 65535)
        self.labrec_port.setValue(int(self.cfg.LabRecorder.Port))
        self.labrec_connect_btn = QPushButton("Connect")
        self.labrec_disconnect_btn = QPushButton("Disconnect")
        self.labrec_disconnect_btn.setEnabled(False)
        self.labrec_status = QLabel("Disconnected")
        self.labrec_status.setStyleSheet("color: #b00020;")
        lf.addRow(self.labrec_enabled)
        lf.addRow("RCS host", self.labrec_host)
        lf.addRow("RCS port", self.labrec_port)
        lf.addRow(self.labrec_connect_btn)
        lf.addRow(self.labrec_disconnect_btn)
        lf.addRow("Status", self.labrec_status)
        lf.addRow(QLabel("LabRecorder Streams"))
        labrec_widget.setLayout(lf)
        self.tabs.addTab(labrec_widget, "LabRecorder")

        self.lsl_discover_btn = QPushButton("Discover streams")
        self.lsl_discover_btn.setEnabled(False)
        self.lsl_streams_table = QTableWidget()
        self.lsl_streams_table.setEnabled(False)
        self.lsl_streams_table.setColumnCount(8)
        self.lsl_streams_table.setHorizontalHeaderLabels([
            "Record", "Name", "Type", "Channels", "SRate", "Source ID", "UID", "Host"
        ])
        self.lsl_streams_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.lsl_streams_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.lsl_streams_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.lsl_streams_table.horizontalHeader().setStretchLastSection(True)
        lf.addRow(self.lsl_discover_btn)
        lf.addRow(self.lsl_streams_table)

        # Preview wall
        self.preview_panel = PreviewPanel()
        self.preview_mgr = PreviewManager(self)
        self.preview_frame_signal.connect(self.preview_mgr.on_frame)
        self._cap_load_count = 0
        self._cap_load_dialog: Optional[QProgressDialog] = None
        self._cap_dialog_shown_at: Optional[float] = None
        self._cap_dialog_min_ms = 600
        self._apply_count = 0
        self._apply_dialog: Optional[QProgressDialog] = None
        self._start_progress_dialog: Optional[QProgressDialog] = None
        self._start_apply_thread: Optional[_ApplyAllThread] = None
        self._pending_start_warnings: list[str] = []
        self._preview_refresh_timer = QTimer(self)
        self._preview_refresh_timer.setSingleShot(True)
        self._preview_refresh_timer.timeout.connect(self._do_refresh_previews_from_panels)

        # Camera tabs
        self.cam_panels = []
        self.max_cams = 4
        self._init_camera_tabs()

        left = QWidget()
        left_layout = QVBoxLayout()
        left_layout.addLayout(form)
        left_layout.addLayout(btn_row)
        left_layout.addWidget(self.tabs)
        left_layout.addWidget(QLabel("Log"))
        left_layout.addWidget(self.debug_logs)
        left_layout.addWidget(self.logbox)
        left.setLayout(left_layout)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(self.preview_panel)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)

        root = QWidget()
        root_layout = QVBoxLayout()
        root_layout.addWidget(splitter)
        root.setLayout(root_layout)
        self.setCentralWidget(root)

        self.btn_load.clicked.connect(self.on_load)
        self.btn_add_camera.clicked.connect(self._on_add_camera)
        self.btn_start.clicked.connect(self.on_start)
        self.btn_stop.clicked.connect(self.on_stop)
        self.labrec_connect_btn.clicked.connect(self.on_connect_labrecorder)
        self.labrec_disconnect_btn.clicked.connect(self.on_disconnect_labrecorder)
        self.labrec_enabled.stateChanged.connect(self._update_labrecorder_controls)
        self.lsl_discover_btn.clicked.connect(self.on_discover_lsl_streams)
        self._update_labrecorder_controls()

    def _refresh_previews_from_panels(self):
        if self._preview_refresh_timer.isActive():
            self._preview_refresh_timer.stop()
        self._preview_refresh_timer.start(200)

    def _do_refresh_previews_from_panels(self):
        # Don't reconfigure preview workers while a run is active/recording.
        if not self.btn_start.isEnabled():
            return
        if not self.cfg.Video.Enabled:
            self.preview_mgr.stop_all_previews()
            return

        self.preview_mgr.stop_all_previews()
        for panel in self.cam_panels:
            # Skip a panel whose own capability probe is still running
            if getattr(panel, "_caps_loading", False):
                continue
            cam_cfg = panel.to_config()
            if cam_cfg.Enabled:
                self.preview_mgr.start_cam_preview(cam_cfg)

    def _format_log_line(self, msg: str, loglevel: str) -> str:
        now = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        return f"{now} {loglevel}: {msg}"

    def _append_log(self, line: str):
        self.logbox.append(line)

    def _write_log_line(self, line: str):
        if not self._log_file:
            return
        with self._log_lock:
            try:
                self._log_file.write(line + "\n")
                self._log_file.flush()
            except Exception:
                pass

    def log(self, msg: str, loglevel: str = "INFO"):
        if loglevel == "DEBUG" and not self._show_debug:
            return
        line = self._format_log_line(msg, loglevel)
        self._write_log_line(line)
        if QThread.currentThread() != self.thread():
            self.log_signal.emit(line)
            return
        self._append_log(line)

    def _on_debug_logs_changed(self, _state: int):
        self._show_debug = self.debug_logs.isChecked()

    def _open_run_log(self):
        self._close_run_log()
        if not self.controller:
            return
        try:
            self._log_path = os.path.join(self.controller.outdir, "run.log")
            self._log_file = open(self._log_path, "a", encoding="utf-8")
        except Exception as exc:
            self._log_file = None
            self._log_path = None
            self.log(f"Could not open run log: {exc}", loglevel="WARNING")

    def _close_run_log(self):
        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass
        self._log_file = None
        self._log_path = None

    def _ensure_progress_dialog(self, title: str, message: str) -> QProgressDialog:
        dlg = QProgressDialog(message, None, 0, 0, self)
        dlg.setWindowTitle(title)
        dlg.setCancelButton(None)
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        return dlg

    def _show_caps_dialog(self):
        if self._cap_load_dialog is None:
            self._cap_load_dialog = self._ensure_progress_dialog(
                "Loading", "Loading camera capabilities..."
            )
        self._cap_load_dialog.setLabelText("Loading camera capabilities...")
        self._cap_load_dialog.setRange(0, 100)
        self._cap_load_dialog.setValue(0)
        self._cap_load_dialog.show()
        self._cap_dialog_shown_at = time.monotonic()
        QApplication.processEvents()

    def _hide_caps_dialog(self):
        if self._cap_load_dialog:
            if self._cap_dialog_shown_at is None:
                self._cap_load_dialog.hide()
                return
            elapsed_ms = (time.monotonic() - self._cap_dialog_shown_at) * 1000.0
            remaining = max(0, int(self._cap_dialog_min_ms - elapsed_ms))
            if remaining > 0:
                QTimer.singleShot(remaining, self._cap_load_dialog.hide)
            else:
                self._cap_load_dialog.hide()
            self._cap_dialog_shown_at = None

    def _on_caps_load_started(self):
        self._cap_load_count += 1
        if self._cap_load_count == 1:
            self._show_caps_dialog()

    def _on_caps_load_finished(self):
        self._cap_load_count = max(0, self._cap_load_count - 1)
        if self._cap_load_count == 0:
            self._hide_caps_dialog()

    def _on_caps_load_progress(self, pct: int, msg: str):
        if self._cap_load_dialog is None:
            self._show_caps_dialog()
        if msg:
            self._cap_load_dialog.setLabelText(msg)
        self._cap_load_dialog.setRange(0, 100)
        self._cap_load_dialog.setValue(max(0, min(100, int(pct))))

    def _show_apply_dialog(self):
        if self._apply_dialog is None:
            self._apply_dialog = self._ensure_progress_dialog(
                "Applying", "Applying camera settings..."
            )
        self._apply_dialog.setLabelText("Applying camera settings...")
        self._apply_dialog.show()
        QApplication.processEvents()

    def _hide_apply_dialog(self):
        if self._apply_dialog:
            self._apply_dialog.hide()

    def _on_apply_started(self):
        self._apply_count += 1
        if self._apply_count == 1:
            self._show_apply_dialog()

    def _on_apply_finished(self):
        self._apply_count = max(0, self._apply_count - 1)
        if self._apply_count == 0:
            self._hide_apply_dialog()

    def _show_start_progress(self, message: str):
        if self._start_progress_dialog is None:
            self._start_progress_dialog = self._ensure_progress_dialog("Working", message)
        self._start_progress_dialog.setLabelText(message)
        self._start_progress_dialog.show()
        QApplication.processEvents()

    def _hide_start_progress(self):
        if self._start_progress_dialog:
            self._start_progress_dialog.hide()

    def _populate_audio_devices(self):
        self.audio_device.blockSignals(True)
        self.audio_device.clear()
        devs = list_input_devices()
        default_idx = default_input_device_index()
        self.audio_device.addItem("(default)", None)
        for d in devs:
            self.audio_device.addItem(f"[{d['index']}] {d['name']}", d["index"])
        if self.cfg.Audio.Device:
            try:
                di = int(self.cfg.Audio.Device)
                idx = self.audio_device.findData(di)
                if idx >= 0:
                    self.audio_device.setCurrentIndex(idx)
            except ValueError:
                pass
        elif default_idx is not None:
            idx = self.audio_device.findData(default_idx)
            if idx >= 0:
                self.audio_device.setCurrentIndex(idx)
        self.audio_device.blockSignals(False)

    def _on_audio_device_changed(self, _index: int):
        self._refresh_audio_capabilities()

    def _refresh_audio_capabilities(
        self,
        preferred_samplerate: Optional[int] = None,
        preferred_bitdepth: Optional[int] = None,
    ):
        """Probe the selected audio device's actual supported sample rates/bit depths
        and repopulate the Sample rate / Bit depth combos with only valid choices, so
        the GUI can never offer (and silently have overridden at record time) a
        combination the device doesn't support."""
        device = self.audio_device.currentData()
        if preferred_samplerate is None:
            preferred_samplerate = self.audio_sr.currentData()
        if preferred_samplerate is None:
            preferred_samplerate = int(self.cfg.Audio.SampleRate)
        if preferred_bitdepth is None:
            preferred_bitdepth = self.audio_bit.currentData()
        if preferred_bitdepth is None:
            preferred_bitdepth = int(self.cfg.Audio.BitDepth)
        preferred_channels = int(self.audio_ch.value()) or int(self.cfg.Audio.Channels)

        caps = get_audio_device_capabilities(device, channels=preferred_channels)
        self._audio_caps = caps

        rates = caps.get("samplerates") or []
        self.audio_sr.blockSignals(True)
        self.audio_sr.clear()
        if rates:
            for sr in rates:
                self.audio_sr.addItem(str(sr), sr)
            idx = self.audio_sr.findData(preferred_samplerate)
            if idx < 0:
                default_sr = caps.get("default_samplerate")
                idx = self.audio_sr.findData(default_sr) if default_sr in rates else 0
            self.audio_sr.setCurrentIndex(max(idx, 0))
        else:
            # Couldn't probe the device (e.g. none selected/available yet); keep the
            # configured value visible rather than silently discarding it.
            self.audio_sr.addItem(str(preferred_samplerate), preferred_samplerate)
            self.audio_sr.setCurrentIndex(0)
        self.audio_sr.blockSignals(False)

        depths = caps.get("bitdepths") or []
        self.audio_bit.blockSignals(True)
        self.audio_bit.clear()
        if depths:
            for b in depths:
                self.audio_bit.addItem(str(b), b)
            idx = self.audio_bit.findData(preferred_bitdepth)
            self.audio_bit.setCurrentIndex(idx if idx >= 0 else 0)
        else:
            self.audio_bit.addItem(str(preferred_bitdepth), preferred_bitdepth)
            self.audio_bit.setCurrentIndex(0)
        self.audio_bit.blockSignals(False)

        max_ch = caps.get("max_channels") or 0
        self.audio_ch.setMaximum(max_ch if max_ch > 0 else 16)
        if max_ch > 0 and self.audio_ch.value() > max_ch:
            self.audio_ch.setValue(max_ch)

    def _init_camera_tabs(self):
        initial_count = self._determine_initial_camera_count()
        for i in range(initial_count):
            cam_cfg = self.cfg.Video.Cams[i] if i < len(self.cfg.Video.Cams) else VideoCamConfig()
            self._add_camera_panel(cam_cfg)

    def _determine_initial_camera_count(self) -> int:
        cfg_count = len(self.cfg.Video.Cams)
        if cfg_count > 0:
            return min(cfg_count, self.max_cams)

        detected = 0
        try:
            detected = len(list_video_devices())
        except Exception:
            detected = 0

        if detected > 1:
            return min(detected, self.max_cams)
        return 1

    def _update_add_camera_button(self):
        self.btn_add_camera.setEnabled(
            (len(self.cam_panels) < self.max_cams) and not self._recording_active
        )
        self._update_remove_buttons()

    def _update_remove_buttons(self):
        enabled = (len(self.cam_panels) > 1) and not self._recording_active
        for panel in self.cam_panels:
            panel.set_remove_enabled(enabled)

    def _add_camera_panel(self, cam_cfg: VideoCamConfig | None = None):
        if len(self.cam_panels) >= self.max_cams:
            return
        is_default = cam_cfg is None
        cam_cfg = cam_cfg or VideoCamConfig()
        panel = CameraPanel(cam_cfg)
        if is_default:
            panel.enabled.setChecked(True)
        panel.applyStarted.connect(self.preview_mgr.stop_all_previews)
        panel.applyStarted.connect(self._on_apply_started)
        panel.applyFinished.connect(self._on_apply_finished)
        panel.applyFinished.connect(self._refresh_previews_from_panels)
        panel.previewConfigChanged.connect(self._refresh_previews_from_panels)
        panel.capabilitiesLoadStarted.connect(self.preview_mgr.stop_all_previews)
        panel.capabilitiesLoadStarted.connect(self._on_caps_load_started)
        panel.capabilitiesLoadFinished.connect(self._on_caps_load_finished)
        panel.capabilitiesLoadFinished.connect(self._refresh_previews_from_panels)
        panel.capabilitiesLoadProgress.connect(self._on_caps_load_progress)
        panel.removeRequested.connect(self._on_remove_camera)
        self.cam_panels.append(panel)
        self.tabs.addTab(panel, f"Camera {len(self.cam_panels)}")
        self._update_add_camera_button()
        should_probe = (not is_default) or (len(self.cam_panels) == 1)
        if should_probe:
            panel.refresh_capabilities()

    def _on_add_camera(self):
        self._add_camera_panel()
        self._refresh_previews_from_panels()

    def _on_remove_camera(self, panel: CameraPanel):
        if self._recording_active or len(self.cam_panels) <= 1:
            return
        if panel not in self.cam_panels:
            return

        cam_index = self.cam_panels.index(panel)
        self.cam_panels.pop(cam_index)
        tab_index = self.tabs.indexOf(panel)
        if tab_index >= 0:
            self.tabs.removeTab(tab_index)
        panel.setParent(None)
        panel.deleteLater()

        for i, cam_panel in enumerate(self.cam_panels):
            idx = self.tabs.indexOf(cam_panel)
            if idx >= 0:
                self.tabs.setTabText(idx, f"Camera {i + 1}")

        self._update_add_camera_button()
        self._refresh_previews_from_panels()

    def pull_gui_into_cfg(self):
        self.cfg.Prompts.ExperimentName = self.experiment.text().strip()
        self.cfg.Prompts.Subject = self.subject.text().strip()
        self.cfg.Prompts.Session = self.session.text().strip()
        self.cfg.Prompts.Block = self.block.text().strip()
        self.cfg.Prompts.Acquisition = self.acq.text().strip()
        self.cfg.Prompts.Run = self.run.text().strip()

        self.cfg.Audio.Enabled = self.audio_enabled.isChecked()
        dev = self.audio_device.currentData()
        self.cfg.Audio.Device = str(dev) if dev is not None else None
        self.cfg.Audio.SampleRate = int(self.audio_sr.currentData())
        self.cfg.Audio.BitDepth = int(self.audio_bit.currentData())
        self.cfg.Audio.Channels = int(self.audio_ch.value())
        self.cfg.Audio.StreamName = self.audio_stream_name.text().strip() or "Audio"

        self.cfg.LabRecorder.Enabled = self.labrec_enabled.isChecked()
        self.cfg.LabRecorder.Host = self.labrec_host.text().strip() or self.cfg.LabRecorder.Host
        self.cfg.LabRecorder.Port = int(self.labrec_port.value())
        self.cfg.Video.Cams = []
        for panel in self.cam_panels:
            self.cfg.Video.Cams.append(panel.to_config())
        self.cfg.Video.MaxCams = max(self.cfg.Video.MaxCams, len(self.cfg.Video.Cams))
        self.cfg.Buffering.AudioBufferSeconds = float(self.audio_buffer_seconds.value())
        self.cfg.Buffering.VideoBufferFrames = int(self.video_buffer_frames.value())
        self.cfg.Buffering.WriterQueueSize = int(self.writer_queue_size.value())
        self.cfg.Buffering.WriterDropPolicy = str(self.writer_drop_policy.currentData())

    def on_load(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load config", ".", "CFG files (*.cfg);;All files (*)")
        if not path:
            return
        try:
            self.cfg = load_cfg(path)
            self._apply_cfg_to_gui()
            self.log(f"Loaded config: {path}")
        except Exception as e:
            QMessageBox.critical(self, "Load failed", str(e))

    def on_start(self):
        self.pull_gui_into_cfg()
        try:
            # Stop preview workers; recording will supply frames for preview.
            self.preview_mgr.stop_all_previews()
            self._begin_start_sequence()
        except Exception as e:
            QMessageBox.critical(self, "Start failed", str(e))

    def _begin_start_sequence(self):
        if self._start_apply_thread and self._start_apply_thread.isRunning():
            return
        items, warnings = self._collect_camera_apply_items()
        warnings.extend(self._validate_audio_settings())
        self._pending_start_warnings = warnings
        self.btn_start.setEnabled(False)
        if items:
            self._show_start_progress("Applying camera settings...")
            thread = _ApplyAllThread(items, parent=self)
            self._start_apply_thread = thread
            thread.finished_apply.connect(self._on_apply_all_finished)
            thread.failed.connect(self._on_apply_all_error)
            thread.start()
            return
        self._finish_start_after_apply([])

    def _collect_camera_apply_items(self) -> tuple[list[dict], list[str]]:
        items: list[dict] = []
        warnings: list[str] = []
        for panel in self.cam_panels:
            cam_cfg = panel.to_config()
            if not cam_cfg.Enabled:
                continue
            for msg in panel.validate_settings():
                warnings.append(f"Camera {cam_cfg.Label}: {msg}")
            controls = panel.build_controls()
            items.append(
                {
                    "label": cam_cfg.Label,
                    "devnode": cam_cfg.DevNode,
                    "controls": controls,
                }
            )
        return items, warnings

    def _validate_audio_settings(self) -> list[str]:
        if not self.audio_enabled.isChecked():
            return []
        device = self.audio_device.currentData()
        samplerate = int(self.audio_sr.currentData())
        bitdepth = int(self.audio_bit.currentData())
        channels = int(self.audio_ch.value())
        if is_input_config_supported(device, samplerate, channels, bitdepth):
            return []
        return [
            f"Audio: samplerate={samplerate}, bitdepth={bitdepth}, channels={channels} "
            "is not supported by the selected input device."
        ]

    def _on_apply_all_finished(self, failed_msgs: list[str]):
        self._hide_start_progress()
        if self._start_apply_thread:
            try:
                self._start_apply_thread.quit()
                self._start_apply_thread.wait(1000)
            except Exception:
                pass
            self._start_apply_thread = None
        self._finish_start_after_apply(failed_msgs)

    def _on_apply_all_error(self, msg: str):
        self._hide_start_progress()
        if self._start_apply_thread:
            try:
                self._start_apply_thread.quit()
                self._start_apply_thread.wait(1000)
            except Exception:
                pass
            self._start_apply_thread = None
        QMessageBox.critical(self, "Apply failed", msg)
        self.log(f"Apply failed: {msg}", loglevel="ERROR")
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._refresh_previews_from_panels()

    def _finish_start_after_apply(self, failed_msgs: list[str]):
        warnings = list(self._pending_start_warnings or [])
        warnings.extend(failed_msgs or [])
        self._pending_start_warnings = []
        if warnings:
            body = "Some settings may not be supported:\n\n"
            body += "\n".join(f"- {msg}" for msg in warnings)
            body += "\n\nPlease adjust settings before starting the run."
            QMessageBox.warning(self, "Settings warning", body)
            for msg in warnings:
                self.log(msg, loglevel="WARNING")
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)
            self._refresh_previews_from_panels()
            return
        self._start_recording()

    def _start_recording(self):
        try:
            self._show_start_progress("Starting recording...")
            lsl_streams = self._get_selected_lsl_streams()
            self.controller = RunController(
                self.cfg,
                status_cb=self.log,
                lsl_streams=lsl_streams,
                preview_release_cb=self._stop_preview_for_cam,
                preview_frame_cb=self.preview_frame_signal.emit,
            )
            self._open_run_log()
            self.controller.start()
            self.btn_start.setEnabled(False)
            self.btn_stop.setEnabled(True)
            self._recording_active = True
            self._update_add_camera_button()
        except Exception as e:
            QMessageBox.critical(self, "Start failed", str(e))
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)
            self._refresh_previews_from_panels()
        finally:
            self._hide_start_progress()

    def _stop_preview_for_cam(self, cam_cfg: VideoCamConfig) -> bool:
        cam_index = int(cam_cfg.DeviceIndex)
        if cam_index not in self.preview_mgr.workers:
            return False
        self.log(f"Stopping preview for camera {cam_cfg.Label} (index {cam_index})")
        return self.preview_mgr.stop_cam_preview(cam_index)

    def on_stop(self):
        try:
            self.preview_mgr.stop_all_previews()
            self.controller.stop()
            outdir = os.path.abspath(self.controller.outdir)
            msg = f"Results written to:\n{outdir}\n\nClose the app now?"
            confirm = QMessageBox.question(
                self,
                "Recording stopped",
                msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if confirm == QMessageBox.StandardButton.Yes:
                self.close()
        finally:
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)
            self._recording_active = False
            self._update_add_camera_button()
            if self.cfg.Video.Enabled:
                self._refresh_previews_from_panels()
            self._close_run_log()

    def on_connect_labrecorder(self):
        host = self.labrec_host.text().strip() or self.cfg.LabRecorder.Host
        port = int(self.labrec_port.value())
        if self.labrec_rcs and self.labrec_rcs.sock:
            self.log(f"LabRecorder RCS already connected at {host}:{port}")
            return
        rcs = LabRecorderRCS(host=host, port=port)
        try:
            rcs.connect()
            self.labrec_rcs = rcs
            self._set_labrecorder_status(connected=True, host=host, port=port)
            self.log(f"LabRecorder RCS connected at {host}:{port}")
        except Exception as e:
            self._set_labrecorder_status(connected=False)
            QMessageBox.critical(self, "LabRecorder RCS connection failed", str(e))

    def on_disconnect_labrecorder(self):
        if not self.labrec_rcs:
            return
        try:
            self.labrec_rcs.close()
        finally:
            self.labrec_rcs = None
            self._set_labrecorder_status(connected=False)
            self.log("LabRecorder RCS disconnected")

    def on_discover_lsl_streams(self):
        self.lsl_streams_table.setRowCount(0)
        try:
            from pylsl import resolve_streams
            streams = resolve_streams(wait_time=2.0)
            if not streams:
                self.lsl_streams_table.setRowCount(0)
                return
            self.lsl_streams_table.setRowCount(len(streams))
            for row, stream in enumerate(streams):
                chk = QTableWidgetItem()
                chk.setCheckState(Qt.CheckState.Unchecked)
                chk.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                chk.setData(Qt.ItemDataRole.UserRole, stream)
                self.lsl_streams_table.setItem(row, 0, chk)

                values = [
                    stream.name(),
                    stream.type(),
                    str(stream.channel_count()),
                    str(stream.nominal_srate()),
                    stream.source_id(),
                    stream.uid(),
                    stream.hostname(),
                ]
                for col, val in enumerate(values, start=1):
                    item = QTableWidgetItem(val)
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                    self.lsl_streams_table.setItem(row, col, item)
        except Exception as e:
            self.lsl_streams_table.setRowCount(0)
            QMessageBox.critical(self, "LSL stream discovery failed", str(e))

    def _get_selected_lsl_streams(self):
        selected: List[StreamInfo] = []
        for row in range(self.lsl_streams_table.rowCount()):
            item = self.lsl_streams_table.item(row, 0)
            if not item:
                continue
            if item.checkState() == Qt.CheckState.Checked:
                stream = item.data(Qt.ItemDataRole.UserRole)
                if stream is not None:
                    selected.append(stream)
        return selected

    def _update_labrecorder_controls(self):
        enabled = self.labrec_enabled.isChecked()
        self.labrec_host.setEnabled(enabled)
        self.labrec_port.setEnabled(enabled)
        self.labrec_connect_btn.setEnabled(enabled)
        self.labrec_disconnect_btn.setEnabled(enabled and bool(self.labrec_rcs and self.labrec_rcs.sock))
        connected = bool(self.labrec_rcs and self.labrec_rcs.sock)
        self.lsl_discover_btn.setEnabled(connected)
        self.lsl_streams_table.setEnabled(connected)

    def _set_labrecorder_status(self, connected: bool, host: str = "", port: int = 0):
        if connected:
            self.labrec_status.setText(f"Connected to {host}:{port}")
            self.labrec_status.setStyleSheet("color: #0b6a0b;")
            self.labrec_disconnect_btn.setEnabled(True)
            self.lsl_discover_btn.setEnabled(True)
            self.lsl_streams_table.setEnabled(True)
        else:
            self.labrec_status.setText("Disconnected")
            self.labrec_status.setStyleSheet("color: #b00020;")
            self.labrec_disconnect_btn.setEnabled(False)
            self.lsl_discover_btn.setEnabled(False)
            self.lsl_streams_table.setEnabled(False)
            self.lsl_streams_table.setRowCount(0)

    def _apply_cfg_to_gui(self):
        self.experiment.setText(self.cfg.Prompts.ExperimentName)
        self.subject.setText(self.cfg.Prompts.Subject)
        self.session.setText(self.cfg.Prompts.Session)
        self.block.setText(self.cfg.Prompts.Block)
        self.acq.setText(self.cfg.Prompts.Acquisition)
        self.run.setText(self.cfg.Prompts.Run)

        self.audio_enabled.setChecked(bool(self.cfg.Audio.Enabled))
        self.audio_ch.setValue(int(self.cfg.Audio.Channels))
        self._populate_audio_devices()
        self._refresh_audio_capabilities(
            preferred_samplerate=int(self.cfg.Audio.SampleRate),
            preferred_bitdepth=int(self.cfg.Audio.BitDepth),
        )
        self.audio_stream_name.setText(getattr(self.cfg.Audio, "StreamName", "Audio") or "Audio")

        self.labrec_enabled.setChecked(bool(self.cfg.LabRecorder.Enabled))
        self.labrec_host.setText(self.cfg.LabRecorder.Host)
        self.labrec_port.setValue(int(self.cfg.LabRecorder.Port))
        self._update_labrecorder_controls()

        self.audio_buffer_seconds.setValue(float(getattr(self.cfg.Buffering, "AudioBufferSeconds", 0.0)))
        self.video_buffer_frames.setValue(int(getattr(self.cfg.Buffering, "VideoBufferFrames", 0)))
        self.writer_queue_size.setValue(int(getattr(self.cfg.Buffering, "WriterQueueSize", 256)))
        policy = getattr(self.cfg.Buffering, "WriterDropPolicy", "drop_oldest")
        idx = self.writer_drop_policy.findData(policy)
        if idx >= 0:
            self.writer_drop_policy.setCurrentIndex(idx)

        self._rebuild_camera_tabs_from_cfg()
        self._refresh_previews_from_panels()

    def _rebuild_camera_tabs_from_cfg(self):
        for panel in list(self.cam_panels):
            idx = self.tabs.indexOf(panel)
            if idx >= 0:
                self.tabs.removeTab(idx)
            panel.setParent(None)
            panel.deleteLater()
        self.cam_panels = []

        initial_count = self._determine_initial_camera_count()
        for i in range(initial_count):
            cam_cfg = self.cfg.Video.Cams[i] if i < len(self.cfg.Video.Cams) else VideoCamConfig()
            self._add_camera_panel(cam_cfg)
