# coding: utf-8
"""应用级半透明滚动条样式（主题自适应）。

深色主题用半透明白，浅色主题用半透明黑；
原生 Qt 控件使用 QSS；QFluentWidgets 的滚动条是自绘 QWidget，
需要通过其颜色 API 单独设置。应用事件过滤器会覆盖后续动态创建的
下拉列表、滚动区等控件。
"""
from PyQt6.QtCore import QEvent, QObject
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication, QWidget

from qfluentwidgets import ScrollBar as FluentScrollBar, isDarkTheme, qconfig


def _native_qss() -> str:
    if isDarkTheme():
        base, hover, pressed = 18, 145, 220
        color = "255, 255, 255"
    else:
        base, hover, pressed = 14, 120, 170
        color = "0, 0, 0"
    return f"""
QScrollBar:vertical {{
    background: rgba({color}, {base});
    border: none;
    border-radius: 6px;
    width: 12px;
    margin: 0px;
}}
QScrollBar::handle:vertical {{
    background: rgba({color}, {hover});
    border: none;
    border-radius: 2px;
    min-height: 28px;
    margin: 2px 4px;
}}
QScrollBar::handle:vertical:hover {{
    background: rgba({color}, {pressed});
    border-radius: 3px;
    margin: 2px 3px;
}}
QScrollBar::handle:vertical:pressed {{
    background: rgba({color}, {pressed});
    border-radius: 3px;
    margin: 2px 3px;
}}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {{
    height: 0px;
    border: none;
    background: transparent;
}}
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {{
    background: transparent;
}}
QScrollBar:horizontal {{
    background: rgba({color}, {base});
    border: none;
    border-radius: 6px;
    height: 12px;
    margin: 0px;
}}
QScrollBar::handle:horizontal {{
    background: rgba({color}, {hover});
    border: none;
    border-radius: 2px;
    min-width: 28px;
    margin: 4px 2px;
}}
QScrollBar::handle:horizontal:hover {{
    background: rgba({color}, {pressed});
    border-radius: 3px;
    margin: 3px 2px;
}}
QScrollBar::handle:horizontal:pressed {{
    background: rgba({color}, {pressed});
    border-radius: 3px;
    margin: 3px 2px;
}}
QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {{
    width: 0px;
    border: none;
    background: transparent;
}}
QScrollBar::add-page:horizontal,
QScrollBar::sub-page:horizontal {{
    background: transparent;
}}
"""

_STYLED_PROPERTY = "_themedScrollBar"

# 兼容别名：默认（深色）主题的原生滚动条 QSS
NATIVE_SCROLLBAR_QSS = _native_qss()


def _fluent_colors():
    if isDarkTheme():
        handle = QColor(255, 255, 255, 145)
        arrow = QColor(255, 255, 255, 180)
        groove = QColor(255, 255, 255, 24)
    else:
        handle = QColor(0, 0, 0, 120)
        arrow = QColor(0, 0, 0, 150)
        groove = QColor(0, 0, 0, 20)
    return handle, arrow, groove


def _style_fluent_bar(bar: FluentScrollBar, force: bool = False) -> None:
    if bar.property(_STYLED_PROPERTY) and not force:
        return
    handle, arrow, groove = _fluent_colors()
    bar.setHandleColor(handle, handle)
    bar.setArrowColor(arrow, arrow)
    bar.setGrooveColor(groove, groove)
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
    app.setStyleSheet(app.styleSheet() + "\n" + _native_qss())

    style_filter = _ScrollBarStyleFilter(app)
    app.installEventFilter(style_filter)
    app._whiteScrollBarStyleFilter = style_filter

    def _retheme() -> None:
        # 主题切换：刷新原生 QSS 与已有 Fluent 滚动条颜色
        app.setStyleSheet(app.styleSheet().split("QScrollBar:vertical")[0]
                          + "\n" + _native_qss())
        for widget in app.topLevelWidgets():
            for bar in widget.findChildren(FluentScrollBar):
                _style_fluent_bar(bar, force=True)

    qconfig.themeChangedFinished.connect(_retheme)

    for widget in app.topLevelWidgets():
        apply_white_scrollbars(widget)
