# coding: utf-8
"""左侧面板：连接配置卡片（串口参数/DTR/RTS/打开关闭/状态）+ 接收选项卡片。"""
from typing import List, Tuple

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QFormLayout, QHBoxLayout, QSpacerItem, QSizePolicy, QVBoxLayout, QWidget,
)

from qfluentwidgets import (
    BodyLabel, CardWidget, CheckBox, ComboBox, EditableComboBox, FluentIcon,
    InfoBadge, InfoBar, PrimaryPushButton, PushButton, SubtitleLabel,
    SwitchButton, ToolButton,
)

from app import serial_utils as su


def _labeled_switch(sw: SwitchButton, text: str) -> None:
    """SwitchButton 的 setText 只设 offText，勾选态会回退到 onText 默认值 'On'；
    这里同时设置 on/off 文本，保证开关两态文字一致。"""
    sw.setOnText(text)
    sw.setOffText(text)


class ConnectPanel(QWidget):
    # 对外信号（由 console_page 连接）
    openRequested = pyqtSignal(dict)
    closeRequested = pyqtSignal()
    dtrChanged = pyqtSignal(bool)
    rtsChanged = pyqtSignal(bool)
    codecChanged = pyqtSignal(str)
    hexDisplayChanged = pyqtSignal(bool)
    timestampChanged = pyqtSignal(bool)
    pauseChanged = pyqtSignal(bool)
    autoScrollChanged = pyqtSignal(bool)
    logToggled = pyqtSignal(bool)
    clearRequested = pyqtSignal()
    resetCountersRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._infoBarParent = None
        self._opened = False
        self._badge = None
        self._portDevices: list = []      # 有序真实端口名
        self._portLabel: dict = {}        # 真实端口名 -> 下拉显示文本

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self._buildConnectCard())
        layout.addWidget(self._buildReceiveCard())
        layout.addSpacerItem(QSpacerItem(0, 0, QSizePolicy.Policy.Minimum,
                                         QSizePolicy.Policy.Expanding))

    # ── 卡片①：连接 ─────────────────────────────────────────────

    def _buildConnectCard(self) -> CardWidget:
        card = CardWidget(self)
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 14, 16, 14)
        v.addWidget(SubtitleLabel("连接", card))

        form = QFormLayout()
        form.setSpacing(10)

        # 端口 + 刷新
        self.portCombo = ComboBox(card)
        self.portCombo.setMinimumWidth(150)
        refreshBtn = ToolButton(FluentIcon.UPDATE, card)
        refreshBtn.setToolTip("刷新端口列表")
        refreshBtn.clicked.connect(lambda _checked=False: self.refreshPorts())
        portRow = QHBoxLayout()
        portRow.addWidget(self.portCombo, 1)
        portRow.addWidget(refreshBtn)
        form.addRow(BodyLabel("端口", card), portRow)

        # 波特率（可编辑，支持自定义）
        self.baudCombo = EditableComboBox(card)
        self.baudCombo.addItems(su.BAUDRATES)
        self.baudCombo.setCurrentText("9600")
        form.addRow(BodyLabel("波特率", card), self.baudCombo)

        self.dataCombo = ComboBox(card)
        self.dataCombo.addItems(su.DATABITS)
        self.dataCombo.setCurrentText("8")
        form.addRow(BodyLabel("数据位", card), self.dataCombo)

        self.stopCombo = ComboBox(card)
        self.stopCombo.addItems(list(su.STOPBIT_MAP))
        form.addRow(BodyLabel("停止位", card), self.stopCombo)

        self.parityCombo = ComboBox(card)
        self.parityCombo.addItems(list(su.PARITY_MAP))
        form.addRow(BodyLabel("校验位", card), self.parityCombo)

        self.flowCombo = ComboBox(card)
        self.flowCombo.addItems(list(su.FLOWCONTROL_MAP))
        form.addRow(BodyLabel("流控", card), self.flowCombo)

        sigRow = QHBoxLayout()
        self.dtrCheck = CheckBox("DTR", card)
        self.dtrCheck.setChecked(True)
        self.rtsCheck = CheckBox("RTS", card)
        self.dtrCheck.stateChanged.connect(
            lambda s: self.dtrChanged.emit(self.dtrCheck.isChecked()))
        self.rtsCheck.stateChanged.connect(
            lambda s: self.rtsChanged.emit(self.rtsCheck.isChecked()))
        sigRow.addWidget(self.dtrCheck)
        sigRow.addWidget(self.rtsCheck)
        form.addRow(BodyLabel("信号", card), sigRow)

        v.addLayout(form)

        self.openBtn = PrimaryPushButton("打开串口", card)
        self.openBtn.clicked.connect(lambda _checked=False: self._onOpenClicked())
        v.addWidget(self.openBtn)

        statusRow = QHBoxLayout()
        self._statusLabel = BodyLabel("状态", card)
        statusRow.addWidget(self._statusLabel)
        self._badgeBox = QHBoxLayout()
        statusRow.addLayout(self._badgeBox)
        statusRow.addStretch(1)
        v.addLayout(statusRow)
        self._setBadge("attention", "未连接")

        self._paramWidgets = [
            self.portCombo, self.baudCombo, self.dataCombo,
            self.stopCombo, self.parityCombo, self.flowCombo,
        ]
        return card

    # ── 卡片②：接收选项 ─────────────────────────────────────────

    def _buildReceiveCard(self) -> CardWidget:
        card = CardWidget(self)
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(8)
        v.addWidget(SubtitleLabel("接收选项", card))

        codecRow = QHBoxLayout()
        self.codecCombo = ComboBox(card)
        self.codecCombo.addItems(su.CODECS)
        self.codecCombo.currentTextChanged.connect(self.codecChanged.emit)
        codecRow.addWidget(BodyLabel("编码", card))
        codecRow.addWidget(self.codecCombo, 1)
        v.addLayout(codecRow)

        self.hexSwitch = SwitchButton(card)
        _labeled_switch(self.hexSwitch, "HEX 显示")
        self.hexSwitch.checkedChanged.connect(self.hexDisplayChanged.emit)
        v.addWidget(self.hexSwitch)

        self.tsSwitch = SwitchButton(card)
        _labeled_switch(self.tsSwitch, "时间戳")
        self.tsSwitch.checkedChanged.connect(self.timestampChanged.emit)
        v.addWidget(self.tsSwitch)

        self.pauseSwitch = SwitchButton(card)
        _labeled_switch(self.pauseSwitch, "暂停显示")
        self.pauseSwitch.checkedChanged.connect(self.pauseChanged.emit)
        v.addWidget(self.pauseSwitch)

        self.scrollSwitch = SwitchButton(card)
        _labeled_switch(self.scrollSwitch, "自动滚动")
        self.scrollSwitch.setChecked(True)
        self.scrollSwitch.checkedChanged.connect(self.autoScrollChanged.emit)
        v.addWidget(self.scrollSwitch)

        self.logSwitch = SwitchButton(card)
        _labeled_switch(self.logSwitch, "保存原始日志")
        self.logSwitch.checkedChanged.connect(self.logToggled.emit)
        v.addWidget(self.logSwitch)

        btnRow = QHBoxLayout()
        clearBtn = PushButton(FluentIcon.BROOM, "清屏", card)
        clearBtn.clicked.connect(lambda _checked=False: self.clearRequested.emit())
        self.resetBtn = ToolButton(FluentIcon.ROTATE, card)
        self.resetBtn.setToolTip("清零计数")
        self.resetBtn.clicked.connect(
            lambda _checked=False: self.resetCountersRequested.emit())
        btnRow.addWidget(clearBtn, 1)
        btnRow.addWidget(self.resetBtn)
        v.addLayout(btnRow)

        self.countLabel = BodyLabel("RX: 0 B   TX: 0 B", card)
        v.addWidget(self.countLabel)
        return card

    # ── 对外 API ────────────────────────────────────────────────

    def refreshPorts(self, ports: List[Tuple[str, str]] = None):
        if ports is None:
            ports = su.list_serial_ports()
        devices = [p[0] for p in ports]
        if devices == self._portDevices:
            return
        selected = self.currentPort()
        self._portDevices = devices
        self._portLabel = {d: su.format_port_label(d, desc) for d, desc in ports}

        self.portCombo.blockSignals(True)
        self.portCombo.clear()
        for d in devices:
            self.portCombo.addItem(self._portLabel[d])
        if selected in self._portLabel:
            self.portCombo.setCurrentText(self._portLabel[selected])
        self.portCombo.blockSignals(False)

    def currentPort(self) -> str:
        """当前选中的真实端口名（不含设备描述）。"""
        label = self.portCombo.currentText()
        for d, l in self._portLabel.items():
            if l == label:
                return d
        return label.strip()

    def setOpened(self, port: str):
        self._opened = True
        self.openBtn.setText("关闭串口")
        for w in self._paramWidgets:
            w.setEnabled(False)
        self._setBadge("success", f"已连接 {port}")

    def setClosed(self):
        self._opened = False
        self.openBtn.setText("打开串口")
        for w in self._paramWidgets:
            w.setEnabled(True)
        self._setBadge("attention", "未连接")

    def setOpenFailed(self, msg: str):
        self.setClosed()
        self._setBadge("error", "打开失败")
        InfoBar.error(
            title="无法打开串口", content=msg, duration=5000,
            parent=self._infoBarParent or self)

    def setInfoBarParent(self, parent: QWidget):
        """指定提示条锚点，避免在窄连接面板内显示时溢出裁切。"""
        self._infoBarParent = parent

    def setCounts(self, rx: int, tx: int):
        self.countLabel.setText(f"RX: {su.fmt_bytes(rx)}   TX: {su.fmt_bytes(tx)}")

    def isHexSend(self) -> bool:
        return self.hexSwitch.isChecked()

    # ── 内部 ────────────────────────────────────────────────────

    def _onOpenClicked(self):
        if self._opened:
            self.closeRequested.emit()
            return
        port = self.currentPort()
        if not port:
            self._setBadge("error", "未选择端口")
            InfoBar.warning(
                title="提示", content="请先选择端口", duration=3000,
                parent=self._infoBarParent or self)
            return
        try:
            cfg = su.build_open_config(
                port,
                self.baudCombo.currentText().strip(),
                self.dataCombo.currentText(),
                self.stopCombo.currentText(),
                self.parityCombo.currentText(),
                self.flowCombo.currentText(),
                self.dtrCheck.isChecked(),
                self.rtsCheck.isChecked(),
            )
        except ValueError as e:
            self._setBadge("error", "参数错误")
            InfoBar.error(
                title="参数错误", content=str(e), duration=5000,
                parent=self._infoBarParent or self)
            return
        self.openRequested.emit(cfg)

    def _setBadge(self, kind: str, text: str):
        if self._badge is not None:
            self._badgeBox.removeWidget(self._badge)
            self._badge.deleteLater()
            self._badge = None
        maker = {
            "success": InfoBadge.success,
            "error": InfoBadge.error,
            "attention": InfoBadge.attension,
        }[kind]
        self._badge = maker(text, parent=self)
        self._badgeBox.addWidget(self._badge)
