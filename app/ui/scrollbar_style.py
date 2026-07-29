# coding: utf-8
"""应用级半透明白色滚动条样式。

原生 Qt 控件使用 QSS；QFluentWidgets 的滚动条是自绘 QWidget，
需要通过其颜色 API 单独设置。应用事件过滤器会覆盖后续动态创建的
下拉列表、滚动区等控件。
"""
from PyQt6.QtCore import QEvent, QObject
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication, QWidget

from qfluentwidgets import ScrollBar as FluentScrollBar


NATIVE_SCROLLBAR_QSS = """
QScrollBar:vertical {
    background: rgba(255, 255, 255, 18);
    border: none;
    border-radius: 6px;
    width: 12px;
    margin: 0px;
}
QScrollBar::handle:vertical {
    background: rgba(255, 255, 255, 145);
    border: none;
    border-radius: 2px;
    min-height: 28px;
    margin: 2px 4px;
}
QScrollBar::handle:vertical:hover {
    background: rgba(255, 255, 255, 185);
    border-radius: 3px;
    margin: 2px 3px;
}
QScrollBar::handle:vertical:pressed {
    background: rgba(255, 255, 255, 220);
    border-radius: 3px;
    margin: 2px 3px;
}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {
    height: 0px;
    border: none;
    background: transparent;
}
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {
    background: transparent;
}
QScrollBar:horizontal {
    background: rgba(255, 255, 255, 18);
    border: none;
    border-radius: 6px;
    height: 12px;
    margin: 0px;
}
QScrollBar::handle:horizontal {
    background: rgba(255, 255, 255, 145);
    border: none;
    border-radius: 2px;
    min-width: 28px;
    margin: 4px 2px;
}
QScrollBar::handle:horizontal:hover {
    background: rgba(255, 255, 255, 185);
    border-radius: 3px;
    margin: 3px 2px;
}
QScrollBar::handle:horizontal:pressed {
    background: rgba(255, 255, 255, 220);
    border-radius: 3px;
    margin: 3px 2px;
}
QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {
    width: 0px;
    border: none;
    background: transparent;
}
QScrollBar::add-page:horizontal,
QScrollBar::sub-page:horizontal {
    background: transparent;
}
"""

_HANDLE = QColor(255, 255, 255, 145)
_ARROW = QColor(255, 255, 255, 180)
_GROOVE = QColor(255, 255, 255, 24)
_STYLED_PROPERTY = "_semiTransparentWhiteScrollBar"


def _style_fluent_bar(bar: FluentScrollBar) -> None:
    if bar.property(_STYLED_PROPERTY):
        return
    # 浅色和深色主题都保持同一半透明白色，切换主题后无需重新设置。
    bar.setHandleColor(_HANDLE, _HANDLE)
    bar.setArrowColor(_ARROW, _ARROW)
    bar.setGrooveColor(_GROOVE, _GROOVE)
    bar.setProperty(_STYLED_PROPERTY, True)
    bar.update()


def _style_object(obj: QObject) -> None:
    if isinstance(obj, FluentScrollBar):
        _style_fluent_bar(obj)

    # 某些 QFluentWidgets 控件通过 scrollDelegate 持有两根自绘滚动条；
    # 在控件本身 Polish/Show 时也主动覆盖，避免隐藏滚动条尚未收到事件。
    # 不同 QFluentWidgets 控件/版本同时存在 scrollDelegate 与历史拼写
    # scrollDelagate，两者都兼容。
    for attr in ("scrollDelegate", "scrollDelagate"):
        delegate = getattr(obj, attr, None)
        if delegate is None:
            continue
        for name in ("vScrollBar", "hScrollBar"):
            bar = getattr(delegate, name, None)
            if isinstance(bar, FluentScrollBar):
                _style_fluent_bar(bar)


def apply_white_scrollbars(root: QWidget) -> None:
    """立即设置 root 内已经创建的所有 Fluent 滚动条。"""
    _style_object(root)
    for bar in root.findChildren(FluentScrollBar):
        _style_fluent_bar(bar)


class _ScrollBarStyleFilter(QObject):
    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.Polish, QEvent.Type.Show):
            _style_object(obj)
        return False


def install_white_scrollbars(app: QApplication) -> None:
    """安装一次应用级样式，覆盖当前及未来创建的全部滚动条。"""
    if getattr(app, "_whiteScrollBarStyleFilter", None) is not None:
        return

    # QSS 自动覆盖全部原生 QScrollBar（包括终端和第三方控件内部实例）。
    app.setStyleSheet(app.styleSheet() + "\n" + NATIVE_SCROLLBAR_QSS)

    style_filter = _ScrollBarStyleFilter(app)
    app.installEventFilter(style_filter)
    app._whiteScrollBarStyleFilter = style_filter

    for widget in app.topLevelWidgets():
        apply_white_scrollbars(widget)
