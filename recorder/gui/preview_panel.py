from __future__ import annotations

from typing import Dict, Iterable, List

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QGridLayout, QGroupBox, QLabel, QVBoxLayout,
                             QWidget)


class PreviewPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.labels: Dict[int, QLabel] = {}

        self.group = QGroupBox("Live Preview")
        self.grid = QGridLayout()
        self.group.setLayout(self.grid)
        layout = QVBoxLayout()
        layout.addWidget(self.group)
        self.setLayout(layout)

    def _make_label(self, cam_index: int) -> QLabel:
        lbl = QLabel(f"Camera {cam_index}: inactive")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setMinimumSize(360, 220)
        lbl.setStyleSheet("border: 1px solid #666; background: #111; color: #ddd;")
        return lbl

    def _rebuild_grid(self):
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item and item.widget():
                item.widget().setParent(None)

        cams: List[int] = sorted(self.labels.keys())
        cols = 2 if len(cams) > 1 else 1
        for idx, cam_index in enumerate(cams):
            r = idx // cols
            c = idx % cols
            self.grid.addWidget(self.labels[cam_index], r, c)

    def ensure_label(self, cam_index: int) -> QLabel:
        if cam_index not in self.labels:
            self.labels[cam_index] = self._make_label(cam_index)
            self._rebuild_grid()
        return self.labels[cam_index]

    def set_active_cameras(self, cam_indices: Iterable[int]):
        active = set(int(x) for x in cam_indices)
        removed = [k for k in self.labels if k not in active]
        for k in removed:
            lbl = self.labels.pop(k)
            lbl.setParent(None)
        for k in active:
            if k not in self.labels:
                self.labels[k] = self._make_label(k)
        self._rebuild_grid()
