from __future__ import annotations

import copy
import faulthandler
import json
import os
import shutil
import tempfile
import threading
import time
from dataclasses import asdict
from datetime import datetime
from typing import List, Optional

from pylsl import StreamInfo
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
                             QComboBox, QDoubleSpinBox, QFileDialog,
                             QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                             QMainWindow, QMessageBox, QProgressDialog,
                             QPushButton, QSizePolicy, QSpinBox, QSplitter,
                             QTableWidget, QTableWidgetItem, QTabWidget,
                             QTextEdit, QVBoxLayout, QWidget)

from ..audio.devices import (default_input_device_index,
                             get_audio_device_capabilities,
                             is_input_config_supported, list_input_devices)
from ..config import AppConfig, VideoCamConfig, load_cfg, save_cfg
from ..lsl.labrecorder_rcs import LabRecorderRCS
from ..utils.constants import _logs_path, _project_root
from ..utils.utils import get_environment_info
from ..video.camera_settings import apply_camera_controls
from ..video.devices import list_video_devices
from ..xdf.xdf_validation import ValidationReport, validate_test_recording
from ..xdf.xdf_writer import (FULL_BUFFER_BLOCK_THREAD_POLICY,
                              FULL_BUFFER_DEFAULT_POLICY,
                              FULL_BUFFER_DROP_NEWEST_POLICY,
                              FULL_BUFFER_DROP_OLDEST_POLICY)
from .camera_panel import CameraPanel
from .camera_worker import CAMERA_PREVIEW_STREAM_TYPE
from .preview_manager import PreviewManager, preview_key
from .preview_panel import PreviewPanel
from .run_controller import RunController


def build_config_log_payload(label: str, cfg: AppConfig) -> str:
    """Build a single JSON log line combining environment info
    with a full config snapshot."""
    payload = {
        "event": label,
        "environment": get_environment_info(_project_root()),
        "config": asdict(cfg),
    }
    return json.dumps(payload, indent=2, default=str, ensure_ascii=False)


def _exclude_camera_preview_streams(streams: List[StreamInfo]) -> List[StreamInfo]:
    """Drop this app's own camera-preview outlets from a list of resolved LSL streams.
    Selecting one to be recorded (via this app's own LSL stream table) will yield
    an empty stream: it only exists for the lifetime of the live preview and
    goes silent when Start is pressed."""
    return [s for s in streams if s.type() != CAMERA_PREVIEW_STREAM_TYPE]


def _default_camera_label(label: str, position: int) -> str:
    """Fall back to "Cam{position}" (1-based tab position) when `label` is blank."""
    return label.strip() or f"Cam{position}"


def _find_duplicate_camera_labels(labels: list[str]) -> list[str]:
    """Return each label that appears more than once in `labels`.
    RunController keys XDF video stream names/lookups by Label, so a
    duplicate would silently collide (one camera's stream or verified
    settings overwriting or misattributed to the other's)."""
    seen: set[str] = set()
    duplicates: set[str] = set()
    for label in labels:
        if label in seen:
            duplicates.add(label)
        seen.add(label)
    return sorted(duplicates)


def _recording_stream_rows(controller) -> list[tuple[str, str, str, str]]:
    """(name, type, device, details) rows describing what an active
    recording is capturing. "device" is the physical/hosting device behind
    each stream."""
    if controller is None:
        return []
    rows: list[tuple[str, str, str, str]] = []
    if controller.audio_enabled and controller.audio_settings:
        a = controller.audio_settings
        rows.append((
            a.stream_name, "Audio", a.device_name or "(default)",
            f"{a.samplerate:g} Hz, {a.channels} ch, {a.bitdepth}-bit",
        ))
    if controller.video_enabled:
        for cam in controller.cams:
            rows.append((
                cam.Label, "Video", cam.DeviceName or "Unknown device",
                f"{cam.Width}x{cam.Height} @ {cam.FPS}fps, {cam.PixelFormat}",
            ))
    for stream in controller.lsl_streams:
        srate = stream.nominal_srate()
        rate = f"{srate:g} Hz" if srate > 0 else "irregular rate"
        rows.append((
            stream.name(), stream.type() or "LSL", stream.hostname() or "",
            f"{rate}, {stream.channel_count()} ch",
        ))
    return rows


