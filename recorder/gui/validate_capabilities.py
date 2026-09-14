from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QDialog, QDoubleSpinBox, QGridLayout,
                             QGroupBox, QHBoxLayout, QLabel, QMessageBox,
                             QProgressBar, QPushButton, QSpinBox, QTableWidget,
                             QTableWidgetItem, QVBoxLayout, QWidget)

from ..video.camera_settings import (combination_key,
                                     load_validated_combinations,
                                     store_validated_combinations,
                                     validate_combination)
from ..video.constants import (DEFAULT_FPS_TOLERANCE,
                               DEFAULT_VALIDATION_DURATION,
                               DEFAULT_VALIDATION_WARMUP, FPS_TOLERANCE_RANGE,
                               VALIDATION_DURATION_RANGE,
                               VALIDATION_WARMUP_RANGE)

_CHECKBOX_COLUMNS = 6


def _combinations_for_selection(
    modes_by_format: Dict[str, List[Tuple[int, int, set]]],
    checked_formats: set,
    checked_resolutions: set,
    checked_fps: set,
) -> List[Tuple[str, int, int, int]]:
    """Filter declared (pixel_format, width, height, fps) combinations
    matching the user's current selection."""
    combos: List[Tuple[str, int, int, int]] = []
    for fmt, modes in modes_by_format.items():
        if fmt not in checked_formats:
            continue
        for width, height, fps_values in modes:
            if (width, height) not in checked_resolutions:
                continue
            for fps in fps_values:
                if fps in checked_fps:
                    combos.append((fmt, width, height, fps))
    return combos


def _effective_result(
    result: Optional[Dict[str, Any]],
    fps: float,
    tolerance: float,
) -> Optional[Dict[str, Any]]:
    """Re-evaluate a stored result's `passed` flag against `tolerance`
    (a fraction, e.g. 0.15 = 15%) rather than trusting whichever tolerance
    happened to be in effect when it was originally measured.
    Returns `result` unchanged if there is nothing to re-evaluate
    (either no result, or no measured_fps recorded) or if
    re-evaluating does not actually change the passed flag."""
    if result is None or result.get("measured_fps") is None:
        return result
    measured = result["measured_fps"]
    passed = bool(
        result.get("could_open", True)
        and (1 - tolerance) * fps <= measured <= (1 + tolerance) * fps
    )
    if passed == result.get("passed"):
        return result
    return {**result, "passed": passed}


def _combos_needing_validation(
    combos: List[Tuple[str, int, int, int]],
    cached: Dict[str, Dict[str, Any]],
    tolerance: float,
) -> Tuple[List[Tuple[str, int, int, int]], int]:
    """Split `combos` into (to_test, skipped_passed_count): a combination
    already passing under `tolerance` is skipped; anything unvalidated or
    currently failing is always (re)tested."""
    to_test: List[Tuple[str, int, int, int]] = []
    skipped_passed = 0
    for pf, w, h, fps in combos:
        result = _effective_result(cached.get(combination_key(pf, w, h, fps)), fps, tolerance)
        if result and result.get("passed"):
            skipped_passed += 1
            continue
        to_test.append((pf, w, h, fps))
    return to_test, skipped_passed


