# coding: utf-8
"""串口调试工具入口。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from qfluentwidgets import setTheme

from app.config import cfg, loadConfig, qconfig
from app.ui.main_window import MainWindow


def main():
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    loadConfig()
    setTheme(qconfig.get(cfg.themeMode))

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
