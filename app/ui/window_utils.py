# coding: utf-8
"""顶层窗口通用位置工具。"""
from PyQt6.QtGui import QCursor, QGuiApplication


def center_window(window, screen=None):
    """将窗口置于当前显示器的可用区域中央。"""
    screen = screen or QGuiApplication.screenAt(QCursor.pos()) \
        or QGuiApplication.primaryScreen()
    if screen is None:
        return
    frame = window.frameGeometry()
    frame.moveCenter(screen.availableGeometry().center())
    window.move(frame.topLeft())
