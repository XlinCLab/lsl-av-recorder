from __future__ import annotations

from typing import Dict, Iterable, List

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QGridLayout, QGroupBox, QLabel, QVBoxLayout,
                             QWidget)


class PreviewPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        # Keyed by preview_manager.preview_key (device identity), not a raw numeric index
        self.labels: Dict[str, QLabel] = {}

        self.group = QGroupBox("Live Preview")
        self.grid = QGridLayout()
        self.group.setLayout(self.grid)
        layout = QVBoxLayout()
        layout.addWidget(self.group)
        self.setLayout(layout)

    def _make_label(self, key: str) -> QLabel:
        lbl = QLabel(f"Camera {key}: inactive")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setMinimumSize(360, 220)
        lbl.setStyleSheet("border: 1px solid #666; background: #111; color: #ddd;")
        return lbl

    def _rebuild_grid(self):
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item and item.widget():
                item.widget().setParent(None)

        cams: List[str] = sorted(self.labels.keys())
        cols = 2 if len(cams) > 1 else 1
        for idx, key in enumerate(cams):
            r = idx // cols
            c = idx % cols
            self.grid.addWidget(self.labels[key], r, c)

    def ensure_label(self, key: str) -> QLabel:
        if key not in self.labels:
            self.labels[key] = self._make_label(key)
            self._rebuild_grid()
        return self.labels[key]

    def set_active_cameras(self, keys: Iterable[str]):
        active = set(str(x) for x in keys)
        removed = [k for k in self.labels if k not in active]
        for k in removed:
            lbl = self.labels.pop(k)
            lbl.setParent(None)
        for k in active:
            if k not in self.labels:
                self.labels[k] = self._make_label(k)
        self._rebuild_grid()
