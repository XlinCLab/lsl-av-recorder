from __future__ import annotations

import time
from collections import deque
from typing import Any, Dict, List, Optional

import numpy as np
from pylsl import StreamInfo, StreamInlet, resolve_streams
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
                             QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                             QLabel, QMessageBox, QProgressBar, QPushButton,
                             QSpinBox, QVBoxLayout, QWidget)

from ..lsl.constants import COUNTER_PATTERNS as DEFAULT_COUNTER_PATTERNS
from ..lsl.constants import (DEFAULT_A50_FACTOR, DEFAULT_A50_WARN,
                             DEFAULT_BASELINE_DURATION_S, DEFAULT_CMI_BAD,
                             DEFAULT_CMI_WARN, DEFAULT_HOP_SEC,
                             DEFAULT_LINE_FREQ, DEFAULT_SAT_LEVEL,
                             DEFAULT_SMOOTH_WINDOWS, DEFAULT_WINDOW_SEC)
from ..lsl.constants import NONEEG_PATTERNS as DEFAULT_NONEEG_PATTERNS
from ..lsl.constants import STATUS_COLORS
from ..lsl.lsl_inlet_recorder import extract_channel_info
from ..lsl.refgnd_monitor import (analyze_window, detect_counter_index,
                                  detect_eeg_indices, load_baseline,
                                  save_baseline, smoothed,
                                  thresholds_for_baseline, verdict)
from .widgets import make_checkbox_grid, with_help_icon

REF_GND_MONITOR_EXT_DESCR = (
    "NOTE: This does NOT measure impedance in ohms as that would require injecting "
    "a known current and reading the voltage response, which only the amplifier's "
    "own firmware can do. Instead it measures a consequence of a bad Ref/Ground contact: "
    "the front-end's ability to reject common-mode interference (mains hum)."
)

MAINS_FREQ_DESCR = """Mains frequency:
- 50 Hz (most of the world)
- 60 Hz (North America)
(!) Must match the actual mains frequency in the room where EEG is being measured."""

SAT_LEVEL_DESCR = """Absolute value of sample at or above this value counts as front-end saturation (RAIL).
(!) This is amplifier-specific. Check your device's data range."""

A50_WARN_DESCR = "Mains-hum amplitude threshold used when no baseline has been recorded for this device."

A50_FACTOR_DESCR = "Once a baseline is loaded, the A50 threshold is this many times the baseline's measured 95th-percentile hum."

CMI_WARN_DESCR = "Common-Mode Index above which the status turns yellow (with elevated hum)."

CMI_BAD_DESCR = "Common-Mode Index above which the status turns red (with elevated hum)."

RECORD_BASELINE_DESCR = "Record this many seconds against a trusted setup (e.g. fresh gel on Ref/GND, no known issues) to set device-specific thresholds."


