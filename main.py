from __future__ import annotations
import sys
from PyQt6.QtWidgets import QApplication
from recorder.gui.main_window import MainWindow

def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.resize(1250, 850)
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
