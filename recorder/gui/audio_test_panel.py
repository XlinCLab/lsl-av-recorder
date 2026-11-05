"""The "Test audio" panel of the Audio tab: record a few seconds with the selected
device and settings, watch the input level, then play the recording back."""
from __future__ import annotations

import html
from typing import Callable, Optional

from PyQt6.QtCore import QSize, QTimer
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel,
                             QMessageBox, QProgressBar, QPushButton, QSpinBox,
                             QStyle, QTextBrowser, QVBoxLayout, QWidget)

from ..audio.audio_test import (AudioPlayer, AudioTestResult, AudioTestSession,
                                LevelReading)
from ..audio.devices import list_output_devices
from ..audio.lsl_audio import AudioStreamSettings

TICK_MS = 50
# The level meter spans this range (dBFS)
METER_MIN_DBFS = -60
DEFAULT_TEST_SECONDS = 5
MIN_TEST_SECONDS = 2
MAX_TEST_SECONDS = 30
_SYSTEM_DEFAULT_OUTPUT = "(system default)"
HELP_TEXT = (
    "Records a few seconds with the selected audio device and settings, then plays them back.\n\n"
    "The result checks the true measured sample rate against the configured sample rate, "
    "and warns about potential clipping or quiet/silent audio signal."
)
NORMALIZE_HELP_TEXT = (
    "Plays a quiet recording louder so it can be judged by ear.\n\n"
    "This changes only the playback, not the recording."
)
# A channel peaking below this is shown as "silent" rather than as a very large negative level
SILENT_CHANNEL_DBFS = -120.0


def meter_value(dbfs_level: float) -> int:
    """Progress-bar value (tenths of a dB above METER_MIN_DBFS, 0..600) for a level."""
    return int(round(max(0.0, min(1.0, (dbfs_level - METER_MIN_DBFS) / -METER_MIN_DBFS)) * 600))


def format_result_html(result: AudioTestResult) -> str:
    """The summary shown after a test recording."""
    esc = html.escape
    lines = []
    if result.measured_rate is None:
        lines.append("Delivered rate: not measured (recording too short)")
    else:
        ok = abs(result.measured_rate - result.samplerate) / result.samplerate <= 0.01
        lines.append(
            f"{'✓' if ok else '⚠'} Delivered rate: {result.measured_rate:,.0f} Hz "
            f"(configured {result.configured_rate:,} Hz)"
        )
    peaks = ", ".join("silent" if db <= SILENT_CHANNEL_DBFS else f"{db:.1f}" for db in result.levels.channel_peak_dbfs)
    lines.append(
        f"Level: peak {result.levels.peak_dbfs:.1f} dBFS, RMS {result.levels.rms_dbfs:.1f} dBFS"
        + (f" (peak per channel: {peaks} dBFS)" if len(result.levels.channel_peaks) > 1 else "")
    )
    lines.append(f"Recorded {result.duration_s:.1f} s from {result.device_name or 'the selected device'}")
    text = "<br>".join(esc(line) for line in lines)
    if result.warnings:
        text += "<br><br><b>Warnings</b><ul style='margin-top:2px'>" + "".join(
            f"<li>{esc(w)}</li>" for w in result.warnings
        ) + "</ul>"
    else:
        text += "<br><br>✓ No problems detected."
    return text


