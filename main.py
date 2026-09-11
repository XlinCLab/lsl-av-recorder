from __future__ import annotations

import faulthandler
import sys

from PyQt6.QtWidgets import QApplication

from recorder.gui.main_window import MainWindow


def main():
    faulthandler.enable(all_threads=True)
    app = QApplication(sys.argv)
    w = MainWindow()
    # Cap the initial application size to the actual usable screen area
    screen = app.primaryScreen()
    available = screen.availableGeometry() if screen else None
    width, height = 1250, 850
    if available is not None:
        width = min(width, available.width())
        height = min(height, available.height())
    w.resize(width, height)
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
