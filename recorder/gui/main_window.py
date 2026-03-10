from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox,
                             QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                             QLineEdit, QMainWindow, QMessageBox, QPushButton,
                             QSpinBox, QSplitter, QTableWidget,
                             QTableWidgetItem, QTabWidget, QTextEdit,
                             QVBoxLayout, QWidget)

from ..audio.devices import default_input_device_index, list_input_devices
from ..config import AppConfig, VideoCamConfig, load_cfg
from ..lsl.labrecorder_rcs import LabRecorderRCS
from ..naming import build_paths  # keep global import too
from .camera_panel import CameraPanel
from .preview_manager import PreviewManager
from .preview_panel import PreviewPanel
from .run_controller import RunController
from ..video.devices import list_video_devices


class MainWindow(QMainWindow):
    def __init__(self, cfg_path: Optional[str] = None):
        super().__init__()
        self.setWindowTitle("LSL AV Recorder (Audio via LSL, LabRecorder XDF)")
        self._recording_active = False
        self.logbox = QTextEdit()
        self.logbox.setReadOnly(True)
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
        self._populate_audio_devices()

        self.audio_sr = QComboBox()
        for sr in [22050, 44100, 48000, 96000]:
            self.audio_sr.addItem(str(sr), sr)
        self.audio_sr.setCurrentText(str(self.cfg.Audio.SampleRate))

        self.audio_bit = QComboBox()
        for b in [16, 32, 64]:
            self.audio_bit.addItem(str(b), b)
        self.audio_bit.setCurrentText(str(self.cfg.Audio.BitDepth))

        self.audio_ch = QSpinBox(); self.audio_ch.setRange(1, 16); self.audio_ch.setValue(self.cfg.Audio.Channels)
        self.audio_stream_name = QLineEdit(getattr(self.cfg.Audio, "StreamName", "Audio") or "Audio")

        af.addRow(self.audio_enabled)
        af.addRow("Input device", self.audio_device)
        af.addRow("Sample rate", self.audio_sr)
        af.addRow("Bit depth", self.audio_bit)
        af.addRow("Channels", self.audio_ch)
        af.addRow("LSL stream name", self.audio_stream_name)
        audio_widget.setLayout(af)
        self.tabs.addTab(audio_widget, "Audio")

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

        # Camera tabs
        self.cam_panels = []
        self.max_cams = 4
        self._init_camera_tabs()

        # Preview wall
        self.preview_panel = PreviewPanel()
        self.preview_mgr = PreviewManager(self)
        # Show camera previews if video is enabled in config
        if self.cfg.Video.Enabled:
            for panel in self.cam_panels:
                if panel.enabled.isChecked():
                    self.preview_mgr.start_cam_preview(panel.to_config())

        left = QWidget()
        left_layout = QVBoxLayout()
        left_layout.addLayout(form)
        left_layout.addLayout(btn_row)
        left_layout.addWidget(self.tabs)
        left_layout.addWidget(QLabel("Log"))
        left_layout.addWidget(self.logbox)
        left.setLayout(left_layout)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(self.preview_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

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
        # Don't reconfigure preview workers while a run is active/recording.
        if not self.btn_start.isEnabled():
            return
        if not self.cfg.Video.Enabled:
            self.preview_mgr.stop_all_previews()
            return

        self.preview_mgr.stop_all_previews()
        for panel in self.cam_panels:
            cam_cfg = panel.to_config()
            if cam_cfg.Enabled:
                self.preview_mgr.start_cam_preview(cam_cfg)

    def log(self, msg: str, loglevel: str = "INFO"):
        now = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        msg = f"{now} {loglevel}: {msg}"
        self.logbox.append(msg)

    def _populate_audio_devices(self):
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
        cam_cfg = cam_cfg or VideoCamConfig()
        panel = CameraPanel(cam_cfg)
        panel.enabled.setChecked(True)
        panel.previewConfigChanged.connect(self._refresh_previews_from_panels)
        panel.removeRequested.connect(self._on_remove_camera)
        self.cam_panels.append(panel)
        self.tabs.addTab(panel, f"Camera {len(self.cam_panels)}")
        self._update_add_camera_button()

    def _on_add_camera(self):
        self._add_camera_panel()
        self._refresh_previews_from_panels()

    def _on_remove_camera(self, panel: CameraPanel):
        if self._recording_active or len(self.cam_panels) <= 1:
            return
        if panel not in self.cam_panels:
            return

        cam_index = self.cam_panels.index(panel)
        tab_index = cam_index + 1  # tab 0 is Audio
        self.cam_panels.pop(cam_index)
        self.tabs.removeTab(tab_index)
        panel.setParent(None)
        panel.deleteLater()

        for i, cam_panel in enumerate(self.cam_panels):
            self.tabs.setTabText(i + 1, f"Camera {i + 1}")

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

    def on_load(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load config", ".", "CFG files (*.cfg);;All files (*)")
        if not path:
            return
        try:
            self.cfg = load_cfg(path)
            self.log(f"Loaded config: {path}")
        except Exception as e:
            QMessageBox.critical(self, "Load failed", str(e))

    def on_start(self):
        self.pull_gui_into_cfg()
        try:
            self.controller = RunController(self.cfg, status_cb=self.log)
            self.controller.start()

            # Local import is intentional (protects against stale installs / name binding issues)
            from recorder.naming import build_paths as _build_paths
            paths = _build_paths(self.cfg.Output, self.cfg.Prompts)

            # Start video recording (only if video is enabled in config)
            if self.cfg.Video.Enabled:
                self.preview_mgr.start_recording_all(
                    out_dir=paths["base_dir"],
                    base_name=paths["base_name"],
                    video_container=self.cfg.Video.Container,
                    codec=self.cfg.Video.Codec,
                )

            self.btn_start.setEnabled(False)
            self.btn_stop.setEnabled(True)
            self._recording_active = True
            self._update_add_camera_button()
        except Exception as e:
            QMessageBox.critical(self, "Start failed", str(e))

    def on_stop(self):
        try:
            self.preview_mgr.stop_recording_all()
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