class _RefGndMonitorThread(QThread):
    """Pulls continuously from an already-open StreamInlet and analyzes one
    window every `hop_sec`. In live mode (`baseline_seconds=None`) it emits
    `window_ready` for as long as it runs; in baseline mode, it instead
    accumulates each window's raw A50/CMI and emits `baseline_ready` once
    `baseline_seconds` have elapsed, then stops itself."""

    window_ready = pyqtSignal(dict)
    baseline_progress = pyqtSignal(float, float)  # elapsed, total
    baseline_ready = pyqtSignal(list, list)  # a50 values, cmi values
    failed = pyqtSignal(str)

    def __init__(
        self,
        inlet: StreamInlet,
        fs: float,
        eeg_idx: List[int],
        counter_idx: Optional[int],
        line_freq: float,
        sat_level: float,
        window_sec: float = DEFAULT_WINDOW_SEC,
        hop_sec: float = DEFAULT_HOP_SEC,
        baseline_seconds: Optional[float] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._inlet = inlet
        self._fs = fs
        self._eeg_idx = eeg_idx
        self._counter_idx = counter_idx
        self._line_freq = line_freq
        self._sat_level = sat_level
        self._window_n = max(1, int(round(window_sec * fs)))
        self._hop_n = max(1, int(round(hop_sec * fs)))
        self._baseline_seconds = baseline_seconds
        self._running = False

    def request_stop(self):
        self._running = False

    def run(self):
        self._running = True
        buf = deque(maxlen=self._window_n)
        since_hop = 0
        a50_values: List[float] = []
        cmi_values: List[float] = []
        start = time.monotonic()
        try:
            while self._running:
                chunk, _ = self._inlet.pull_chunk(timeout=0.5, max_samples=self._hop_n * 4)
                if not chunk:
                    if self._baseline_seconds is not None:
                        elapsed = time.monotonic() - start
                        self.baseline_progress.emit(elapsed, self._baseline_seconds)
                        if elapsed >= self._baseline_seconds:
                            break
                    continue

                for sample in chunk:
                    buf.append(sample)
                since_hop += len(chunk)
                if len(buf) < self._window_n or since_hop < self._hop_n:
                    continue
                since_hop = 0

                A = np.asarray(buf, dtype=np.float64).T
                counter = A[self._counter_idx] if self._counter_idx is not None else None
                m = analyze_window(
                    A[self._eeg_idx], self._fs, counter, None,
                    f0=self._line_freq, sat=self._sat_level,
                )

                if self._baseline_seconds is not None:
                    a50_values.append(m["a50_med"])
                    cmi_values.append(m["cmi"])
                    elapsed = time.monotonic() - start
                    self.baseline_progress.emit(elapsed, self._baseline_seconds)
                    if elapsed >= self._baseline_seconds:
                        break
                else:
                    self.window_ready.emit(m)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        finally:
            self._running = False

        if self._baseline_seconds is not None:
            self.baseline_ready.emit(a50_values, cmi_values)


class RefGndMonitorDialog(QDialog):
    """Live Ref/GND (reference/ground) contact-quality monitor for an EEG
    LSL stream. Does not measure impedance; rather, it measures how well the
    amplifier's front-end rejects mains hum as a common-mode signal, which
    degrades when Ref/GND contact is poor."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Ref/GND Monitor")
        self.setMinimumWidth(480)

        self._inlet: Optional[StreamInlet] = None
        self._stream_info: Optional[StreamInfo] = None
        self._device_name: str = ""
        self._labels: List[str] = []
        self._fs: float = 0.0
        self._monitor_thread: Optional[_RefGndMonitorThread] = None
        self._baseline_thread: Optional[_RefGndMonitorThread] = None
        self._hist: List[Dict[str, Any]] = []
        self._log_fh = None

        # --- Stream selection -------------------------------------------------
        stream_row = QHBoxLayout()
        self.stream_combo = QComboBox()
        self.btn_discover = QPushButton("Discover streams")
        self.btn_discover.clicked.connect(self._on_discover)
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.setEnabled(False)
        self.btn_connect.clicked.connect(self._on_connect)
        stream_row.addWidget(QLabel("Stream:"))
        stream_row.addWidget(self.stream_combo, 1)
        stream_row.addWidget(self.btn_discover)
        stream_row.addWidget(self.btn_connect)

        # --- Channel roles (populated after Connect) ---------------------------
        self.counter_combo = QComboBox()
        self.counter_combo.setToolTip(
            "Packet-counter channel, used to detect dropped packets. "
            "Auto-detected from the channel name; "
            "correct it here if the auto-detected selection is wrong, "
            "or select 'None' if there is none."
        )
        channel_role_form = QFormLayout()
        channel_role_form.addRow("Counter channel", self.counter_combo)

        self.eeg_channels_box = QGroupBox("EEG channels")
        self._eeg_checkboxes: Dict[int, QCheckBox] = {}
        eeg_box_layout = QVBoxLayout()
        eeg_select_row = QHBoxLayout()
        self.btn_eeg_select_all = QPushButton("Select all")
        self.btn_eeg_deselect_all = QPushButton("Deselect all")
        self.btn_eeg_select_all.clicked.connect(lambda: self._set_all_eeg_checked(True))
        self.btn_eeg_deselect_all.clicked.connect(lambda: self._set_all_eeg_checked(False))
        eeg_select_row.addWidget(self.btn_eeg_select_all)
        eeg_select_row.addWidget(self.btn_eeg_deselect_all)
        eeg_select_row.addStretch(1)
        eeg_box_layout.addLayout(eeg_select_row)
        self._eeg_grid_placeholder = QWidget()
        eeg_box_layout.addWidget(self._eeg_grid_placeholder)
        self.eeg_channels_box.setLayout(eeg_box_layout)

        # --- Settings -----------------------------------------------------------
        self.line_freq_spin = QDoubleSpinBox()
        self.line_freq_spin.setRange(40.0, 70.0)
        self.line_freq_spin.setValue(DEFAULT_LINE_FREQ)
        self.line_freq_spin.setSuffix(" Hz")
        self.line_freq_spin.setToolTip(MAINS_FREQ_DESCR)

        self.sat_level_spin = QDoubleSpinBox()
        self.sat_level_spin.setRange(1.0, 1e9)
        self.sat_level_spin.setDecimals(0)
        self.sat_level_spin.setSingleStep(1000.0)
        self.sat_level_spin.setValue(DEFAULT_SAT_LEVEL)
        self.sat_level_spin.setToolTip(SAT_LEVEL_DESCR)

        self.a50_warn_spin = QDoubleSpinBox()
        self.a50_warn_spin.setRange(0.0, 1e9)
        self.a50_warn_spin.setDecimals(2)
        self.a50_warn_spin.setValue(DEFAULT_A50_WARN)
        self.a50_warn_spin.setToolTip(A50_WARN_DESCR)

        self.a50_factor_spin = QDoubleSpinBox()
        self.a50_factor_spin.setRange(1.0, 20.0)
        self.a50_factor_spin.setDecimals(1)
        self.a50_factor_spin.setValue(DEFAULT_A50_FACTOR)
        self.a50_factor_spin.setToolTip(A50_FACTOR_DESCR)

        self.cmi_warn_spin = QDoubleSpinBox()
        self.cmi_warn_spin.setRange(0.0, 1.0)
        self.cmi_warn_spin.setDecimals(2)
        self.cmi_warn_spin.setSingleStep(0.05)
        self.cmi_warn_spin.setValue(DEFAULT_CMI_WARN)
        self.cmi_warn_spin.setToolTip(CMI_WARN_DESCR)

        self.cmi_bad_spin = QDoubleSpinBox()
        self.cmi_bad_spin.setRange(0.0, 1.0)
        self.cmi_bad_spin.setDecimals(2)
        self.cmi_bad_spin.setSingleStep(0.05)
        self.cmi_bad_spin.setValue(DEFAULT_CMI_BAD)
        self.cmi_bad_spin.setToolTip(CMI_BAD_DESCR)

        settings_form_left = QFormLayout()
        settings_form_left.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        settings_form_left.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        settings_form_right = QFormLayout()
        settings_form_right.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        settings_form_right.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        settings_form_left.addRow("Line frequency", self._settings_field(
            self.line_freq_spin, "Line frequency", MAINS_FREQ_DESCR,
        ))
        settings_form_left.addRow("A50 warn (no baseline)", self._settings_field(
            self.a50_warn_spin, "A50 warn (no baseline)", A50_WARN_DESCR,
        ))
        settings_form_left.addRow("A50 factor (with baseline)", self._settings_field(
            self.a50_factor_spin, "A50 factor (with baseline)", A50_FACTOR_DESCR,
        ))
        settings_form_right.addRow("Saturation level", self._settings_field(
            self.sat_level_spin, "Saturation level", SAT_LEVEL_DESCR,
        ))
        settings_form_right.addRow("CMI warn", self._settings_field(
            self.cmi_warn_spin, "CMI warn", CMI_WARN_DESCR,
        ))
        settings_form_right.addRow("CMI bad", self._settings_field(
            self.cmi_bad_spin, "CMI bad", CMI_BAD_DESCR,
        ))

        settings_columns = QHBoxLayout()
        settings_columns.addLayout(settings_form_left)
        settings_columns.addLayout(settings_form_right)
        settings_columns.addStretch(1)

        # --- Baseline -------------------------------------------------------
        self.baseline_status_label = QLabel("No baseline recorded for this device yet.")
        self.baseline_status_label.setWordWrap(True)
        self.baseline_duration_spin = QSpinBox()
        self.baseline_duration_spin.setRange(10, 600)
        self.baseline_duration_spin.setValue(DEFAULT_BASELINE_DURATION_S)
        self.baseline_duration_spin.setSuffix(" s")
        self.btn_record_baseline = QPushButton("Record baseline")
        self.btn_record_baseline.setToolTip(RECORD_BASELINE_DESCR)
        self.btn_record_baseline.setEnabled(False)
        self.btn_record_baseline.clicked.connect(self._on_record_baseline)
        self.baseline_progress_bar = QProgressBar()
        self.baseline_progress_bar.setVisible(False)
        baseline_row = QHBoxLayout()
        baseline_row.addLayout(with_help_icon(
            owner=self,
            widget=self.btn_record_baseline,
            title="Record baseline",
            text=RECORD_BASELINE_DESCR,
        ))
        baseline_row.addWidget(self.baseline_duration_spin)
        baseline_row.addWidget(self.baseline_progress_bar, 1)

        # --- Live monitoring --------------------------------------------------
        self.btn_start = QPushButton("Start monitoring")
        self.btn_start.setEnabled(False)
        self.btn_start.clicked.connect(self._on_start_monitoring)
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._on_stop_monitoring)
        monitor_row = QHBoxLayout()
        monitor_row.addWidget(self.btn_start)
        monitor_row.addWidget(self.btn_stop)

        self.status_label = QLabel("Not connected")
        self.status_label.setStyleSheet("font-weight: bold; font-size: 16px; padding: 4px;")
        self.reason_label = QLabel("")
        self.reason_label.setWordWrap(True)
        self.metrics_label = QLabel("")
        self.metrics_label.setWordWrap(True)

        self.log_checkbox = QCheckBox("Log to CSV")
        self.log_checkbox.toggled.connect(self._on_log_toggled)
        self.log_path_label = QLabel("(no file selected)")
        self.log_path_label.setWordWrap(True)
        self.btn_choose_log = QPushButton("Choose file...")
        self.btn_choose_log.setEnabled(False)
        self.btn_choose_log.clicked.connect(self._on_choose_log_file)
        log_row = QHBoxLayout()
        log_row.addWidget(self.log_checkbox)
        log_row.addWidget(self.log_path_label, 1)
        log_row.addWidget(self.btn_choose_log)
        self._log_path: Optional[str] = None

        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.accept)

        layout = QVBoxLayout()
        layout.addLayout(stream_row)
        layout.addLayout(channel_role_form)
        layout.addWidget(self.eeg_channels_box)
        layout.addWidget(QLabel("Settings"))
        layout.addLayout(settings_columns)
        layout.addWidget(QLabel("Baseline"))
        layout.addWidget(self.baseline_status_label)
        layout.addLayout(baseline_row)
        layout.addLayout(monitor_row)
        layout.addWidget(self.status_label)
        layout.addWidget(self.reason_label)
        layout.addWidget(self.metrics_label)
        layout.addLayout(log_row)
        layout.addWidget(self.btn_close)
        self.setLayout(layout)

    def _settings_field(self, widget: QWidget, title: str, text: str) -> QHBoxLayout:
        row = with_help_icon(owner=self, widget=widget, title=title, text=text)
        row.addStretch(1)
        return row

    # -------------------------------------------------------------------
    # Stream discovery / connection
    # -------------------------------------------------------------------

    def _on_discover(self):
        self.stream_combo.clear()
        try:
            streams = [s for s in resolve_streams(wait_time=2.0) if s.type() != "VideoFrame"]
        except Exception as exc:
            QMessageBox.critical(self, "Discovery failed", str(exc))
            return
        if not streams:
            QMessageBox.information(self, "No streams found", "No LSL streams were found on the network.")
            return
        # EEG-type streams first
        streams.sort(key=lambda s: s.type() == "EEG", reverse=True)
        for s in streams:
            self.stream_combo.addItem(f"{s.name()} ({s.type()}, {s.channel_count()}ch, {s.nominal_srate():.0f}Hz)", s)
        self.btn_connect.setEnabled(True)

    def _on_connect(self):
        stream = self.stream_combo.currentData()
        if stream is None:
            return
        try:
            inlet = StreamInlet(stream, max_buflen=10, recover=False)
            full_info = inlet.info()
        except Exception as exc:
            QMessageBox.critical(self, "Connection failed", str(exc))
            return

        self._teardown_inlet()
        self._inlet = inlet
        self._stream_info = stream
        self._device_name = stream.name()
        self._fs = full_info.nominal_srate()
        channels = extract_channel_info(full_info)
        self._labels = [
            c.get("label", f"ch{i}") for i, c in enumerate(channels)] or [
            f"ch{i}" for i in range(stream.channel_count())
        ]

        counter_idx = detect_counter_index(self._labels, DEFAULT_COUNTER_PATTERNS)
        self.counter_combo.clear()
        self.counter_combo.addItem("None", None)
        for i, label in enumerate(self._labels):
            self.counter_combo.addItem(f"[{i}] {label}", i)
        self.counter_combo.setCurrentIndex((counter_idx + 1) if counter_idx is not None else 0)

        eeg_idx = set(detect_eeg_indices(self._labels, counter_idx, DEFAULT_NONEEG_PATTERNS))
        box, checkboxes = make_checkbox_grid(
            list(range(len(self._labels))),
            formatter=lambda i: f"[{i}] {self._labels[i]}",
            columns=4,
        )
        for i, cb in checkboxes.items():
            cb.setChecked(i in eeg_idx)
        self._eeg_checkboxes = checkboxes
        old_layout = self.eeg_channels_box.layout()
        old_layout.replaceWidget(self._eeg_grid_placeholder, box)
        self._eeg_grid_placeholder.deleteLater()
        self._eeg_grid_placeholder = box
        old_layout.activate()
        self.adjustSize()

        baseline = load_baseline(self._device_name)
        if baseline:
            self.baseline_status_label.setText(
                f"Baseline recorded {baseline.get('timestamp', '?')} "
                f"({baseline.get('n_windows', 0)} windows): "
                f"A50 p95={baseline['a50_p95']:.2f}, CMI p95={baseline['cmi_p95']:.3f}"
            )
        else:
            self.baseline_status_label.setText("No baseline recorded for this device yet.")

        self.btn_record_baseline.setEnabled(True)
        self.btn_start.setEnabled(True)
        self.status_label.setText("Connected (not monitoring)")
        self.status_label.setStyleSheet("font-weight: bold; font-size: 16px; padding: 4px;")

    def _set_all_eeg_checked(self, checked: bool):
        for cb in self._eeg_checkboxes.values():
            cb.setChecked(checked)

    def _selected_eeg_indices(self) -> List[int]:
        return sorted(i for i, cb in self._eeg_checkboxes.items() if cb.isChecked())

    def _selected_counter_index(self) -> Optional[int]:
        return self.counter_combo.currentData()

    # -------------------------------------------------------------------
    # Baseline recording
    # -------------------------------------------------------------------

    def _on_record_baseline(self):
        eeg_idx = self._selected_eeg_indices()
        if len(eeg_idx) < 2:
            QMessageBox.warning(self, "Not enough channels", "Select at least 2 EEG channels for the common-mode calculation.")
            return
        self.btn_record_baseline.setEnabled(False)
        self.btn_start.setEnabled(False)
        self.baseline_progress_bar.setVisible(True)
        self.baseline_progress_bar.setRange(0, 100)
        self.baseline_progress_bar.setValue(0)

        duration = self.baseline_duration_spin.value()
        thread = _RefGndMonitorThread(
            inlet=self._inlet,
            fs=self._fs,
            eeg_idx=eeg_idx,
            counter_idx=self._selected_counter_index(),
            line_freq=self.line_freq_spin.value(),
            sat_level=self.sat_level_spin.value(),
            baseline_seconds=float(duration),
            parent=self,
        )
        self._baseline_thread = thread
        thread.baseline_progress.connect(self._on_baseline_progress)
        thread.baseline_ready.connect(self._on_baseline_ready)
        thread.failed.connect(self._on_thread_failed)
        thread.finished.connect(self._on_baseline_thread_finished)
        thread.start()

    def _on_baseline_progress(self, elapsed: float, total: float):
        pct = int(100 * elapsed / total) if total else 0
        self.baseline_progress_bar.setValue(min(100, pct))

    def _on_baseline_ready(self, a50_values: list, cmi_values: list):
        if len(a50_values) < 2:
            QMessageBox.warning(self, "Baseline failed", "Not enough windows were collected to save a baseline.")
            return
        baseline = save_baseline(self._device_name, a50_values, cmi_values, line_freq=self.line_freq_spin.value())
        self.baseline_status_label.setText(
            f"Baseline recorded {baseline['timestamp']} ({baseline['n_windows']} windows): "
            f"A50 p95={baseline['a50_p95']:.2f}, CMI p95={baseline['cmi_p95']:.3f}"
        )

    def _on_baseline_thread_finished(self):
        self.baseline_progress_bar.setVisible(False)
        self.btn_record_baseline.setEnabled(True)
        self.btn_start.setEnabled(True)
        self._baseline_thread = None

    # -------------------------------------------------------------------
    # Live monitoring
    # -------------------------------------------------------------------

    def _on_start_monitoring(self):
        eeg_idx = self._selected_eeg_indices()
        if len(eeg_idx) < 2:
            QMessageBox.warning(self, "Not enough channels", "Select at least 2 EEG channels for the common-mode calculation.")
            return
        self._hist = []
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_record_baseline.setEnabled(False)

        thread = _RefGndMonitorThread(
            inlet=self._inlet,
            fs=self._fs,
            eeg_idx=eeg_idx,
            counter_idx=self._selected_counter_index(),
            line_freq=self.line_freq_spin.value(),
            sat_level=self.sat_level_spin.value(),
            parent=self,
        )
        self._monitor_thread = thread
        thread.window_ready.connect(self._on_window_ready)
        thread.failed.connect(self._on_thread_failed)
        thread.finished.connect(self._on_monitor_thread_finished)
        thread.start()

    def _on_stop_monitoring(self):
        if self._monitor_thread is not None:
            self._monitor_thread.request_stop()

    def _on_monitor_thread_finished(self):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_record_baseline.setEnabled(True)
        self._monitor_thread = None
        self.status_label.setText("Connected (not monitoring)")
        self.status_label.setStyleSheet("font-weight: bold; font-size: 16px; padding: 4px;")

    def _current_thresholds(self) -> Dict[str, Any]:
        baseline = load_baseline(self._device_name)
        return thresholds_for_baseline(
            baseline,
            a50_warn_default=self.a50_warn_spin.value(),
            cmi_warn_default=self.cmi_warn_spin.value(),
            cmi_bad_default=self.cmi_bad_spin.value(),
            a50_factor=self.a50_factor_spin.value(),
        )

    def _on_window_ready(self, m: Dict[str, Any]):
        self._hist.append(m)
        if len(self._hist) > DEFAULT_SMOOTH_WINDOWS:
            self._hist = self._hist[-DEFAULT_SMOOTH_WINDOWS:]
        ms = smoothed(self._hist, m)
        thr = self._current_thresholds()
        status, reason = verdict(ms, thr)
        self.status_label.setText(status)
        self.status_label.setStyleSheet(
            f"font-weight: bold; font-size: 16px; padding: 4px; color: white; "
            f"background-color: {STATUS_COLORS.get(status, '#444')};"
        )
        self.reason_label.setText(reason)
        self.metrics_label.setText(
            f"A50={ms['a50_med']:.2f}  CMI={ms['cmi']:.3f}  rail={ms['rail']:.4f}  "
            f"gaps={ms['gaps']}  (thresholds: {thr['source']})"
        )
        if self._log_fh:
            el = time.time()
            self._log_fh.write(
                f"{el:.3f},{m['a50_med']:.4f},{m['a50_max']:.4f},{m['cmi']:.4f},"
                f"{m['rail']:.6f},{m['gaps']},{m['n_hot']},{status}\n"
            )
            self._log_fh.flush()

    def _on_thread_failed(self, msg: str):
        QMessageBox.critical(self, "Ref/GND monitor error", msg)

    # -------------------------------------------------------------------
    # CSV logging
    # -------------------------------------------------------------------

    def _on_log_toggled(self, checked: bool):
        self.btn_choose_log.setEnabled(checked)
        if not checked and self._log_fh:
            self._log_fh.close()
            self._log_fh = None

    def _on_choose_log_file(self):
        path, _ = QFileDialog.getSaveFileName(self, "Choose CSV log file", ".", "CSV files (*.csv)")
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        self._log_path = path
        self.log_path_label.setText(path)
        try:
            self._log_fh = open(path, "w")
            self._log_fh.write("t,a50_med,a50_max,cmi,rail,gaps,n_hot,status\n")
        except Exception as exc:
            QMessageBox.critical(self, "Could not open log file", str(exc))
            self.log_checkbox.setChecked(False)

    # -------------------------------------------------------------------
    # Teardown
    # -------------------------------------------------------------------

    def _teardown_inlet(self):
        if self._inlet is not None:
            try:
                self._inlet.close_stream()
            except Exception:
                pass
            self._inlet = None

    def closeEvent(self, event):
        for thread in (self._monitor_thread, self._baseline_thread):
            if thread is not None and thread.isRunning():
                thread.request_stop()
                thread.wait(3000)
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None
        self._teardown_inlet()
        super().closeEvent(event)
