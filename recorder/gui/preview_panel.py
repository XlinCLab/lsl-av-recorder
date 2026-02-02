from __future__ import annotations
from typing import Dict
from PyQt6.QtWidgets import QWidget, QLabel, QGridLayout, QGroupBox, QVBoxLayout
from PyQt6.QtCore import Qt

class PreviewPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.labels: Dict[int, QLabel] = {}

        group = QGroupBox("Live Preview")
        grid = QGridLayout()

        positions = [(0,0),(0,1),(1,0),(1,1)]
        for slot, (r,c) in enumerate(positions):
            lbl = QLabel(f"Slot {slot+1}: inactive")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setMinimumSize(360, 220)
            lbl.setStyleSheet("border: 1px solid #666; background: #111; color: #ddd;")
            grid.addWidget(lbl, r, c)
            self.labels[slot] = lbl

        group.setLayout(grid)
        layout = QVBoxLayout()
        layout.addWidget(group)
        self.setLayout(layout)
