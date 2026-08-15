# coding: utf-8
"""主调试页：左侧连接/接收选项面板 + 右侧 QSplitter（接收区 / 发送区）。

负责把 ConnectPanel / SendPanel 的信号接到 SerialThread 与 ReceivePanel。
"""
import time
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QHBoxLayout, QSplitter, QWidget

from qfluentwidgets import CardWidget, InfoBar, SingleDirectionScrollArea

from app import serial_utils as su
from app.config import APP_DIR, cfg, qconfig
from app.serial_worker import SerialThread
from app.ui.connect_panel import ConnectPanel
from app.ui.receive_panel import ReceivePanel
from app.ui.send_panel import SendPanel


class ConsolePage(QWidget):

    def __init__(self, st: SerialThread, parent=None):
        super().__init__(parent)
        self.st = st
        self._logActive = False

        self.connectPanel = ConnectPanel()
        self.receivePanel = ReceivePanel()
        self.sendPanel = SendPanel()
        # 左侧连接面板较窄，提示条统一显示在宽阔的接收区右上角。
        self.connectPanel.setInfoBarParent(self.receivePanel)

        # ── 布局 ────────────────────────────────────────────────
        scroll = SingleDirectionScrollArea(self)
        scroll.setWidget(self.connectPanel)
        scroll.setFixedWidth(330)
        scroll.setWidgetResizable(True)
        # 官方透明化：透出窗口主题底色，深色模式下半透明白卡片才显深色
        scroll.enableTransparentBackground()

        self._sendCard = CardWidget(self)
        sendLayout = QHBoxLayout(self._sendCard)
        sendLayout.setContentsMargins(12, 10, 12, 10)
        sendLayout.addWidget(self.sendPanel)

        splitter = QSplitter(Qt.Orientation.Vertical, self)
        splitter.addWidget(self.receivePanel)
        splitter.addWidget(self._sendCard)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 0)
        splitter.setChildrenCollapsible(False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 40, 0, 0)  # 顶部留白避开悬浮标题栏(48px)
        layout.setSpacing(12)
        layout.addWidget(scroll)
        layout.addWidget(splitter, 1)

        self.connectPanel.refreshPorts()
        self._connectSignals()

    # ── 信号接线 ────────────────────────────────────────────────

    def _connectSignals(self):
        cp, rp, sp, st = (self.connectPanel, self.receivePanel,
                          self.sendPanel, self.st)
        w = st.worker

        # worker → UI
        w.dataReceived.connect(self._onData)
        w.dataWritten.connect(rp.addTx)
        w.portOpened.connect(cp.setOpened)
        w.portClosed.connect(lambda _name: cp.setClosed())
        w.openFailed.connect(cp.setOpenFailed)
        w.errorOccurred.connect(
            lambda msg: InfoBar.error(title="串口错误", content=msg,
                                      duration=5000, parent=rp))
        rp.countsChanged.connect(cp.setCounts)

        # 连接面板 → worker / 接收区
        cp.openRequested.connect(st.sigOpen.emit)
        cp.closeRequested.connect(st.sigClose.emit)
        cp.dtrChanged.connect(st.sigSetDTR.emit)
        cp.rtsChanged.connect(st.sigSetRTS.emit)
        cp.codecChanged.connect(rp.setCodec)
        cp.codecChanged.connect(sp.setCodec)
        cp.hexDisplayChanged.connect(rp.setHexDisplay)
        cp.timestampChanged.connect(rp.setTimestamp)
        cp.pauseChanged.connect(rp.setPaused)
        cp.autoScrollChanged.connect(rp.setAutoScroll)
        cp.logToggled.connect(self._onLogToggled)
        cp.clearRequested.connect(rp.clearDisplay)
        cp.resetCountersRequested.connect(rp.resetCounters)

        # 发送面板 → worker
        sp.sendRequested.connect(st.sigWrite.emit)

        # 终端模式：键盘直发 + 切换时隐藏底部发送框
        rp.modeChanged.connect(self._onMode)
        rp.terminal.sendRequested.connect(st.sigWrite.emit)

    # ── 数据分流 / 模式切换 ─────────────────────────────────────

    def _onData(self, data: bytes, ts: float):
        rp = self.receivePanel
        rp.bumpRx(len(data))
        if rp.isTerminal():
            rp.feed_term(data)
        else:
            rp.feed_log(data, ts)

    def _onMode(self, terminal: bool):
        # 终端模式直接键盘输入，隐藏底部发送框；日志模式恢复
        self._sendCard.setVisible(not terminal)

    # ── 日志开关 ────────────────────────────────────────────────

    def _onLogToggled(self, on: bool):
        if on:
            port = self.connectPanel.currentPort() or "port"
            logDir = qconfig.get(cfg.logDir) or str(APP_DIR / "logs")
            try:
                Path(logDir).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                self.connectPanel.logSwitch.setChecked(False)
                InfoBar.error(title="无法创建日志目录", content=str(e),
                              duration=5000, parent=self.receivePanel)
                return
            ts = time.strftime("%Y%m%d_%H%M%S")
            path = str(Path(logDir) / f"serial_{port}_{ts}.bin")
            self.st.sigSetLogFile.emit(path)
            self._logActive = True
            InfoBar.success(title="开始记录", content=path,
                            duration=3000, parent=self.receivePanel)
        elif self._logActive:
            self.st.sigSetLogFile.emit("")
            self._logActive = False

    # ── 对外 ────────────────────────────────────────────────────

    def refreshPorts(self):
        self.connectPanel.refreshPorts(su.list_serial_ports())

    def shutdown(self):
        """关窗前调用：停周期发送与日志。"""
        self.sendPanel.stopPeriodic()
        if self._logActive:
            self.st.sigSetLogFile.emit("")
            self._logActive = False
