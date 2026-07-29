# coding: utf-8
"""VSCode/Word 风格的小型检索条（浮在目标视图右上角）。

纯 UI：发出 textChanged / next / prev / caseChanged / close 信号；
具体查找与高亮由宿主视图（终端 / 日志）实现。
"""
from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QWidget

from qfluentwidgets import (
    BodyLabel, CardWidget, LineEdit, PushButton, TogglePushButton,
)


class FindBar(CardWidget):
    textChanged = pyqtSignal(str)
    nextRequested = pyqtSignal()
    prevRequested = pyqtSignal()
    caseChanged = pyqtSignal(bool)
    closeRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setVisible(False)
        self._right_margin = 10

        h = QHBoxLayout(self)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(6)

        self.input = LineEdit(self)
        self.input.setPlaceholderText("查找")
        self.input.setClearButtonEnabled(True)
        self.input.setFixedWidth(220)
        self.input.installEventFilter(self)
        self.input.textChanged.connect(self.textChanged.emit)
        self.input.returnPressed.connect(self.nextRequested.emit)
        h.addWidget(self.input)

        self.count = BodyLabel("0/0", self)
        self.count.setFixedWidth(56)
        h.addWidget(self.count)

        self.caseBtn = TogglePushButton("Aa", self)
        self.caseBtn.setFixedWidth(40)
        self.caseBtn.setToolTip("区分大小写")
        self.caseBtn.toggled.connect(self.caseChanged.emit)   # ToggleButton 用 toggled
        h.addWidget(self.caseBtn)

        self.prevBtn = PushButton("↑", self)
        self.prevBtn.setFixedSize(30, 28)
        self.prevBtn.setToolTip("上一个")
        self.prevBtn.clicked.connect(lambda _=False: self.prevRequested.emit())
        h.addWidget(self.prevBtn)

        self.nextBtn = PushButton("↓", self)
        self.nextBtn.setFixedSize(30, 28)
        self.nextBtn.setToolTip("下一个")
        self.nextBtn.clicked.connect(lambda _=False: self.nextRequested.emit())
        h.addWidget(self.nextBtn)

        self.closeBtn = PushButton("✕", self)
        self.closeBtn.setFixedSize(30, 28)
        self.closeBtn.setToolTip("关闭 (Esc)")
        self.closeBtn.clicked.connect(lambda _=False: self.closeRequested.emit())
        h.addWidget(self.closeBtn)

    # ── 对外 ────────────────────────────────────────────────────

    def open(self, text: str = ""):
        self.setVisible(True)
        if text:
            self.input.blockSignals(True)
            self.input.setText(text)
            self.input.blockSignals(False)
        # 关闭后再次打开时也要恢复上一次查询的匹配与计数。
        self.textChanged.emit(self.input.text())
        self.place()
        self.raise_()
        self.input.setFocus()
        self.input.selectAll()

    def place(self):
        p = self.parentWidget()
        if p is None:
            return
        self.adjustSize()
        w = self.sizeHint().width()
        self.move(max(0, p.width() - w - self._right_margin), 8)

    def set_right_margin(self, margin: int):
        """设置与宿主右边缘的间距，并立即更新位置。"""
        self._right_margin = max(0, int(margin))
        self.place()

    def set_count(self, cur: int, total: int):
        self.count.setText(f"{cur}/{total}" if total else "无")

    # ── Esc 关闭 ────────────────────────────────────────────────

    def eventFilter(self, obj, ev):
        # qfluentwidgets 在 LineEdit 构造期间可能触发父对象的事件过滤，
        # 此时 self.input 尚未完成赋值。
        if obj is getattr(self, "input", None) and ev.type() == QEvent.Type.KeyPress:
            if ev.key() == Qt.Key.Key_Escape:
                self.closeRequested.emit()
                return True
            if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and \
                    ev.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.prevRequested.emit()
                return True
            if ev.key() == Qt.Key.Key_F and \
                    ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.input.selectAll()
                return True
        return super().eventFilter(obj, ev)
