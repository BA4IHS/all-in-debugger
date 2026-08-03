# coding: utf-8
"""终端/日志视图的主题自适应样式。

原生 QPlainTextEdit 不随 qfluentwidgets 主题切换，
本模块提供按当前主题生成的 QSS，并监听 qconfig.themeChangedFinished 即时刷新。
"""
from PyQt6.QtCore import Qt

from qfluentwidgets import isDarkTheme, qconfig


def terminal_qss() -> str:
    """按当前主题返回终端视图 QSS。"""
    if isDarkTheme():
        bg, fg = "#1f1f1f", "#d8d8d8"
        border, sel_bg, sel_fg = "#3a3f47", "#2f5b8f", "#ffffff"
    else:
        bg, fg = "#ffffff", "#1f1f1f"
        border, sel_bg, sel_fg = "#d9d9d9", "#cce8ff", "#000000"
    return (
        f"QPlainTextEdit {{ background-color: {bg}; color: {fg}; "
        f"border: 1px solid {border}; }} "
        f"QPlainTextEdit::selection {{ background: {sel_bg}; color: {sel_fg}; }}"
    )


def setup_log_view(view) -> None:
    """日志/终端视图统一约定：主题自适应 + 自动换行、去除横向滚动条。

    日志类内容没有横向滚动的必要，长行自动折行（与 RTT Viewer 一致）。"""
    view.setStyleSheet(terminal_qss())
    view.setLineWrapMode(view.LineWrapMode.WidgetWidth)
    view.setHorizontalScrollBarPolicy(
        Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    qconfig.themeChangedFinished.connect(
        lambda: view.setStyleSheet(terminal_qss()))


# 兼容旧名：仅样式，不含换行/滚动条策略
apply_terminal_style = setup_log_view