class AudioTestPanel(QFrame):
    def __init__(
        self,
        get_settings: Callable[[], AudioStreamSettings],
        prepare: Callable[[], bool],
        log: Callable[[str, str], None],
        parent: Optional[QWidget] = None,
    ):
        """`prepare()` refreshes and validates the audio settings and returns False (after telling
        the user why) if a test cannot be made now; `get_settings()` builds the AudioStreamSettings
        for the selected device and raises if the device is not available."""
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._get_settings = get_settings
        self._prepare = prepare
        self._log = log
        self._session: Optional[AudioTestSession] = None
        self._result: Optional[AudioTestResult] = None
        self._player = AudioPlayer(status_cb=lambda m, level="INFO": self._log(m, level))
        self._external_busy = False

        self.duration = QSpinBox()
        self.duration.setRange(MIN_TEST_SECONDS, MAX_TEST_SECONDS)
        self.duration.setValue(DEFAULT_TEST_SECONDS)
        self.duration.setSuffix(" s")
        self.btn_record = QPushButton("Record test")
        self.btn_stop = QPushButton("Stop")
        self.btn_play = QPushButton("Play back")
        self.btn_stop_play = QPushButton("Stop playback")
        self.output_device = QComboBox()
        self.normalize = QCheckBox("Normalize playback volume")
        self.normalize.setChecked(True)
        self.normalize.setToolTip(
            "Plays the recording louder if it is quiet. Only the playback changes, not the recording."
        )
        self.meter = QProgressBar()
        self.meter.setRange(0, 600)
        self.meter.setTextVisible(False)
        self.meter.setFixedHeight(14)
        self.level_label = QLabel("")
        # Transient status line (recording, playing); hidden while there is nothing to say
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setVisible(False)

        self.btn_help = self._help_button("Test audio", HELP_TEXT)
        self.btn_help_normalize = self._help_button("Normalize playback volume", NORMALIZE_HELP_TEXT)
        header_row = QHBoxLayout()
        header_row.addWidget(QLabel("Test audio"))
        header_row.addWidget(self.btn_help)
        header_row.addStretch(1)
        # A text view rather than a word-wrapped label: its height does not depend on the
        # panel width, which a label inside nested layouts and a scroll area gets wrong
        self.result_view = QTextBrowser()
        self.result_view.setFrameShape(QFrame.Shape.NoFrame)
        self.result_view.viewport().setAutoFillBackground(False)
        self.result_view.setMinimumHeight(140)

        # Laid out in rows that stay narrow, since the panel sits in a column beside the settings
        self.output_device.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.output_device.setMinimumContentsLength(12)

        record_row = QHBoxLayout()
        record_row.addWidget(QLabel("Duration"))
        record_row.addWidget(self.duration)
        record_row.addWidget(self.btn_record)
        record_row.addWidget(self.btn_stop)
        record_row.addStretch(1)
        meter_row = QHBoxLayout()
        meter_row.addWidget(QLabel("Input level"))
        meter_row.addWidget(self.meter, 1)
        meter_row.addWidget(self.level_label)
        play_row = QHBoxLayout()
        play_row.addWidget(self.btn_play)
        play_row.addWidget(self.btn_stop_play)
        play_row.addStretch(1)
        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("Playback output"))
        output_row.addWidget(self.output_device, 1)

        normalize_row = QHBoxLayout()
        normalize_row.addWidget(self.normalize)
        normalize_row.addWidget(self.btn_help_normalize)
        normalize_row.addStretch(1)

        layout = QVBoxLayout()
        layout.addLayout(header_row)
        layout.addLayout(record_row)
        layout.addLayout(meter_row)
        layout.addWidget(self.status)
        layout.addSpacing(6)
        layout.addLayout(play_row)
        layout.addLayout(output_row)
        layout.addLayout(normalize_row)
        layout.addSpacing(6)
        layout.addWidget(self.result_view, 1)
        self.setLayout(layout)

        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self.btn_record.clicked.connect(self.start_recording)
        self.btn_stop.clicked.connect(self.finish_recording)
        self.btn_play.clicked.connect(self.start_playback)
        self.btn_stop_play.clicked.connect(self.stop_playback)

        self.refresh_output_devices()
        self._update_buttons()

    def _help_button(self, title: str, text: str) -> QPushButton:
        """A small "?" button that shows `text`."""
        button = QPushButton()
        button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarContextHelpButton))
        button.setIconSize(QSize(12, 12))
        button.setFixedSize(24, 24)
        button.clicked.connect(lambda: QMessageBox.information(self, title, text))
        return button

    def _set_status(self, text: str) -> None:
        """Show a transient status line; hidden when empty so it takes no room."""
        self.status.setText(text)
        self.status.setVisible(bool(text))

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def recording(self) -> bool:
        return self._session is not None

    @property
    def playing(self) -> bool:
        return self._player.playing

    def is_active(self) -> bool:
        """True while the panel holds an audio device (recording or playing)."""
        return self.recording or self.playing

    def set_external_busy(self, busy: bool) -> None:
        """Disable the panel while a recording or test recording uses the audio device."""
        self._external_busy = busy
        self._update_buttons()

    def _update_buttons(self) -> None:
        idle = not self.is_active() and not self._external_busy
        self.btn_record.setEnabled(idle)
        self.duration.setEnabled(idle)
        self.btn_stop.setEnabled(self.recording)
        self.btn_play.setEnabled(idle and self._result is not None and len(self._result.samples) > 0)
        self.btn_stop_play.setEnabled(self.playing)
        self.output_device.setEnabled(not self.is_active())
        self.normalize.setEnabled(not self.is_active())

    def refresh_output_devices(self) -> None:
        """Repopulate the playback devices (keeps the current choice if it still exists)."""
        previous = self.output_device.currentData()
        try:
            devices = list_output_devices()
        except Exception as exc:
            self._log(f"Could not list audio output devices: {exc}", "WARNING")
            devices = []
        self.output_device.blockSignals(True)
        self.output_device.clear()
        self.output_device.addItem(_SYSTEM_DEFAULT_OUTPUT, None)
        for d in devices:
            api = f" ({d['hostapi_name']})" if d.get("hostapi_name") else ""
            self.output_device.addItem(f"[{d['index']}] {d['name']}{api}", d["index"])
        idx = self.output_device.findData(previous)
        self.output_device.setCurrentIndex(max(idx, 0))
        self.output_device.blockSignals(False)

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def start_recording(self) -> None:
        if self.is_active() or self._external_busy:
            return
        if not self._prepare():
            return
        self._result = None
        self.result_view.clear()
        try:
            settings = self._get_settings()
            session = AudioTestSession(
                settings, duration_s=float(self.duration.value()),
                status_cb=lambda m, level="INFO": self._log(m, level),
            )
            session.start()
        except Exception as exc:
            self._log(f"Audio test could not start: {exc}", "ERROR")
            QMessageBox.critical(self, "Audio test failed", str(exc))
            self._update_buttons()
            return
        self._session = session
        self._log(
            f"Audio test: recording {self.duration.value()} s from [{settings.device_index}] "
            f"{settings.device_name} at {settings.samplerate} Hz, {settings.channels} channel(s)", "INFO",
        )
        self._set_status("Recording...")
        self._timer.start()
        self._update_buttons()

    def finish_recording(self) -> None:
        """Stop the recording (early if the button was pressed) and show its result."""
        session, self._session = self._session, None
        if session is None:
            return
        self._timer.stop()
        try:
            result = session.stop()
        except Exception as exc:
            self._log(f"Audio test failed while stopping: {exc}", "ERROR")
            self._set_status("The audio test failed.")
            self._update_buttons()
            return
        self._result = result
        self.meter.setValue(0)
        self.level_label.setText("")
        self._set_status("" if len(result.samples) else "Nothing was recorded.")
        self.result_view.setHtml(format_result_html(result))
        for w in result.warnings:
            self._log(f"Audio test: {w}", "WARNING")
        rate = "not measured" if result.measured_rate is None else f"{result.measured_rate:.1f} Hz"
        self._log(
            f"Audio test finished: {result.duration_s:.1f} s, measured rate {rate} "
            f"(configured {result.configured_rate} Hz), peak {result.levels.peak_dbfs:.1f} dBFS, "
            f"{len(result.warnings)} warning(s)", "INFO",
        )
        self._update_buttons()

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------

    def start_playback(self) -> None:
        if self._result is None or self.is_active() or self._external_busy:
            return
        try:
            info = self._player.play(
                self._result.samples, self._result.samplerate,
                output_device=self.output_device.currentData(), normalize=self.normalize.isChecked(),
            )
        except Exception as exc:
            self._log(f"Audio playback failed: {exc}", "ERROR")
            QMessageBox.critical(self, "Playback failed", str(exc))
            self._update_buttons()
            return
        extra = []
        if info.gain_db:
            extra.append(f"volume raised by {info.gain_db:.0f} dB")
        if info.resampled:
            extra.append(f"resampled to {info.samplerate} Hz for this device")
        self._set_status(f"Playing on {info.device_name}" + (f" ({', '.join(extra)})" if extra else "") + "...")
        self._log(f"Audio test: playing back on {info.device_name}" + (f" ({', '.join(extra)})" if extra else ""), "INFO")
        self._timer.start()
        self._update_buttons()

    def stop_playback(self) -> None:
        self._player.stop()
        if not self.recording:
            self._timer.stop()
        self._set_status("Playback stopped.")
        self._update_buttons()

    # ------------------------------------------------------------------
    # Timer
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        if self._session is not None:
            reading: LevelReading = self._session.read_levels()
            self.meter.setValue(meter_value(reading.peak_dbfs))
            self.level_label.setText(
                f"{reading.peak_hold_dbfs:.1f} dBFS" + ("  CLIPPING" if reading.clipped else "")
            )
            self._set_status(
                f"Recording... {min(self._session.elapsed_s, self._session.duration_s):.1f} / "
                f"{self._session.duration_s:.0f} s"
            )
            if self._session.reached_duration:
                self.finish_recording()
        elif self._player.playing:
            if not self._player.poll():
                self._timer.stop()
                self._set_status("")
                self._update_buttons()
        else:
            self._timer.stop()

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Release the audio device: discard a recording in progress and stop playback."""
        self._timer.stop()
        if self._session is not None:
            session, self._session = self._session, None
            session.cancel()
        self._player.stop()
        self._update_buttons()
