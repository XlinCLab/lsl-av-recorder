"""Dialog shown when a camera's live capture rate diverges from what was expected."""
from __future__ import annotations

from typing import Optional

from PyQt6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton,
                             QRadioButton, QSpinBox, QVBoxLayout, QWidget)

from ..video.video_recorder import (FPS_ACTION_ABORT, FPS_ACTION_ACCEPT,
                                    FPS_ACTION_IGNORE)

DEFAULT_IGNORE_MINUTES = 10
MAX_IGNORE_MINUTES = 24 * 60


def ignore_seconds_for(until_stopped: bool, minutes: int) -> Optional[float]:
    """Seconds to silence the prompt for; None means until the recording stops."""
    return None if until_stopped else float(minutes) * 60.0


class FpsDivergenceDialog(QDialog):
    """Modal dialog offering: accept the observed rate, ignore the warning
    (until the recording stops, or for a number of minutes), or abort.

    After exec(), `action` is one of the FPS_ACTION_* values and
    `ignore_seconds` is set when action is ignore (None = until stopped).
    Closing the window any other way counts as an abort, rather than silently
    continuing on an unacknowledged deviation."""

    def __init__(self, title: str, message: str, note: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.action = FPS_ACTION_ABORT
        self.ignore_seconds: Optional[float] = None

        message_label = QLabel(message)
        message_label.setWordWrap(True)

        self.until_stopped_radio = QRadioButton("Until this recording is stopped")
        self.until_stopped_radio.setChecked(True)
        self.for_minutes_radio = QRadioButton("For")
        self.minutes_spin = QSpinBox()
        self.minutes_spin.setRange(1, MAX_IGNORE_MINUTES)
        self.minutes_spin.setValue(DEFAULT_IGNORE_MINUTES)
        self.minutes_spin.setSuffix(" minutes")
        self.minutes_spin.setEnabled(False)
        self.for_minutes_radio.toggled.connect(self.minutes_spin.setEnabled)

        ignore_row = QHBoxLayout()
        ignore_row.addWidget(QLabel("If ignoring, ignore this camera's warnings:"))
        ignore_row.addStretch(1)
        duration_row = QHBoxLayout()
        duration_row.addWidget(self.until_stopped_radio)
        duration_row.addSpacing(12)
        duration_row.addWidget(self.for_minutes_radio)
        duration_row.addWidget(self.minutes_spin)
        duration_row.addStretch(1)

        self.accept_btn = QPushButton("Accept and continue")
        self.ignore_btn = QPushButton("Ignore")
        self.abort_btn = QPushButton("Abort recording")
        self.abort_btn.setDefault(True)
        self.accept_btn.clicked.connect(lambda: self._finish(FPS_ACTION_ACCEPT))
        self.ignore_btn.clicked.connect(lambda: self._finish(FPS_ACTION_IGNORE))
        self.abort_btn.clicked.connect(lambda: self._finish(FPS_ACTION_ABORT))
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.accept_btn)
        buttons.addWidget(self.ignore_btn)
        buttons.addWidget(self.abort_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(message_label)
        if note:
            note_label = QLabel(note)
            note_label.setWordWrap(True)
            layout.addWidget(note_label)
        layout.addLayout(ignore_row)
        layout.addLayout(duration_row)
        layout.addSpacing(8)
        layout.addLayout(buttons)

    def _finish(self, action: str):
        self.action = action
        if action == FPS_ACTION_IGNORE:
            self.ignore_seconds = ignore_seconds_for(
                self.until_stopped_radio.isChecked(), self.minutes_spin.value()
            )
        self.accept()

    def reject(self):
        self.action = FPS_ACTION_ABORT
        super().reject()
