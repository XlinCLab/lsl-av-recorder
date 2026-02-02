from __future__ import annotations
from typing import Optional

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLineEdit, QPushButton, QFileDialog, QTabWidget, QTextEdit,
    QComboBox, QSpinBox, QLabel, QCheckBox, QMessageBox, QSplitter
)
from PyQt6.QtCore import Qt

from ..config import load_cfg, AppConfig, VideoCamConfig
from ..audio.devices import list_input_devices, default_input_device_index
from ..naming import build_paths  # keep global import too
from .camera_panel import CameraPanel
from .run_controller import RunController
from .preview_manager import PreviewManager
from .preview_panel import PreviewPanel

class MainWindow(QMainWindow):
    def __init__(self, cfg_path: Optional[str] = None):
        super().__init__()
        self.setWindowTitle("LSL AV Recorder (Audio via LSL, LabRecorder XDF)")

        self.cfg: AppConfig = load_cfg(cfg_path) if cfg_path else load_cfg("example.cfg")
        self.controller = RunController(self.cfg, status_cb=self.log)

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
        self.btn_start = QPushButton("Start")
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setEnabled(False)
        btn_row.addWidget(self.btn_load)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_stop)

        self.tabs = QTabWidget()

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

        # Camera tabs
        self.cam_panels = []
        for i in range(4):
            cam_cfg = self.cfg.Video.Cams[i] if i < len(self.cfg.Video.Cams) else VideoCamConfig()
            panel = CameraPanel(cam_cfg)
            self.cam_panels.append(panel)
            self.tabs.addTab(panel, f"Camera {i+1}")

        # Preview wall
        self.preview_panel = PreviewPanel()
        self.preview_wall = [self.preview_panel.labels[i] for i in range(4)]
        self.preview_mgr = PreviewManager(self)

        for panel in self.cam_panels:
            if panel.enabled.isChecked():
                self.preview_mgr.start_cam_preview(panel.to_config())

        self.logbox = QTextEdit()
        self.logbox.setReadOnly(True)

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
        self.btn_start.clicked.connect(self.on_start)
        self.btn_stop.clicked.connect(self.on_stop)

    def log(self, msg: str):
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

        for i, panel in enumerate(self.cam_panels):
            if i < len(self.cfg.Video.Cams):
                self.cfg.Video.Cams[i] = panel.to_config()
            else:
                self.cfg.Video.Cams.append(panel.to_config())

    def on_load(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load config", ".", "CFG files (*.cfg);;All files (*)")
        if not path:
            return
        try:
            self.cfg = load_cfg(path)
            self.controller = RunController(self.cfg, status_cb=self.log)
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

            self.preview_mgr.start_recording_all(
                out_dir=paths["base_dir"],
                base_name=paths["base_name"],
                video_container=self.cfg.Video.Container,
                codec=self.cfg.Video.Codec,
            )

            self.btn_start.setEnabled(False)
            self.btn_stop.setEnabled(True)
        except Exception as e:
            QMessageBox.critical(self, "Start failed", str(e))

    def on_stop(self):
        try:
            self.preview_mgr.stop_recording_all()
            self.controller.stop()
        finally:
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)
