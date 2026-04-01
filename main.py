from __future__ import annotations

import faulthandler
import sys

from PyQt6.QtWidgets import QApplication

from recorder.gui.main_window import MainWindow


def main():
    faulthandler.enable(all_threads=True)
    app = QApplication(sys.argv)
    w = MainWindow()
    w.resize(1250, 850)
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
