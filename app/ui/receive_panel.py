# coding: utf-8
"""接收区：日志视图(PlainTextEdit) 与 终端视图(QTerminalWidget) 二合一。

- 顶部工具条：模式切换（日志/终端）+ 终端专用控件（本地回显/回车符/清屏）
- 收/发计数提到面板层，两种模式下都正确累加
- 日志视图沿用 ReceiveController 的 30fps 批量渲染/截断防卡顿
"""
from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QTextCursor
from PyQt6.QtWidgets import QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget

from qfluentwidgets import (
    BodyLabel, ComboBox, FluentIcon, PlainTextEdit, PushButton, SwitchButton,
)

from app.serial_utils import decode_chunk, format_hex, make_decoder, timestamp_str
from app.ui.terminal_widget import QTerminalWidget

ENTER_MODES = {"CR": "\r", "CRLF": "\r\n", "LF": "\n"}


def _labeled_switch(sw, text: str) -> None:
    sw.setOnText(text)
    sw.setOffText(text)


class ReceiveController:
    """日志视图的批量渲染控制器（只渲染，不计数）。"""

    FLUSH_MS = 33

    def __init__(self, view: PlainTextEdit):
        self._view = view
        self._pending = bytearray()
        self._codec = "UTF-8"
        self._decoder = make_decoder(self._codec)
        self._hexDisplay = False
        self._timestamp = False
        self._paused = False
        self._autoScroll = True
        self._maxChars = 200_000

        self._timer = QTimer(view)
        self._timer.setInterval(self.FLUSH_MS)
        self._timer.timeout.connect(self._flush)
        self._timer.start()

    def feed(self, data: bytes, _ts: float):
        self._pending += data

    def setHexDisplay(self, on: bool):
        self._hexDisplay = bool(on)

    def setTimestamp(self, on: bool):
        self._timestamp = bool(on)

    def setPaused(self, on: bool):
        self._paused = bool(on)

    def setAutoScroll(self, on: bool):
        self._autoScroll = bool(on)

    def setCodec(self, codec: str):
        if codec != self._codec:
            self._codec = codec
            self._decoder = make_decoder(codec)

    def setMaxChars(self, n: int):
        self._maxChars = max(1000, int(n))

    def clear(self):
        self._pending.clear()
        self._decoder = make_decoder(self._codec)
        self._view.clear()

    def _flush(self):
        if not self._pending:
            return
        data = bytes(self._pending)
        self._pending.clear()
        if self._paused:
            return
        if self._hexDisplay:
            text = format_hex(data)
            if self._timestamp:
                text = timestamp_str() + " " + text
        else:
            text = decode_chunk(self._decoder, data)
            if not text:
                return
            if self._timestamp:
                text = timestamp_str() + " " + text
        self._view.appendPlainText(text)
        self._truncate()
        if self._autoScroll:
            sb = self._view.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _truncate(self):
        doc = self._view.document()
        total = doc.characterCount()
        if total <= self._maxChars:
            return
        cut = total - self._maxChars // 2
        cursor = QTextCursor(doc)
        cursor.setPosition(0)
        cursor.setPosition(cut, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        cursor.deleteChar()


class ReceivePanel(QWidget):
    countsChanged = pyqtSignal(int, int)
    modeChanged = pyqtSignal(bool)   # True = 终端模式

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rx = 0
        self._tx = 0

        # 日志视图
        self.view = PlainTextEdit(self)
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(30000)
        self.view.setFont(QFont("Consolas", 10))
        self._ctrl = ReceiveController(self.view)

        # 终端视图
        self.terminal = QTerminalWidget(self)

        self._stack = QStackedWidget(self)
        # 终端外套一层留白容器，使带描边的终端面板"浮"在右侧窗格里
        self._termWrap = QWidget(self)
        tw_lay = QVBoxLayout(self._termWrap)
        tw_lay.setContentsMargins(6, 6, 6, 6)
        tw_lay.addWidget(self.terminal)
        self._stack.addWidget(self.view)         # 0 日志
        self._stack.addWidget(self._termWrap)    # 1 终端

        toolbar = self._build_toolbar()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(toolbar)
        layout.addWidget(self._stack, 1)

        # 计数刷新
        self._countTimer = QTimer(self)
        self._countTimer.setInterval(500)
        self._countTimer.timeout.connect(self._emitCounts)
        self._countTimer.start()

        # 日志视图：手动上滚暂停自动滚动
        sb = self.view.verticalScrollBar()
        sb.sliderPressed.connect(self._onSliderPressed)
        sb.rangeChanged.connect(self._onRangeChanged)

    def _build_toolbar(self) -> QWidget:
        bar = QWidget(self)
        h = QHBoxLayout(bar)
        h.setContentsMargins(2, 2, 2, 2)
        h.setSpacing(8)

        self.modeSwitch = SwitchButton(bar)
        _labeled_switch(self.modeSwitch, "终端模式")
        self.modeSwitch.checkedChanged.connect(self.setMode)
        h.addWidget(self.modeSwitch)

        # 终端专用控件
        self._termBar = QWidget(bar)
        th = QHBoxLayout(self._termBar)
        th.setContentsMargins(0, 0, 0, 0)
        th.setSpacing(8)

        self.echoSwitch = SwitchButton(self._termBar)
        _labeled_switch(self.echoSwitch, "本地回显")
        self.echoSwitch.checkedChanged.connect(self.terminal.set_local_echo)
        th.addWidget(self.echoSwitch)

        th.addWidget(BodyLabel("回车", self._termBar))
        self.enterCombo = ComboBox(self._termBar)
        self.enterCombo.addItems(list(ENTER_MODES))
        self.enterCombo.setCurrentText("CR")
        self.enterCombo.currentTextChanged.connect(
            lambda t: self.terminal.set_enter_mode(ENTER_MODES.get(t, "\r")))
        th.addWidget(self.enterCombo)

        termClear = PushButton(FluentIcon.BROOM, "清屏", self._termBar)
        termClear.clicked.connect(lambda _=False: self.terminal.clear())
        th.addWidget(termClear)
        th.addStretch(1)

        h.addWidget(self._termBar)
        h.addStretch(1)
        self._termBar.setVisible(False)
        return bar

    # ── 对外 API ────────────────────────────────────────────────

    def isTerminal(self) -> bool:
        return self._stack.currentIndex() == 1

    def setMode(self, terminal: bool):
        self._stack.setCurrentIndex(1 if terminal else 0)
        self._termBar.setVisible(terminal)
        if terminal:
            self.terminal.setFocus()
        else:
            self.view.setFocus()
        self.modeChanged.emit(terminal)

    def feed_log(self, data: bytes, ts: float):
        self._ctrl.feed(data, ts)

    def feed_term(self, data: bytes):
        self.terminal.feed_bytes(data)

    def bumpRx(self, n: int):
        self._rx += n

    def addTx(self, n: int):
        self._tx += n

    def setCodec(self, codec: str):
        self._ctrl.setCodec(codec)
        self.terminal.set_codec(codec)

    def setMaxChars(self, n: int):
        self._ctrl.setMaxChars(n)

    def setHexDisplay(self, on: bool):
        self._ctrl.setHexDisplay(on)

    def setTimestamp(self, on: bool):
        self._ctrl.setTimestamp(on)

    def setPaused(self, on: bool):
        self._ctrl.setPaused(on)

    def setAutoScroll(self, on: bool):
        self._ctrl.setAutoScroll(on)

    def clearDisplay(self):
        self._ctrl.clear()
        self.terminal.clear()

    def resetCounters(self):
        self._rx = 0
        self._tx = 0
        self._emitCounts()

    # ── 内部 ────────────────────────────────────────────────────

    def _emitCounts(self):
        self.countsChanged.emit(self._rx, self._tx)

    def _onSliderPressed(self):
        sb = self.view.verticalScrollBar()
        if sb.value() < sb.maximum() - 1:
            self._ctrl.setAutoScroll(False)

    def _onRangeChanged(self):
        sb = self.view.verticalScrollBar()
        if sb.value() >= sb.maximum() - 1:
            self._ctrl.setAutoScroll(True)
