# coding: utf-8
"""小工具页：工具入口集合。

按键点击后弹出对应工具的独立子窗口（非模态 show()，不阻塞主进程
其他操作）。Phoenix 烧录为单例窗口（重复点击激活已开窗口）。后续
工具按同样模式在此挂载：新增按键 + 专用 _open_xxx 分支即可。
"""
from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from qfluentwidgets import (
    BodyLabel, CardWidget, PrimaryPushButton, SubtitleLabel,
)


class ToolsPage(QWidget):
    """小工具页：工具入口按键 → 独立子窗口。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._windows = []          # 已打开的子窗口（防 GC，关闭时移除）
        self.setObjectName("toolsInterface")

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 40, 24, 24)   # 顶部留白避开悬浮标题栏(48px)，与各页一致
        root.setSpacing(16)

        title = SubtitleLabel("小工具", self)
        root.addWidget(title)
        tip = BodyLabel(
            "Phoenix 烧录：经命令行调用全志 PhoenixConsole 的完整烧录窗口。",
            self)
        tip.setWordWrap(True)
        root.addWidget(tip)

        card = CardWidget(self)
        box = QVBoxLayout(card)
        box.setContentsMargins(16, 16, 16, 16)
        box.setSpacing(12)

        row = QHBoxLayout()
        row.setSpacing(12)
        btn = PrimaryPushButton("Phoenix 烧录", card)
        btn.clicked.connect(self._open_phoenix)
        row.addWidget(btn)
        row.addStretch(1)
        box.addLayout(row)

        root.addWidget(card)
        root.addStretch(1)

    def _open_phoenix(self):
        """打开 Phoenix 烧录窗口（单例：已开则激活，不重复创建）。"""
        from app.ui.phoenix_window import PhoenixWindow
        for win in self._windows:
            if isinstance(win, PhoenixWindow):
                win.raise_()
                win.activateWindow()
                return
        win = PhoenixWindow()
        self._windows.append(win)
        win.destroyed.connect(
            lambda _obj=win: self._windows.remove(win)
            if win in self._windows else None)
        win.show()
        win.raise_()
        win.activateWindow()

    def shutdown(self):
        """关闭所有已打开的子窗口（主窗口退出时调用）。"""
        for win in list(self._windows):
            win.close()
        self._windows.clear()
