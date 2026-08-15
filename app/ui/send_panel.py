# coding: utf-8
"""发送区：输入框 Enter 发送、HEX 切换、换行符、周期发送、历史记录。"""
from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from qfluentwidgets import (
    BodyLabel, ComboBox, FluentIcon, InfoBar, LineEdit, PrimaryPushButton,
    SpinBox, SwitchButton, TogglePushButton,
)

from app import serial_utils as su
from app.config import loadData, saveData

HISTORY_MAX = 50


def _labeled_switch(sw, text: str) -> None:
    """同时设置 on/off 文本，避免勾选态回退到默认 'On'。"""
    sw.setOnText(text)
    sw.setOffText(text)


class SendPanel(QWidget):
    sendRequested = pyqtSignal(bytes)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._codec = "UTF-8"
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._send)

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        # 历史记录
        historyRow = QHBoxLayout()
        self.historyCombo = ComboBox(self)
        self.historyCombo.setToolTip("发送历史（选中回填输入框）")
        self.historyCombo.currentTextChanged.connect(self._onHistorySelected)
        historyRow.addWidget(self.historyCombo, 1)
        v.addLayout(historyRow)

        # 输入 + 发送
        inputRow = QHBoxLayout()
        self.input = LineEdit(self)
        self.input.setPlaceholderText("输入要发送的内容，Enter 发送")
        self.input.setClearButtonEnabled(True)
        self.input.returnPressed.connect(self._send)

        self.hexBtn = TogglePushButton("HEX", self)
        self.hexBtn.setFixedWidth(64)

        self.newlineCombo = ComboBox(self)
        self.newlineCombo.addItems(list(su.NEWLINE_MAP))
        self.newlineCombo.setCurrentText("None")
        self.newlineCombo.setFixedWidth(90)
        self.newlineCombo.setToolTip("文本模式下追加的换行符（HEX 模式忽略）")

        self.sendBtn = PrimaryPushButton(FluentIcon.SEND, "发送", self)
        # 与 HID / DAP 页发送按钮统一固定宽度
        self.sendBtn.setFixedWidth(90)
        self.sendBtn.clicked.connect(self._send)

        inputRow.addWidget(self.input, 1)
        inputRow.addWidget(self.hexBtn)
        inputRow.addWidget(self.newlineCombo)
        inputRow.addWidget(self.sendBtn)
        v.addLayout(inputRow)

        # 周期发送
        periodRow = QHBoxLayout()
        self.periodSwitch = SwitchButton(self)
        _labeled_switch(self.periodSwitch, "周期发送")
        self.intervalSpin = SpinBox(self)
        self.intervalSpin.setRange(10, 86_400_000)
        self.intervalSpin.setValue(1000)
        # 为最多 8 位毫秒值及右侧上下调节按钮预留完整空间。
        self.intervalSpin.setFixedWidth(180)
        self.periodSwitch.checkedChanged.connect(self._onPeriodToggled)
        periodRow.addWidget(self.periodSwitch)
        periodRow.addWidget(self.intervalSpin)
        periodRow.addWidget(BodyLabel("ms", self))
        periodRow.addStretch(1)
        v.addLayout(periodRow)

        self._loadHistory()

    # ── 对外 API ────────────────────────────────────────────────

    def setCodec(self, codec: str):
        self._codec = codec

    def stopPeriodic(self):
        """关窗时调用：停周期定时器。"""
        self._timer.stop()

    # ── 内部 ────────────────────────────────────────────────────

    def _buildPayload(self):
        """构造发送字节；失败返回 (None, msg)。"""
        text = self.input.text()
        if self.hexBtn.isChecked():
            data, err = su.parse_hex_input(text)
            if data is None:
                return None, err
            return data, ""
        if not text:
            return None, "发送内容为空"
        data = su.encode_text(text, self._codec)
        data = su.append_newline(data, self.newlineCombo.currentText())
        return data, ""

    def _send(self):
        data, err = self._buildPayload()
        if data is None:
            InfoBar.warning(title="无法发送", content=err, duration=3000, parent=self)
            return
        self.sendRequested.emit(data)
        self._addHistory(self.input.text())
        self.input.clear()

    def _onPeriodToggled(self, on: bool):
        if on:
            data, err = self._buildPayload()
            if data is None:
                self.periodSwitch.setChecked(False)
                InfoBar.warning(title="无法周期发送", content=err,
                                duration=3000, parent=self)
                return
            self._timer.start(self.intervalSpin.value())
        else:
            self._timer.stop()

    def _onHistorySelected(self, text: str):
        if text:
            self.input.setText(text)

    def _addHistory(self, text: str):
        if not text.strip():
            return
        idx = self.historyCombo.findText(text)
        if idx >= 0:
            self.historyCombo.removeItem(idx)
        self.historyCombo.insertItem(0, text)
        while self.historyCombo.count() > HISTORY_MAX:
            self.historyCombo.removeItem(self.historyCombo.count() - 1)
        self._saveHistory()

    def _loadHistory(self):
        history = loadData().get("sendHistory", [])
        for item in history[:HISTORY_MAX]:
            if isinstance(item, str):
                self.historyCombo.addItem(item)

    def _saveHistory(self):
        data = loadData()
        data["sendHistory"] = [self.historyCombo.itemText(i)
                               for i in range(self.historyCombo.count())]
        saveData(data)