class _ValidateCombinationsThread(QThread):
    combo_validated = pyqtSignal(str, int, int, int, dict)
    progress = pyqtSignal(int, int)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(
        self,
        combos: List[Tuple[str, int, int, int]],
        devnode: str,
        device_index: Optional[int],
        device_name: Optional[str],
        fps_tolerance: float = DEFAULT_FPS_TOLERANCE,
        warmup: float = DEFAULT_VALIDATION_WARMUP,
        duration: float = DEFAULT_VALIDATION_DURATION,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._combos = combos
        self._devnode = devnode
        self._device_index = device_index
        self._device_name = device_name
        self._fps_tolerance = fps_tolerance
        self._warmup = warmup
        self._duration = duration
        self._cancel_requested = False

    def request_cancel(self):
        """
        Request cancelation of validation thread.
        NB: Takes effect between combinations, not instantly.
        """
        self._cancel_requested = True

    def run(self):
        try:
            total = len(self._combos)
            for i, (pixel_format, width, height, fps) in enumerate(self._combos, start=1):
                if self._cancel_requested:
                    self.cancelled.emit()
                    return
                result = validate_combination(
                    devnode=self._devnode,
                    device_index=self._device_index,
                    pixel_format=pixel_format,
                    width=width,
                    height=height,
                    fps=fps,
                    device_name=self._device_name,
                    fps_tolerance=self._fps_tolerance,
                    warmup=self._warmup,
                    duration=self._duration,
                )
                self.combo_validated.emit(pixel_format, width, height, fps, result)
                self.progress.emit(i, total)
        except Exception as exc:
            self.failed.emit(str(exc))


def _make_checkbox_grid(values: List, formatter=str) -> Tuple[QWidget, Dict[Any, QCheckBox]]:
    """Just the wrapping grid of checkboxes -- select-all/deselect-all is
    handled once, collectively, at the dialog level (see
    ValidateCapabilitiesDialog), not per section."""
    checkboxes: Dict[Any, QCheckBox] = {}
    grid = QGridLayout()
    for i, value in enumerate(values):
        cb = QCheckBox(formatter(value))
        checkboxes[value] = cb
        grid.addWidget(cb, i // _CHECKBOX_COLUMNS, i % _CHECKBOX_COLUMNS)
    box = QGroupBox()
    box.setLayout(grid)
    return box, checkboxes


class ValidateCapabilitiesDialog(QDialog):
    """Lets the user pick which declared fps / resolution / pixel-format
    values they actually care about, then empirically opens the device for
    just the resulting (declared) combinations to measure real delivered fps
    -- the cross-platform, on-demand replacement for what used to be an
    automatic, Windows-only, every-combination check on every capability
    refresh (slow, and still only ever tested each combination's declared
    max, never any of its other declared fps values).

    The results table always shows exactly the combinations matching the
    current checkbox selection (live, as checkboxes are toggled) with
    whatever validation result -- from this session or a previous one -- is
    on record for each; nothing selected means nothing shown, not a giant
    list of every declared combination.

    validationStarted/validationFinished let the owning CameraPanel (and, in
    turn, MainWindow) stop this camera's live preview for the duration of an
    actual validation run and restart it afterward -- the same pattern
    already used around Apply settings/Refresh device capabilities, since
    empirically opening the device here would otherwise contend with an
    active preview's own open capture.
    """

    validationStarted = pyqtSignal()
    validationFinished = pyqtSignal()

    def __init__(
        self,
        modes_by_format: Dict[str, List[Tuple[int, int, set]]],
        devnode: str,
        device_index: Optional[int],
        device_name: Optional[str],
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Validate camera capabilities")
        self._modes_by_format = modes_by_format
        self._devnode = devnode
        self._device_index = device_index
        self._device_name = device_name
        self._thread: Optional[_ValidateCombinationsThread] = None
        self._pending_results: Dict[str, Dict[str, Any]] = {}
        self._displayed_combos: List[Tuple[str, int, int, int]] = []

        all_formats = sorted(modes_by_format.keys())
        all_resolutions = sorted(
            {(w, h) for modes in modes_by_format.values() for w, h, _ in modes},
            key=lambda r: (r[0] * r[1], r[0]),
        )
        all_fps = sorted({fps for modes in modes_by_format.values() for _, _, fps_values in modes for fps in fps_values})

        fps_box, self._fps_checkboxes = _make_checkbox_grid(all_fps)
        res_box, self._res_checkboxes = _make_checkbox_grid(all_resolutions, formatter=lambda r: f"{r[0]}x{r[1]}")
        fmt_box, self._format_checkboxes = _make_checkbox_grid(all_formats)
        self._all_checkboxes: List[QCheckBox] = [
            *self._fps_checkboxes.values(), *self._res_checkboxes.values(), *self._format_checkboxes.values(),
        ]
        for cb in self._all_checkboxes:
            cb.stateChanged.connect(self._refresh_results_table)

        select_row = QHBoxLayout()
        btn_select_all = QPushButton("Select all")
        btn_deselect_all = QPushButton("Deselect all")
        btn_select_all.clicked.connect(lambda: [cb.setChecked(True) for cb in self._all_checkboxes])
        btn_deselect_all.clicked.connect(lambda: [cb.setChecked(False) for cb in self._all_checkboxes])
        select_row.addWidget(btn_select_all)
        select_row.addWidget(btn_deselect_all)
        select_row.addStretch(1)

        self.fps_tolerance_spin = QSpinBox()
        self.fps_tolerance_spin.setRange(
            int(round(min(FPS_TOLERANCE_RANGE) * 100)),
            int(round(max(FPS_TOLERANCE_RANGE) * 100)),
        )
        self.fps_tolerance_spin.setValue(int(round(DEFAULT_FPS_TOLERANCE * 100)))
        self.fps_tolerance_spin.setSuffix("%")
        self.fps_tolerance_spin.setToolTip(
            "How much a measured frame rate may diverge from the requested "
            "frame rate before a combination counts as failed. "
            "Lower = stricter."
        )
        self.warmup_spin = QDoubleSpinBox()
        self.warmup_spin.setRange(*VALIDATION_WARMUP_RANGE)
        self.warmup_spin.setDecimals(1)
        self.warmup_spin.setSingleStep(0.5)
        self.warmup_spin.setValue(DEFAULT_VALIDATION_WARMUP)
        self.warmup_spin.setSuffix(" s")
        self.warmup_spin.setToolTip(
            "Duration of warm-up period: how long to discard captured frames before measuring, "
            "to allow device capture to warm up and settle first."
        )
        self.duration_spin = QDoubleSpinBox()
        self.duration_spin.setRange(*VALIDATION_DURATION_RANGE)
        self.duration_spin.setDecimals(1)
        self.duration_spin.setSingleStep(0.5)
        self.duration_spin.setValue(DEFAULT_VALIDATION_DURATION)
        self.duration_spin.setSuffix(" s")
        self.duration_spin.setToolTip(
            "Duration of the frame rate measurement window (after warm-up period)."
        )
        settings_row = QHBoxLayout()
        settings_row.addWidget(QLabel("FPS tolerance"))
        settings_row.addWidget(self.fps_tolerance_spin)
        settings_row.addWidget(QLabel("Warmup duration"))
        settings_row.addWidget(self.warmup_spin)
        settings_row.addWidget(QLabel("Measurement duration"))
        settings_row.addWidget(self.duration_spin)
        settings_row.addStretch(1)

        self.btn_validate = QPushButton("Validate selected")
        self.btn_validate.clicked.connect(self._on_validate)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setVisible(False)
        self.btn_cancel.clicked.connect(self._on_cancel)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        # QProgressBar's own text overlay (setFormat/setTextVisible) is not
        # painted at all under some native styles (notably macOS's) no matter
        # what it's set to -- a separate label is the only way to reliably
        # show the percentage on every platform.
        self.progress_label = QLabel("")
        self.progress_label.setVisible(False)

        self.results_table = QTableWidget(0, 5)
        self.results_table.setHorizontalHeaderLabels(
            ["Pixel format", "Resolution", "FPS", "Result", "Measured FPS"]
        )
        self.results_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        self.btn_close = QPushButton("Save and close")
        self.btn_close.setToolTip(
            "Results are already saved as each combination finishes -- this just "
            "closes the dialog (same as the window's own close button)."
        )
        self.btn_close.clicked.connect(self.accept)

        progress_row = QHBoxLayout()
        progress_row.addWidget(self.progress_bar)
        progress_row.addWidget(self.progress_label)

        layout = QVBoxLayout()
        layout.addLayout(select_row)
        layout.addWidget(QLabel("Frame rate"))
        layout.addWidget(fps_box)
        layout.addWidget(QLabel("Resolution"))
        layout.addWidget(res_box)
        layout.addWidget(QLabel("Pixel format"))
        layout.addWidget(fmt_box)
        layout.addWidget(QLabel("Validation settings"))
        layout.addLayout(settings_row)
        validate_row = QHBoxLayout()
        validate_row.addWidget(self.btn_validate)
        validate_row.addWidget(self.btn_cancel)
        layout.addLayout(validate_row)
        layout.addLayout(progress_row)
        layout.addWidget(QLabel("Results for the checked selection above (this device, all sessions):"))
        layout.addWidget(self.results_table)
        layout.addWidget(self.btn_close)
        self.setLayout(layout)

        # Re-evaluate displayed PASS/FAIL against the tolerance currently selected
        self.fps_tolerance_spin.valueChanged.connect(self._refresh_results_table)

        self._refresh_results_table()

    def _device_key(self) -> str:
        return self._device_name or self._devnode or "unknown"

    def _checked_selection(self) -> Tuple[set, set, set]:
        checked_formats = {v for v, cb in self._format_checkboxes.items() if cb.isChecked()}
        checked_resolutions = {v for v, cb in self._res_checkboxes.items() if cb.isChecked()}
        checked_fps = {v for v, cb in self._fps_checkboxes.items() if cb.isChecked()}
        return checked_formats, checked_resolutions, checked_fps

    def _matching_combos(self) -> List[Tuple[str, int, int, int]]:
        checked_formats, checked_resolutions, checked_fps = self._checked_selection()
        combos = _combinations_for_selection(
            self._modes_by_format, checked_formats, checked_resolutions, checked_fps,
        )
        return sorted(combos, key=lambda c: (c[0], c[1], c[2], c[3]))

    def _refresh_results_table(self, *_args):
        """Repopulate the table with exactly the combinations matching the
        current checkbox selection -- nothing checked means nothing shown.
        Each row's result comes from this run in progress if there is one
        (self._pending_results, so toggling a checkbox mid-validation doesn't
        lose results not yet written to disk), otherwise from whatever was
        already on record for this device."""
        cached = load_validated_combinations(sys.platform, self._device_key())
        tolerance = self.fps_tolerance_spin.value() / 100.0
        self._displayed_combos = self._matching_combos()
        self.results_table.setRowCount(len(self._displayed_combos))
        for row, (pf, w, h, fps) in enumerate(self._displayed_combos):
            key = combination_key(pf, w, h, fps)
            result = self._pending_results.get(key) or cached.get(key)
            self._set_result_row(row, pf, w, h, fps, _effective_result(result, fps, tolerance))

    def _set_result_row(
        self, row: int, pixel_format: str, width: int, height: int, fps: float,
        result: Optional[Dict[str, Any]],
    ):
        self.results_table.setItem(row, 0, QTableWidgetItem(pixel_format))
        self.results_table.setItem(row, 1, QTableWidgetItem(f"{width}x{height}"))
        self.results_table.setItem(row, 2, QTableWidgetItem(f"{fps:g}"))
        if result is None:
            status, measured = "", ""
        elif result.get("passed"):
            status, measured = "✅ PASS", f"{result.get('measured_fps'):.2f}" if result.get("measured_fps") is not None else ""
        elif not result.get("could_open", True):
            status, measured = "❌ NOT SUPPORTED", ""
        else:
            status = "⚠️ FAIL"
            measured = f"{result.get('measured_fps'):.2f}" if result.get("measured_fps") is not None else "n/a"
        self.results_table.setItem(row, 3, QTableWidgetItem(status))
        self.results_table.setItem(row, 4, QTableWidgetItem(measured))

    def _row_for_combo(self, pixel_format: str, width: int, height: int, fps: float) -> Optional[int]:
        for row, (pf, w, h, f) in enumerate(self._displayed_combos):
            if pf == pixel_format and w == width and h == height and f == fps:
                return row
        return None

    def _on_validate(self):
        combos = self._matching_combos()
        if not combos:
            QMessageBox.information(
                self, "Nothing to validate",
                "No declared combination matches the checked frame rate/resolution/"
                "pixel format values. Check at least one value in each of the three "
                "sections above.",
            )
            return

        cached = load_validated_combinations(sys.platform, self._device_key())
        tolerance = self.fps_tolerance_spin.value() / 100.0
        to_test, skipped_passed = _combos_needing_validation(combos, cached, tolerance)

        if not to_test:
            QMessageBox.information(
                self, "Nothing to validate",
                f"All {skipped_passed} selected combination(s) already passed validation "
                "in a previous run. Nothing to re-test.",
            )
            return

        self.btn_validate.setEnabled(False)
        self.btn_cancel.setVisible(True)
        self.btn_cancel.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.progress_label.setVisible(True)
        self.progress_bar.setRange(0, len(to_test))
        self.progress_bar.setValue(0)
        self.progress_label.setText(f"0% (0/{len(to_test)})")

        self._pending_results = {}
        thread = _ValidateCombinationsThread(
            combos=to_test,
            devnode=self._devnode,
            device_index=self._device_index,
            device_name=self._device_name,
            fps_tolerance=tolerance,
            warmup=self.warmup_spin.value(),
            duration=self.duration_spin.value(),
            parent=self,
        )
        self._thread = thread
        thread.combo_validated.connect(self._on_combo_validated)
        thread.progress.connect(self._on_progress)
        thread.cancelled.connect(self._on_thread_cancelled)
        thread.finished.connect(self._on_validation_finished)
        thread.failed.connect(self._on_validation_failed)
        self.validationStarted.emit()
        thread.start()

    def _on_cancel(self):
        if self._thread is not None:
            self.btn_cancel.setEnabled(False)
            self.progress_label.setText(self.progress_label.text() + " -- cancelling...")
            self._thread.request_cancel()

    def _on_thread_cancelled(self):
        self.progress_label.setText("Cancelled.")

    def _on_progress(self, i: int, total: int):
        self.progress_bar.setValue(i)
        pct = int(100 * i / total) if total else 0
        self.progress_label.setText(f"{pct}% ({i}/{total})")

    def _on_combo_validated(self, pixel_format: str, width: int, height: int, fps: int, result: dict):
        key = combination_key(pixel_format, width, height, fps)
        self._pending_results[key] = {
            "passed": result["passed"],
            "measured_fps": result["measured_fps"],
            "could_open": result["could_open"],
        }
        row = self._row_for_combo(pixel_format, width, height, fps)
        if row is not None:
            self._set_result_row(row, pixel_format, width, height, fps, result)

    def _on_validation_finished(self):
        # Whatever combinations completed before a cancel (or a natural
        # finish) are kept -- cancelling doesn't discard results already
        # measured, only skips the ones not yet reached.
        if self._pending_results:
            store_validated_combinations(sys.platform, self._device_key(), self._pending_results)
        self.btn_validate.setEnabled(True)
        self.btn_cancel.setVisible(False)
        self.progress_bar.setVisible(False)
        self.progress_label.setVisible(False)
        self._thread = None
        self.validationFinished.emit()

    def _on_validation_failed(self, msg: str):
        QMessageBox.critical(self, "Validation failed", msg)

    def closeEvent(self, event):
        # Closing the dialog (X button, Escape, or "Save and Close") while a
        # validation run is still in progress must not leave an orphaned
        # background thread still holding the device open -- request it stop
        # and wait for it to actually do so before letting the dialog close.
        if self._thread is not None and self._thread.isRunning():
            self._thread.request_cancel()
            self._thread.wait()
        super().closeEvent(event)