class _DivergenceRequest:
    """A pending "measured setting diverges from configured" decision,
    raised from a VideoRecorder's own worker thread and answered by a modal
    dialog shown on the GUI thread. `event` is set once the dialog has been
    answered (or the app decides not to show one), unblocking the worker
    thread waiting in MainWindow.request_setting_divergence_decision."""

    __slots__ = ("title", "message", "event", "accepted")

    def __init__(self, title: str, message: str):
        self.title = title
        self.message = message
        self.event = threading.Event()
        self.accepted = False


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
                device_name = item.get("device_name")
                controls = item.get("controls") or {}
                if not controls:
                    continue
                rep = apply_camera_controls(
                    devnode=devnode,
                    controls=controls,
                    device_name=device_name
                )
                failed = rep.get("failed", {})
                if failed:
                    failed_items = ", ".join(f"{k}={v}" for k, v in failed.items())
                    failed_msgs.append(f"Camera {label}: failed to apply {failed_items}")
            self.finished_apply.emit(failed_msgs)
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    log_signal = pyqtSignal(str)
    preview_frame_signal = pyqtSignal(object, object)
    divergence_signal = pyqtSignal(object)
    abort_active_run_signal = pyqtSignal(str)

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
        self.divergence_signal.connect(self._on_divergence_request)
        self.abort_active_run_signal.connect(self._abort_active_run)
        self._run_log_file = None
        self._log_lock = threading.Lock()
        self._log_path = None
        # Persistent app-level session log:
        # opened for the lifetime of the app, independent of any specific recording run,
        # so activity before a run ever starts (config loads, setting changes,
        # capability-probe failures, or native crash) still leaves a trace on disk.
        # The per-run `run.log` (opened in _open_run_log) mirrors the same lines
        # for the duration of that run only, alongside its own output.
        self._app_session_log_file = None
        self._app_session_log_path = None
        self._open_app_session_log()
        env = get_environment_info(_project_root())
        self.log(
            f"App started (PID {os.getpid()}): commit={env['commit']} "
            f"platform={env['platform']} hostname={env['hostname']} "
            f"python={env['python_version']}"
        )
        self.cfg: AppConfig = load_cfg(cfg_path) if cfg_path else load_cfg("example.cfg")
        self.log(build_config_log_payload("config_loaded_at_startup", self.cfg))
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
        self.btn_save = QPushButton("Save config")
        self.btn_add_camera = QPushButton("Add camera")
        self.test_duration_spin = QSpinBox()
        self.test_duration_spin.setRange(5, 60)
        self.test_duration_spin.setValue(15)
        self.test_duration_spin.setSuffix(" s")
        self.btn_test_recording = QPushButton("Test recording settings")
        self.btn_start = QPushButton("Start")
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setEnabled(False)
        self.btn_close_app = QPushButton("Close app")
        btn_row.addWidget(self.btn_load)
        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_add_camera)
        btn_row.addStretch(1)
        btn_row.addWidget(QLabel("Test duration:"))
        btn_row.addWidget(self.test_duration_spin)
        btn_row.addWidget(self.btn_test_recording)
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_stop)
        btn_row.addWidget(self.btn_close_app)

        self.tabs = QTabWidget()
        self.btn_remove_camera = QPushButton("Remove camera")
        self.labrec_rcs: Optional[LabRecorderRCS] = None

        # Audio tab
        audio_widget = QWidget()
        af = QFormLayout()
        self.audio_enabled = QCheckBox("Enable audio")
        self.audio_enabled.setChecked(bool(self.cfg.Audio.Enabled))
        self.audio_device = QComboBox()
        self.audio_sr = QComboBox()
        self.audio_bit = QComboBox()
        self.audio_ch = QSpinBox(); self.audio_ch.setRange(1, 16); self.audio_ch.setValue(self.cfg.Audio.Channels)
        self._audio_caps: dict = {}
        self._populate_audio_devices()
        self._refresh_audio_capabilities()
        self.audio_device.currentIndexChanged.connect(self._on_audio_device_changed)
        self.audio_enabled.toggled.connect(lambda checked: self._log_gui_change("Audio.Enabled", checked))
        self.audio_sr.currentIndexChanged.connect(
            lambda _i: self._log_gui_change("Audio.SampleRate", self.audio_sr.currentData())
        )
        self.audio_bit.currentIndexChanged.connect(
            lambda _i: self._log_gui_change("Audio.BitDepth", self.audio_bit.currentData())
        )
        self.audio_ch.valueChanged.connect(lambda v: self._log_gui_change("Audio.Channels", v))

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
        self.writer_drop_policy.addItem("Drop oldest (recommended)", FULL_BUFFER_DROP_OLDEST_POLICY)
        self.writer_drop_policy.addItem("Drop newest (incoming)", FULL_BUFFER_DROP_NEWEST_POLICY)
        self.writer_drop_policy.addItem("Block capture thread", FULL_BUFFER_BLOCK_THREAD_POLICY)
        policy = getattr(self.cfg.Buffering, "WriterDropPolicy", FULL_BUFFER_DEFAULT_POLICY)
        idx = self.writer_drop_policy.findData(policy)
        if idx >= 0:
            self.writer_drop_policy.setCurrentIndex(idx)
        bf.addRow("Audio buffer seconds", self.audio_buffer_seconds)
        bf.addRow("Video buffer frames", self.video_buffer_frames)
        bf.addRow("Writer queue size", self.writer_queue_size)
        bf.addRow("When full", self.writer_drop_policy)
        buffering_widget.setLayout(bf)
        self.tabs.addTab(buffering_widget, "Buffering")
        self.audio_buffer_seconds.valueChanged.connect(
            lambda v: self._log_gui_change("Buffering.AudioBufferSeconds", v)
        )
        self.video_buffer_frames.valueChanged.connect(
            lambda v: self._log_gui_change("Buffering.VideoBufferFrames", v)
        )
        self.writer_queue_size.valueChanged.connect(
            lambda v: self._log_gui_change("Buffering.WriterQueueSize", v)
        )
        self.writer_drop_policy.currentIndexChanged.connect(
            lambda _i: self._log_gui_change("Buffering.WriterDropPolicy", self.writer_drop_policy.currentData())
        )

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
        self.labrec_enabled.toggled.connect(lambda checked: self._log_gui_change("LabRecorder.Enabled", checked))
        self.labrec_port.valueChanged.connect(lambda v: self._log_gui_change("LabRecorder.Port", v))
        self.lsl_streams_table.itemChanged.connect(self._on_lsl_stream_item_changed)

        # Recording status indicator: hidden until a real recording starts,
        # shown above the preview wall for the run's duration
        self.recording_status_label = QLabel()
        self.recording_status_label.setStyleSheet(
            "QLabel { background-color: #b00020; color: white; "
            "font-weight: bold; padding: 6px; border-radius: 4px; }"
        )
        self.recording_status_label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self.recording_status_label.setVisible(False)

        self.recording_streams_table = QTableWidget(0, 4)
        self.recording_streams_table.setHorizontalHeaderLabels(["Stream", "Type", "Device", "Details"])
        self.recording_streams_table.verticalHeader().setVisible(False)
        self.recording_streams_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.recording_streams_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.recording_streams_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.recording_streams_table.horizontalHeader().setStretchLastSection(True)
        self.recording_streams_table.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self.recording_streams_table.setVisible(False)

        self._recording_start_ts: Optional[float] = None
        self._recording_start_clock: str = ""
        self._recording_status_timer = QTimer(self)
        self._recording_status_timer.setInterval(1000)
        self._recording_status_timer.timeout.connect(self._update_recording_status_label)

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
        self._testing_active = False
        self._test_controller: Optional[RunController] = None
        self._test_cfg: Optional[AppConfig] = None
        self._test_lsl_streams: List[StreamInfo] = []
        self._test_duration_s: float = 0.0
        self._test_xdf_path: Optional[str] = None
        self._test_progress_dialog: Optional[QProgressDialog] = None
        self._test_countdown_timer: Optional[QTimer] = None
        self._test_remaining_s: int = 0
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

        preview_container = QWidget()
        preview_container_layout = QVBoxLayout()
        preview_container_layout.setContentsMargins(0, 0, 0, 0)
        preview_container_layout.addWidget(self.recording_status_label, 0)
        preview_container_layout.addWidget(self.recording_streams_table, 0)
        preview_container_layout.addWidget(self.preview_panel, 1)
        preview_container.setLayout(preview_container_layout)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(preview_container)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)

        root = QWidget()
        root_layout = QVBoxLayout()
        root_layout.addWidget(splitter)
        root.setLayout(root_layout)
        self.setCentralWidget(root)

        self.btn_load.clicked.connect(self.on_load)
        self.btn_save.clicked.connect(self.on_save)
        self.btn_add_camera.clicked.connect(self._on_add_camera)
        self.btn_start.clicked.connect(self.on_start)
        self.btn_stop.clicked.connect(self.on_stop)
        self.btn_close_app.clicked.connect(self.on_close_app)
        self.btn_test_recording.clicked.connect(self.on_test_recording)
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
        with self._log_lock:
            if self._app_session_log_file:
                try:
                    self._app_session_log_file.write(line + "\n")
                    self._app_session_log_file.flush()
                except Exception:
                    pass
            if self._run_log_file:
                try:
                    self._run_log_file.write(line + "\n")
                    self._run_log_file.flush()
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

    def _log_gui_change(self, field: str, value):
        """Log a GUI setting change."""
        self.log(f"Setting changed: {field} = {value!r}")

    def _open_app_session_log(self):
        """Open the app-level session log file for the lifetime of this
        process, at a fixed location independent of any run."""
        try:
            logs_dir = _logs_path()
            logs_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            self._app_session_log_path = str(logs_dir / f"session_{timestamp}.log")
            self._app_session_log_file = open(self._app_session_log_path, "a", encoding="utf-8")
            faulthandler.enable(file=self._app_session_log_file, all_threads=True)
        except Exception:
            self._app_session_log_file = None
            self._app_session_log_path = None

    def _close_app_session_log(self):
        if self._app_session_log_file:
            try:
                self._app_session_log_file.close()
            except Exception:
                pass
        self._app_session_log_file = None

    def _copy_app_log_to(self, target_dir: str):
        """Copy the app-level session log's current contents (everything
        logged since app launch, including any pre-Start activity) into
        `target_dir` (output directory where run.log is written)
        as session.log, without disturbing the live file, which keeps being
        written at its original _logs_path() location (see _open_app_log).

        Called twice per run: right after Start (so even a crash mid-run
        still leaves that run's folder with everything up to when it began)
        and again at Stop (so a clean run ends up with the fully up-to-date log)."""
        if not self._app_session_log_file or not self._app_session_log_path:
            return
        try:
            self._app_session_log_file.flush()
        except Exception:
            pass
        try:
            os.makedirs(target_dir, exist_ok=True)
            shutil.copyfile(self._app_session_log_path, os.path.join(target_dir, "session.log"))
        except Exception as exc:
            self.log(f"Could not copy session log to {target_dir}: {exc}", loglevel="WARNING")

    def closeEvent(self, event):
        # Logged unconditionally, on every close path (the "Close app" button,
        # the window's native close control, or any other call to close()).
        # Distinguishes a graceful shutdown from a crash: 
        # if the log simply stops with no matching line here, that means
        # the app went down some other way (native crash, force-kill).
        self.log("Closing app.")
        if self._recording_active and self.controller:
            self.log(
                "App closing while a recording was still active; stopping it first.",
                loglevel="WARNING",
            )
            try:
                self.controller.stop()
                self._copy_app_log_to(self.controller.outdir)
            except Exception as exc:
                self.log(f"Error stopping recording during app close: {exc}", loglevel="ERROR")
        self._close_run_log()
        self._close_app_session_log()
        super().closeEvent(event)

    def on_close_app(self):
        if self._recording_active:
            confirm = QMessageBox.question(
                self,
                "Close app",
                "A recording is currently active. Stop it and close the app?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
        self.log("Close app button clicked by user.")
        self.close()

    def _open_run_log(self):
        self._close_run_log()
        if not self.controller:
            return
        try:
            self._log_path = os.path.join(self.controller.outdir, "run.log")
            self._run_log_file = open(self._log_path, "a", encoding="utf-8")
        except Exception as exc:
            self._run_log_file = None
            self._log_path = None
            self.log(f"Could not open run log: {exc}", loglevel="WARNING")

    def _close_run_log(self):
        if self._run_log_file:
            try:
                self._run_log_file.close()
            except Exception:
                pass
        self._run_log_file = None
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
        self._log_gui_change("Audio.Device", self.audio_device.currentText())
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
            if i < len(self.cfg.Video.Cams):
                self._add_camera_panel(self.cfg.Video.Cams[i])
            else:
                # No config entry for this tab:
                # (e.g. auto-detected extra camera with nothing saved for it yet)
                # treat exactly like "Add camera": no pre-selected device
                self._add_camera_panel()

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
        self._update_camera_settings_controls()

    def _update_remove_buttons(self):
        enabled = (len(self.cam_panels) > 1) and not self._recording_active
        for panel in self.cam_panels:
            panel.set_remove_enabled(enabled)

    def _update_camera_settings_controls(self):
        # Apply settings / Refresh device capabilities both probe or
        # reconfigure a camera's device, which must not run while a
        # recording is active.
        for panel in self.cam_panels:
            panel.set_settings_controls_enabled(not self._recording_active)

    def _add_camera_panel(self, cam_cfg: VideoCamConfig | None = None) -> Optional[CameraPanel]:
        if len(self.cam_panels) >= self.max_cams:
            return None
        # cam_cfg is None exactly when there's no real config entry for this tab;
        # such a panel gets no pre-selected device:
        # the combo starts on "Select a camera...", and device-dependent controls stay
        # grayed out until the user actually picks one
        is_new = cam_cfg is None
        if is_new:
            cam_cfg = VideoCamConfig()
            # Pre-enable so preview starts as soon as a device is chosen,
            # without an extra click; to_config() reports Enabled=False
            # anyway until a real device is actually selected
            cam_cfg.Enabled = True
        # For a brand-new panel, VideoCamConfig()'s own dataclass default
        # ("Cam", not blank) must not be mistaken for a real label
        existing_label = "" if is_new else cam_cfg.Label
        cam_cfg.Label = _default_camera_label(existing_label, len(self.cam_panels) + 1)
        panel = CameraPanel(cam_cfg, preselect_device=not is_new)
        panel.applyStarted.connect(lambda p=panel: self._stop_preview_for_cam(p.to_config()))
        panel.applyStarted.connect(self._on_apply_started)
        panel.applyFinished.connect(self._on_apply_finished)
        panel.applyFinished.connect(lambda p=panel: self._start_preview_for_cam(p.to_config()))
        panel.previewConfigChanged.connect(self._refresh_previews_from_panels)
        panel.capabilitiesLoadStarted.connect(lambda p=panel: self._stop_preview_for_cam(p.to_config()))
        panel.capabilitiesLoadStarted.connect(self._on_caps_load_started)
        panel.capabilitiesLoadFinished.connect(self._on_caps_load_finished)
        panel.capabilitiesLoadFinished.connect(lambda p=panel: self._start_preview_for_cam(p.to_config()))
        panel.capabilitiesLoadProgress.connect(self._on_caps_load_progress)
        panel.removeRequested.connect(self._on_remove_camera)
        panel.log.connect(self.log)
        self.cam_panels.append(panel)
        self.tabs.addTab(panel, f"Camera {len(self.cam_panels)}")
        self._update_add_camera_button()
        # Nothing to probe for a panel with no device selected yet
        if not is_new:
            panel.refresh_capabilities()
        return panel

    def _on_add_camera(self):
        panel = self._add_camera_panel()
        if panel is not None:
            self.tabs.setCurrentWidget(panel)
            self.log(f"Added camera tab: {panel.label.text().strip() or panel._default_label}")
        self._refresh_previews_from_panels()

    def _on_remove_camera(self, panel: CameraPanel):
        if self._recording_active or len(self.cam_panels) <= 1:
            return
        if panel not in self.cam_panels:
            return

        # Logged before the panel is actually torn down, so the log still
        # shows which camera/device was removed even if something in the
        # teardown itself goes wrong.
        self.log(
            f"Removing camera tab: {panel.label.text().strip() or panel._default_label} "
            f"(device={panel._device_name or '?'}, index={panel.device_index.value()})"
        )
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

        # A removed camera's device slot can free up (or otherwise shift)
        # device numbering (seen on MacOS), which each panel's device list
        # is only a snapshot of from whenever it was last populated/refreshed:
        # Refresh video devices on camera removal to avoid this issue
        if self.cam_panels:
            self.log(f"Refreshing video devices for {len(self.cam_panels)} remaining camera(s) after removal")
            for cam_panel in self.cam_panels:
                cam_panel.refresh_video_devices(quiet=True)

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
            self.log(build_config_log_payload("config_loaded", self.cfg))
        except Exception as e:
            QMessageBox.critical(self, "Load failed", str(e))

    def on_save(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save config", ".", "CFG files (*.cfg);;All files (*)")
        if not path:
            return
        if not path.lower().endswith(".cfg"):
            path += ".cfg"
        try:
            self.pull_gui_into_cfg()
            save_cfg(self.cfg, path)
            self.log(f"Saved config: {path}")
            self.log(build_config_log_payload("config_saved", self.cfg))
        except Exception as e:
            QMessageBox.critical(self, "Save failed", str(e))

    def on_start(self):
        if self._testing_active:
            return
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
        warnings.extend(self._validate_unique_camera_labels())
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
                    "device_name": cam_cfg.DeviceName,
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

    def _validate_unique_camera_labels(self) -> list[str]:
        configs = [panel.to_config() for panel in self.cam_panels]
        enabled_labels = [c.Label for c in configs if c.Enabled]
        return [
            f"Camera label '{label}' is used by more than one camera; labels must be unique."
            for label in _find_duplicate_camera_labels(enabled_labels)
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
                divergence_cb=self.request_setting_divergence_decision,
            )
            self.preview_mgr.set_recording_preview_labels(
                {preview_key(cam): cam.Label for cam in self.controller.cams}
            )
            self._open_run_log()
            self._copy_app_log_to(self.controller.outdir)
            self.log(build_config_log_payload("recording_started", self.cfg))
            self.controller.start()
            self.btn_start.setEnabled(False)
            self.btn_stop.setEnabled(True)
            self._recording_active = True
            self._update_add_camera_button()
            self._show_recording_status()
        except Exception as e:
            QMessageBox.critical(self, "Start failed", str(e))
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)
            self._refresh_previews_from_panels()
        finally:
            self._hide_start_progress()

    def _stop_preview_for_cam(self, cam_cfg: VideoCamConfig) -> bool:
        key = preview_key(cam_cfg)
        if key not in self.preview_mgr.workers:
            return False
        self.log(f"Stopping preview for camera {cam_cfg.Label} ({key})")
        return self.preview_mgr.stop_cam_preview(key)

    def _start_preview_for_cam(self, cam_cfg: VideoCamConfig):
        if not self.btn_start.isEnabled():
            return
        if getattr(cam_cfg, "Enabled", False):
            self.preview_mgr.start_cam_preview(cam_cfg)

    def request_setting_divergence_decision(self, title: str, message: str) -> bool:
        """Blocks the calling thread until the user answers a modal dialog
        shown on the GUI thread.
        Returns True (accept: keep recording) or False (abort run).

        Thread-safe: may be called from any thread, in particular a
        VideoRecorder's own worker thread when a measured setting (fps,
        frame size) diverges from what was configured.
        """
        req = _DivergenceRequest(title, message)
        self.divergence_signal.emit(req)
        req.event.wait()
        if not req.accepted:
            self.abort_active_run_signal.emit(f"{title}\n\n{message}")
        return req.accepted

    def _on_divergence_request(self, req: _DivergenceRequest):
        try:
            box = QMessageBox(self)
            box.setWindowTitle(req.title)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setText(req.message)
            accept_btn = box.addButton("Accept and continue", QMessageBox.ButtonRole.AcceptRole)
            abort_btn = box.addButton("Abort recording", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(abort_btn)
            box.exec()
            # Closing the dialog any other way (e.g. the window's own close button)
            # leaves clickedButton() as None; treat as an implicit abort
            # rather than silently continuing on an unacknowledged mismatch
            req.accepted = box.clickedButton() is accept_btn
        finally:
            req.event.set()

    def _abort_active_run(self, reason: str):
        # Only real recordings are wired to request_setting_divergence_decision
        # (see on_test_recording: test recordings deliberately don't get a
        # divergence_cb, since a test already validates these settings at
        # the end with a detailed report).
        if self._recording_active:
            self._abort_real_recording(reason)

    def _teardown_recording(self) -> str:
        """Stop the active controller/previews and copy the run log to the
        output directory. Returns that directory. Shared by the
        user-initiated Stop button and an automatic abort triggered by a
        rejected settings-divergence prompt."""
        self.preview_mgr.stop_all_previews()
        self.controller.stop()
        outdir = os.path.abspath(self.controller.outdir)
        self._copy_app_log_to(outdir)
        return outdir

    def _reset_recording_ui_state(self):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._recording_active = False
        self._update_add_camera_button()
        if self.cfg.Video.Enabled:
            self._refresh_previews_from_panels()
        self._close_run_log()
        self._hide_recording_status()

    def _update_recording_status_label(self):
        if self._recording_start_ts is None:
            return
        elapsed = int(time.monotonic() - self._recording_start_ts)
        hours, rem = divmod(elapsed, 3600)
        minutes, seconds = divmod(rem, 60)
        duration = f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"
        self.recording_status_label.setText(
            f"RECORDING: {duration} elapsed (started {self._recording_start_clock})"
        )

    def _populate_recording_streams_table(self):
        rows = _recording_stream_rows(self.controller)
        table = self.recording_streams_table
        table.setRowCount(len(rows))
        for i, (name, stype, device, details) in enumerate(rows):
            table.setItem(i, 0, QTableWidgetItem(name))
            table.setItem(i, 1, QTableWidgetItem(stype))
            table.setItem(i, 2, QTableWidgetItem(device))
            table.setItem(i, 3, QTableWidgetItem(details))
        table.resizeColumnsToContents()
        table.resizeRowsToContents()
        content_height = table.horizontalHeader().height() + sum(
            table.rowHeight(i) for i in range(table.rowCount())
        )
        table.setFixedHeight(content_height + 2 * table.frameWidth() + 2)

    def _show_recording_status(self):
        self._recording_start_ts = time.monotonic()
        self._recording_start_clock = datetime.now().strftime("%H:%M:%S")
        self._update_recording_status_label()
        self._populate_recording_streams_table()
        self.recording_status_label.setVisible(True)
        self.recording_streams_table.setVisible(True)
        self._recording_status_timer.start()

    def _hide_recording_status(self):
        self._recording_status_timer.stop()
        self.recording_status_label.setVisible(False)
        self.recording_streams_table.setVisible(False)
        self._recording_start_ts = None

    def on_stop(self):
        confirm = QMessageBox.question(
            self,
            "Stop recording",
            "Stop active recording?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        try:
            outdir = self._teardown_recording()
        finally:
            self._reset_recording_ui_state()

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

    def _abort_real_recording(self, reason: str):
        try:
            outdir = self._teardown_recording()
            QMessageBox.warning(
                self,
                "Recording aborted",
                f"{reason}\n\nRecording stopped automatically. Partial results "
                f"written to:\n{outdir}",
            )
        finally:
            self._reset_recording_ui_state()

    def on_test_recording(self):
        """Runs a short, real recording with the currently configured streams
        (audio/video/selected LSL streams) into a temp directory, then loads
        the resulting XDF file with pyxdf and checks it against the config,
        so misconfigurations or recording issues surface immediately instead of
        at the start of a real session or after recording."""
        if self._recording_active or self._testing_active:
            return
        self.pull_gui_into_cfg()

        warnings = self._validate_audio_settings()
        warnings.extend(self._validate_unique_camera_labels())
        if warnings:
            body = "Some settings may not be supported:\n\n"
            body += "\n".join(f"- {msg}" for msg in warnings)
            body += "\n\nPlease adjust settings before testing."
            QMessageBox.warning(self, "Settings warning", body)
            return

        lsl_streams = self._get_selected_lsl_streams()
        if not self.cfg.Audio.Enabled and not self.cfg.Video.Enabled and not lsl_streams:
            QMessageBox.information(
                self,
                "Nothing to test",
                "Enable audio/video or select at least one LSL stream (Discover streams) to test.",
            )
            return

        duration_s = int(self.test_duration_spin.value())
        test_cfg = copy.deepcopy(self.cfg)
        test_cfg.Output.StudyRoot = tempfile.mkdtemp(prefix="lsl_av_recorder_test_")

        self.log(build_config_log_payload("test_recording_started", self.cfg))
        self.preview_mgr.stop_all_previews()
        self.btn_start.setEnabled(False)
        self.btn_test_recording.setEnabled(False)
        self._testing_active = True

        try:
            controller = RunController(
                test_cfg,
                status_cb=lambda msg, loglevel: self.log(f"[Test] {msg}", loglevel),
                lsl_streams=lsl_streams,
                # NB: no divergence_cb needed for test recordings
            )
            controller.start()
        except Exception as exc:
            QMessageBox.critical(self, "Test recording failed to start", str(exc))
            self._abort_test_recording(test_cfg)
            return

        self._test_controller = controller
        self._test_cfg = test_cfg
        self._test_lsl_streams = lsl_streams
        self._test_duration_s = float(duration_s)
        self._test_xdf_path = controller.xdf.path if controller.xdf else None
        self.log(f"[Test] Recording for {duration_s}s into {test_cfg.Output.StudyRoot} ...")
        self._show_test_progress(duration_s)
        QTimer.singleShot(duration_s * 1000, self._finish_test_recording)

    def _abort_test_recording(self, test_cfg: AppConfig):
        self._testing_active = False
        self.btn_start.setEnabled(True)
        self.btn_test_recording.setEnabled(True)
        if self.cfg.Video.Enabled:
            self._refresh_previews_from_panels()
        shutil.rmtree(test_cfg.Output.StudyRoot, ignore_errors=True)

    def _show_test_progress(self, duration_s: int):
        self._test_remaining_s = duration_s
        if self._test_progress_dialog is None:
            self._test_progress_dialog = self._ensure_progress_dialog("Testing recording settings", "")
        self._update_test_progress_label()
        self._test_progress_dialog.show()
        QApplication.processEvents()
        if self._test_countdown_timer is None:
            self._test_countdown_timer = QTimer(self)
            self._test_countdown_timer.timeout.connect(self._tick_test_progress)
        self._test_countdown_timer.start(1000)

    def _tick_test_progress(self):
        self._test_remaining_s = max(0, self._test_remaining_s - 1)
        self._update_test_progress_label()

    def _update_test_progress_label(self):
        if self._test_progress_dialog:
            self._test_progress_dialog.setLabelText(
                f"Recording test streams... {self._test_remaining_s}s remaining"
            )

    def _hide_test_progress(self):
        if self._test_countdown_timer:
            self._test_countdown_timer.stop()
        if self._test_progress_dialog:
            self._test_progress_dialog.hide()

    def _finish_test_recording(self):
        self._hide_test_progress()
        controller = self._test_controller
        test_cfg = self._test_cfg
        lsl_streams = self._test_lsl_streams
        duration_s = self._test_duration_s
        xdf_path = self._test_xdf_path

        try:
            controller.stop()
        except Exception as exc:
            self.log(f"[Test] Error stopping test recording: {exc}", loglevel="ERROR")

        self._testing_active = False
        self.btn_start.setEnabled(True)
        self.btn_test_recording.setEnabled(True)
        if self.cfg.Video.Enabled:
            self._refresh_previews_from_panels()

        self._test_controller = None
        self._test_cfg = None
        self._test_lsl_streams = []
        self._test_xdf_path = None

        if not xdf_path:
            QMessageBox.critical(self, "Test recording failed", "No XDF file was produced.")
            shutil.rmtree(test_cfg.Output.StudyRoot, ignore_errors=True)
            return

        report = validate_test_recording(xdf_path, test_cfg, lsl_streams, expected_duration_s=duration_s)
        self._show_validation_report(report, test_cfg.Output.StudyRoot)

    def _show_validation_report(self, report: ValidationReport, temp_dir: str):
        for check in report.checks:
            loglevel = "INFO" if check.passed else "WARNING"
            mark = "PASS" if check.passed else "FAIL"
            self.log(f"[Test] [{mark}] {check.name}: {check.detail}", loglevel)

        box = QMessageBox(self)
        box.setWindowTitle("Test recording results")
        if report.passed:
            box.setIcon(QMessageBox.Icon.Information)
            box.setText(f"All checks passed ({report.summary()}).")
            shutil.rmtree(temp_dir, ignore_errors=True)
            self.log(f"[Test] Cleaned up temp test recording directory: {temp_dir}")
            ok_btn = box.addButton(QMessageBox.StandardButton.Ok)
            box.setDefaultButton(ok_btn)
        else:
            self.log(
                f"[Test] Validation failed; test recording artifacts: {temp_dir}",
                loglevel="WARNING",
            )
            box.setIcon(QMessageBox.Icon.Warning)
            box.setText(f"{report.summary()}.\n\nTest recording directory:\n{temp_dir}")
            delete_btn = box.addButton("Delete test files", QMessageBox.ButtonRole.AcceptRole)
            keep_btn = box.addButton("Keep for inspection", QMessageBox.ButtonRole.ActionRole)
            box.setDefaultButton(delete_btn)
        box.setDetailedText(report.detailed_text())
        box.exec()

        if not report.passed:
            # Clean up temp test files automatically unless the user explicitly indicated that the files should be kept
            if box.clickedButton() is keep_btn:
                self.log(f"[Test] Keeping temp directory for inspection: {temp_dir}", loglevel="WARNING")
            else:
                shutil.rmtree(temp_dir, ignore_errors=True)
                self.log(f"[Test] Deleted test recording directory: {temp_dir}")

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
        # Population below fires itemChanged per cell
        # block signals so _on_lsl_stream_item_changed only reacts
        # to genuine user clicks, not this programmatic (re)population
        self.lsl_streams_table.blockSignals(True)
        try:
            self.lsl_streams_table.setRowCount(0)
            try:
                from pylsl import resolve_streams
                streams = _exclude_camera_preview_streams(resolve_streams(wait_time=2.0))
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
        finally:
            self.lsl_streams_table.blockSignals(False)
        self.log(f"Discovered {self.lsl_streams_table.rowCount()} LSL stream(s)")

    def _on_lsl_stream_item_changed(self, item: QTableWidgetItem):
        if item.column() != 0:
            return
        stream = item.data(Qt.ItemDataRole.UserRole)
        name = stream.name() if stream is not None else "?"
        checked = item.checkState() == Qt.CheckState.Checked
        self._log_gui_change(f"LSL stream selected for recording <{name}>", checked)

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
        policy = getattr(self.cfg.Buffering, "WriterDropPolicy", FULL_BUFFER_DEFAULT_POLICY)
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
            if i < len(self.cfg.Video.Cams):
                self._add_camera_panel(self.cfg.Video.Cams[i])
            else:
                self._add_camera_panel()
