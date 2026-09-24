"""Small, generic Qt widget helpers shared across GUI dialogs."""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import (QCheckBox, QFormLayout, QGridLayout, QGroupBox,
                             QHBoxLayout, QMessageBox, QPushButton, QStyle,
                             QToolButton, QWidget)


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


def add_collapsible_form_rows(
    form: QFormLayout,
    title: str,
    rows: list[tuple[str, QWidget | QHBoxLayout]],
    collapsed: bool = True,
) -> QToolButton:
    """Append a clickable `title` header to `form`, followed by `rows` (label, field)
    that it shows/hides. The rows live in the same form as the rest of the tab,
    so their labels and fields stay aligned with the other controls."""
    toggle = QToolButton()
    toggle.setText(title)
    toggle.setCheckable(True)
    toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    toggle.setStyleSheet("QToolButton { border: none; font-weight: bold; }")
    form.addRow(toggle)

    first_row = form.rowCount()
    for label, field in rows:
        form.addRow(label, field)
    row_indices = range(first_row, first_row + len(rows))

    def _set_expanded(expanded: bool):
        toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        for row in row_indices:
            form.setRowVisible(row, expanded)

    toggle.toggled.connect(_set_expanded)
    toggle.setChecked(not collapsed)
    _set_expanded(not collapsed)
    return toggle
