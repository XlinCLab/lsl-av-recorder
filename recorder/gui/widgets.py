"""Small, generic Qt widget helpers shared across GUI dialogs."""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from PyQt6.QtCore import QSize
from PyQt6.QtWidgets import (QCheckBox, QGridLayout, QGroupBox, QHBoxLayout,
                             QMessageBox, QPushButton, QStyle, QWidget)


def with_help_icon(
        owner: QWidget,
        widget: QWidget,
        title: str,
        text: str,
        icon_width: int = 12,
        icon_height: int = 12,
    ) -> QHBoxLayout:
    """Wrap `widget` with a trailing "?" icon button containing help/additional information."""
    btn_help = QPushButton()
    btn_help.setIcon(owner.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarContextHelpButton))
    btn_help.setIconSize(QSize(icon_width, icon_height))
    btn_help.setFixedSize(icon_width * 2, icon_height * 2)
    btn_help.clicked.connect(lambda: QMessageBox.information(owner, title, text))
    row = QHBoxLayout()
    row.addWidget(widget)
    row.addWidget(btn_help)
    return row

def make_checkbox_grid(
        values: List,
        formatter=str,
        columns: int = 4,
    ) -> Tuple[QWidget, Dict[Any, QCheckBox]]:
    """Generate a wrapping grid of checkboxes."""
    checkboxes: Dict[Any, QCheckBox] = {}
    grid = QGridLayout()
    for i, value in enumerate(values):
        cb = QCheckBox(formatter(value))
        checkboxes[value] = cb
        grid.addWidget(cb, i // columns, i % columns)
    box = QGroupBox()
    box.setLayout(grid)
    return box, checkboxes
