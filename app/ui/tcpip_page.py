# coding: utf-8
"""TCP/IP 网络调试页：UDP / TCP Server / TCP Client 三模式。

- 左栏：连接卡（模式/本地/远程地址端口/连接按钮/状态/计数）、
  客户端卡（TCP Server 连接列表 + 指定目标发送）、工具卡（日志/收发开关/文件发送/外观）
- 右：复用 ReceivePanel / SendPanel；数据带来源前缀（TCP Server 多客户端）
- 窗口状态（模式/地址/端口）存入 data.json 的 tcpipWindow 键，启动恢复
"""
import time
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QColorDialog, QDialog, QFileDialog, QHBoxLayout, QHeaderView,
    QPushButton, QRadioButton, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, ComboBox, FluentIcon, FluentStyleSheet,
    InfoBar, LineEdit, PrimaryPushButton, PushButton, SingleDirectionScrollArea,
    SpinBox, SubtitleLabel, SwitchButton,
)

from app import tcpip_utils as tu
from app.config import APP_DIR, cfg, loadData, qconfig, saveData
from app.tcpip_worker import TcpipThread
from app.ui.receive_panel import ReceivePanel
from app.ui.send_panel import SendPanel

MODE_TO_KEY = {"TCP Server": "tcp_server",
               "TCP Client": "tcp_client",
               "UDP": "udp"}
WINDOW_KEY = "tcpipWindow"
_MODE_HINTS = {
    "TCP Server": "监听本地端口，等待客户端连接，可指定目标通信",
    "TCP Client": "连接远程主机，单连接",
    "UDP": "发送到目标主机（支持广播 255.255.255.255）",
}


def _labeled_switch(sw, text: str) -> None:
    sw.setOnText(text)
    sw.setOffText(text)


class _ColorButton(QPushButton):
    """色块按钮：点击弹出颜色选择。"""

    def __init__(self, color_str: str, parent=None):
        super().__init__(parent)
        self._color = QColor(color_str)
        self._update_style()
        self.clicked.connect(self._pick)

    def _pick(self):
        color = QColorDialog.getColor(self._color, self, "选择颜色")
        if color.isValid():
            self._color = color
            self._update_style()

    def _update_style(self):
        c = self._color.name()
        fg = "#000000" if self._color.lightness() > 140 else "#FFFFFF"
        self.setText(c.upper())
        self.setFixedWidth(96)
        self.setStyleSheet(
            f"QPushButton {{ background-color: {c}; color: {fg};"
            f" border: 1px solid #555555; border-radius: 4px; }}")

    def color(self) -> QColor:
        return self._color


class AppearanceDialog(QDialog):
    """接收区/发送区字号、文字色、背景色设置（写入 qconfig）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("收发外观设置")
        self.setMinimumWidth(380)
        v = QVBoxLayout(self)

        def section(title, size_box, text_btn, bg_btn):
            lay = QVBoxLayout()
            lay.setSpacing(6)
            row = QHBoxLayout()
            row.addWidget(BodyLabel("字号", self))
            row.addWidget(size_box)
            row.addWidget(BodyLabel("文字色", self))
            row.addWidget(text_btn)
            row.addWidget(BodyLabel("背景色", self))
            row.addWidget(bg_btn)
            row.addStretch(1)
            lay.addWidget(SubtitleLabel(title, self))
            lay.addLayout(row)
            return lay

        self.rxSize = SpinBox(self)
        self.rxSize.setRange(8, 32)
        self.rxSize.setValue(qconfig.get(cfg.rxFontSize))
        self.rxText = _ColorButton(qconfig.get(cfg.rxTextColor), self)
        self.rxBg = _ColorButton(qconfig.get(cfg.rxBgColor), self)
        v.addLayout(section("接收区", self.rxSize, self.rxText, self.rxBg))

        self.txSize = SpinBox(self)
        self.txSize.setRange(8, 32)
        self.txSize.setValue(qconfig.get(cfg.txFontSize))
        self.txText = _ColorButton(qconfig.get(cfg.txTextColor), self)
        self.txBg = _ColorButton(qconfig.get(cfg.txBgColor), self)
        v.addLayout(section("发送区", self.txSize, self.txText, self.txBg))

        btnRow = QHBoxLayout()
        btnRow.addStretch(1)
        okBtn = PrimaryPushButton("确定", self)
        okBtn.clicked.connect(self._apply_and_close)
        cancelBtn = PushButton("取消", self)
        cancelBtn.clicked.connect(self.reject)
        btnRow.addWidget(cancelBtn)
        btnRow.addWidget(okBtn)
        v.addLayout(btnRow)

    def _apply_and_close(self):
        qconfig.set(cfg.rxFontSize, self.rxSize.value())
        qconfig.set(cfg.rxTextColor, self.rxText.color().name())
        qconfig.set(cfg.rxBgColor, self.rxBg.color().name())
        qconfig.set(cfg.txFontSize, self.txSize.value())
        qconfig.set(cfg.txTextColor, self.txText.color().name())
        qconfig.set(cfg.txBgColor, self.txBg.color().name())
        qconfig.save()
        self.accept()


class SendFileDialog(QDialog):
    """文件发送设置：整包或分包（包大小 + 间隔毫秒）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("发送文件")
        self.setMinimumWidth(360)
        v = QVBoxLayout(self)

        self.splitRadio = QRadioButton("整包发送", self)
        self.splitRadio.setChecked(True)
        self.packRadio = QRadioButton("分包发送", self)
        v.addWidget(self.splitRadio)
        v.addWidget(self.packRadio)

        row = QHBoxLayout()
        row.addWidget(BodyLabel("包大小(B)", self))
        self.sizeSpin = SpinBox(self)
        self.sizeSpin.setRange(1, 65536)
        self.sizeSpin.setValue(1024)
        row.addWidget(self.sizeSpin)
        row.addWidget(BodyLabel("间隔(ms)", self))
        self.intervalSpin = SpinBox(self)
        self.intervalSpin.setRange(0, 10_000)
        self.intervalSpin.setValue(100)
        row.addWidget(self.intervalSpin)
        row.addStretch(1)
        v.addLayout(row)
        self.packRadio.toggled.connect(
            lambda on: self.sizeSpin.setEnabled(on))

        btnRow = QHBoxLayout()
        btnRow.addStretch(1)
        okBtn = PrimaryPushButton("发送", self)
        okBtn.clicked.connect(self.accept)
        cancelBtn = PushButton("取消", self)
        cancelBtn.clicked.connect(self.reject)
        btnRow.addWidget(cancelBtn)
        btnRow.addWidget(okBtn)
        v.addLayout(btnRow)

    def packs(self, data: bytes) -> tuple:
        """返回 (packs, interval_ms)。"""
        if not self.splitRadio.isChecked():
            return [data], 0
        size = self.sizeSpin.value()
        packs = [data[i:i + size] for i in range(0, len(data), size)]
        return packs, self.intervalSpin.value()


class TcpipPage(QWidget):

    def __init__(self, tp: TcpipThread, parent=None):
        super().__init__(parent)
        self.tp = tp
        self._running = False
        self._mode = ""
        self._logActive = False
        self._filePacks = []
        self._fileIdx = 0
        self._fileTimer = QTimer(self)
        self._fileTimer.timeout.connect(self._send_next_pack)

        self.receivePanel = ReceivePanel()
        self.sendPanel = SendPanel()

        scroll = SingleDirectionScrollArea(self)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(12)
        ll.addWidget(self._build_connect_card())
        self._clientsCard = self._build_clients_card()
        ll.addWidget(self._clientsCard)
        ll.addWidget(self._build_tools_card())
        ll.addStretch(1)
        scroll.setWidget(left)
        scroll.setFixedWidth(330)
        scroll.setWidgetResizable(True)
        scroll.enableTransparentBackground()
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

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
        layout.setContentsMargins(0, 40, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(scroll)
        layout.addWidget(splitter, 1)

        self._connect_signals()
        self._load_window()
        self._on_mode_changed(self.modeCombo.currentText())
        self._apply_appearance()

    # ── 左：连接卡 ─────────────────────────────────────────────

    def _build_connect_card(self) -> CardWidget:
        card = CardWidget()
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(8)
        v.addWidget(SubtitleLabel("以太网调试连接", card))

        self.modeCombo = ComboBox(card)
        self.modeCombo.addItems(list(MODE_TO_KEY))
        self.modeCombo.setCurrentText("TCP Client")
        self.modeCombo.currentTextChanged.connect(self._on_mode_changed)
        v.addWidget(self.modeCombo)

        self.modeHint = CaptionLabel("", card)
        self.modeHint.setWordWrap(True)
        v.addWidget(self.modeHint)

        v.addWidget(BodyLabel("本地地址", card))
        self.localHostEdit = LineEdit(card)
        self.localHostEdit.setPlaceholderText("留空 = 自动/所有网卡")
        v.addWidget(self.localHostEdit)
        
        lpr = QHBoxLayout()
        lpr.addWidget(BodyLabel("本地端口", card))
        self.localPortSpin = SpinBox(card)
        self.localPortSpin.setRange(0, 65535)
        self.localPortSpin.setValue(0)
        lpr.addStretch(1)
        v.addLayout(lpr)
        v.addWidget(self.localPortSpin)
        

        v.addWidget(BodyLabel("远程主机", card))
        self.remoteHostEdit = LineEdit(card)
        self.remoteHostEdit.setPlaceholderText("如 192.168.1.10")
        v.addWidget(self.remoteHostEdit)

        # x = self.localHostEdit.x()
        # y = self.localHostEdit.y()
        # print(f"相对父窗口坐标: ({x}, {y})")

        rpr = QHBoxLayout()
        rpr.addWidget(BodyLabel("远程端口", card))
        self.remotePortSpin = SpinBox(card)
        self.remotePortSpin.setRange(1, 65535)
        self.remotePortSpin.setValue(80)
        rpr.addStretch(1)
        v.addLayout(rpr)
        v.addWidget(self.remotePortSpin)
        

        self.connectBtn = PrimaryPushButton(FluentIcon.PLAY, "连接", card)
        self.connectBtn.clicked.connect(self._on_connect_clicked)
        v.addWidget(self.connectBtn)

        self.statusLabel = CaptionLabel("未连接", card)
        v.addWidget(self.statusLabel)
        self.countsLabel = CaptionLabel("收 0 · 发 0", card)
        v.addWidget(self.countsLabel)
        return card

    # ── 左：客户端卡（仅 TCP Server）───────────────────────────

    def _build_clients_card(self) -> CardWidget:
        card = CardWidget()
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(8)
        v.addWidget(SubtitleLabel("客户端", card))

        self.targetCombo = ComboBox(card)
        self.targetCombo.setToolTip("TCP Server 模式的发送目标")
        v.addWidget(BodyLabel("发送目标", card))
        v.addWidget(self.targetCombo)

        self.clientTable = QTableWidget(0, 1, card)
        self.clientTable.setHorizontalHeaderLabels(["地址"])
        self.clientTable.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.clientTable.verticalHeader().setVisible(False)
        self.clientTable.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self.clientTable.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self.clientTable.setMaximumHeight(220)
        v.addWidget(self.clientTable)

        br = QHBoxLayout()
        self.disconnectBtn = PushButton(FluentIcon.CANCEL, "断开选中", card)
        self.disconnectBtn.clicked.connect(self._on_disconnect_selected)
        self.disconnectAllBtn = PushButton(FluentIcon.CLOSE, "全部断开", card)
        self.disconnectAllBtn.clicked.connect(
            lambda _=False: self._close_clients(""))
        br.addWidget(self.disconnectBtn)
        br.addWidget(self.disconnectAllBtn)
        v.addLayout(br)
        return card

    # ── 左：工具卡 ─────────────────────────────────────────────

    def _build_tools_card(self) -> CardWidget:
        card = CardWidget()
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(8)
        v.addWidget(SubtitleLabel("工具", card))

        self.hexSwitch = SwitchButton(card)
        _labeled_switch(self.hexSwitch, "HEX 显示")
        self.hexSwitch.checkedChanged.connect(
            self.receivePanel.setHexDisplay)
        v.addWidget(self.hexSwitch)

        self.tsSwitch = SwitchButton(card)
        _labeled_switch(self.tsSwitch, "时间戳")
        self.tsSwitch.checkedChanged.connect(
            self.receivePanel.setTimestamp)
        v.addWidget(self.tsSwitch)

        self.pauseSwitch = SwitchButton(card)
        _labeled_switch(self.pauseSwitch, "暂停接收")
        self.pauseSwitch.checkedChanged.connect(
            self.receivePanel.setPaused)
        v.addWidget(self.pauseSwitch)

        self.logSwitch = SwitchButton(card)
        _labeled_switch(self.logSwitch, "日志存储(.bin)")
        self.logSwitch.checkedChanged.connect(self._on_log_toggled)
        v.addWidget(self.logSwitch)
        self.logPathLabel = CaptionLabel("", card)
        self.logPathLabel.setWordWrap(True)
        v.addWidget(self.logPathLabel)

        fbr = QHBoxLayout()
        self.fileBtn = PushButton(FluentIcon.DOCUMENT, "发送文件", card)
        self.fileBtn.setToolTip("整包或分包发送所选文件")
        self.fileBtn.clicked.connect(self._on_send_file)
        self.clearBtn = PushButton(FluentIcon.BROOM, "清空", card)
        self.clearBtn.clicked.connect(self.receivePanel.clearDisplay)
        fbr.addWidget(self.fileBtn)
        fbr.addWidget(self.clearBtn)
        v.addLayout(fbr)

        self.appearanceBtn = PushButton(FluentIcon.EDIT, "收发外观", card)
        self.appearanceBtn.setToolTip("接收区/发送区字号与颜色设置")
        self.appearanceBtn.clicked.connect(self._on_appearance)
        v.addWidget(self.appearanceBtn)
        return card

    # ── 信号接线 ────────────────────────────────────────────────

    def _connect_signals(self):
        rp, sp, tp = self.receivePanel, self.sendPanel, self.tp
        w = tp.worker

        w.dataReceived.connect(self._on_data)
        w.dataWritten.connect(rp.addTx)
        w.started.connect(self._on_started)
        w.startFailed.connect(self._on_start_failed)
        w.stopped.connect(self._on_stopped)
        w.clientsChanged.connect(self._on_clients)
        w.errorOccurred.connect(
            lambda msg: InfoBar.error(title="网络错误", content=msg,
                                      duration=5000, parent=rp))
        rp.countsChanged.connect(self._on_counts)

        sp.sendRequested.connect(self._on_send)
        rp.terminal.sendRequested.connect(self._on_send)
        rp.modeChanged.connect(
            lambda terminal: self._sendCard.setVisible(not terminal))

    # ── 模式切换 / 连接 ─────────────────────────────────────────

    def _on_mode_changed(self, mode: str):
        self._mode = MODE_TO_KEY.get(mode, "tcp_client")
        self.modeHint.setText(_MODE_HINTS.get(mode, ""))
        is_server = self._mode == "tcp_server"
        is_client = self._mode == "tcp_client"
        is_udp = self._mode == "udp"
        self.localHostEdit.setEnabled(not is_client)
        self.localPortSpin.setEnabled(not is_client)
        self.remoteHostEdit.setEnabled(not is_server)
        self.remotePortSpin.setEnabled(not is_server)
        self.localHostEdit.setPlaceholderText(
            "留空 = 所有网卡" if is_server else "留空 = 自动")
        self.clients_card_visible(is_server)
        self.targetCombo.setEnabled(is_server)

    def clients_card_visible(self, on: bool):
        """客户端卡在 TCP Server 模式下可见，其余模式隐藏。"""
        self._clientsCard.setVisible(on)

    def _on_connect_clicked(self):
        if self._running:
            self.tp.sigStop.emit()
            return
        cfg = {
            "mode": self._mode,
            "local_host": self.localHostEdit.text().strip(),
            "local_port": self.localPortSpin.value(),
            "remote_host": self.remoteHostEdit.text().strip(),
            "remote_port": self.remotePortSpin.value(),
        }
        if self._mode == "tcp_client" and not cfg["remote_host"]:
            InfoBar.warning(title="缺少远程地址",
                            content="TCP Client 需填写远程主机",
                            duration=3500, parent=self.receivePanel)
            return
        if self._mode == "udp" and not cfg["remote_host"]:
            InfoBar.warning(title="缺少目标主机",
                            content="UDP 模式需填写目标主机（支持广播）",
                            duration=3500, parent=self.receivePanel)
            return
        self.tp.sigStart.emit(cfg)

    def _on_started(self, info: dict):
        self._running = True
        self.modeCombo.setEnabled(False)
        self.connectBtn.setText("断开")
        self.connectBtn.setIcon(FluentIcon.CANCEL)
        if info.get("mode") == "tcp_server":
            self.statusLabel.setText(f"监听 {info.get('local')}")
        else:
            self.statusLabel.setText(
                f"已连接 {info.get('remote')}（本地 {info.get('local')}）")
        self._save_window()

    def _on_start_failed(self, msg: str):
        self._running = False
        InfoBar.error(title="连接失败", content=msg, duration=5000,
                      parent=self.receivePanel)

    def _on_stopped(self):
        self._running = False
        self.modeCombo.setEnabled(True)
        self.connectBtn.setText("连接")
        self.connectBtn.setIcon(FluentIcon.PLAY)
        self.statusLabel.setText("未连接")
        self._fileTimer.stop()
        self._filePacks = []
        self._clear_clients()
        self._save_window()

    def _on_counts(self, rx: int, tx: int):
        self.countsLabel.setText(f"收 {rx} · 发 {tx}")

    # ── 数据 / 发送 ─────────────────────────────────────────────

    def _on_data(self, data: bytes, ts: float, source: str):
        rp = self.receivePanel
        rp.bumpRx(len(data))
        if rp.isTerminal():
            rp.feed_term(data)
        else:
            rp.feed_log(data, ts, source)

    def _on_send(self, data: bytes):
        if self._mode == "tcp_server":
            target = self.targetCombo.currentData()
            if not target:
                InfoBar.warning(title="请选择发送目标",
                                content="TCP Server 模式需在客户端列表中选择目标",
                                duration=3500, parent=self.receivePanel)
                return
            self.tp.sigSend.emit({"data": data, "target": target})
        else:
            self.tp.sigSend.emit({"data": data, "target": ""})

    def _on_send_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择要发送的文件")
        if not path:
            return
        try:
            data = Path(path).read_bytes()
        except OSError as e:
            InfoBar.error(title="读取文件失败", content=str(e), duration=5000,
                          parent=self.receivePanel)
            return
        if not data:
            InfoBar.warning(title="文件为空", content=path, duration=3000,
                            parent=self.receivePanel)
            return
        dlg = SendFileDialog(self)
        if not dlg.exec():
            return
        packs, interval = dlg.packs(data)
        if len(packs) == 1:
            self._on_send(packs[0])
            InfoBar.success(title="文件已发送", content=path, duration=3000,
                            parent=self.receivePanel)
        else:
            self._filePacks = packs
            self._fileIdx = 0
            self._fileTimer.start(max(1, interval))
            self._send_next_pack()
            InfoBar.info(title=f"分包发送 {len(packs)} 包", content=path,
                         duration=3000, parent=self.receivePanel)

    def _send_next_pack(self):
        if self._fileIdx >= len(self._filePacks):
            self._fileTimer.stop()
            self._filePacks = []
            InfoBar.success(title="文件发送完成", duration=3000,
                            parent=self.receivePanel)
            return
        self._on_send(self._filePacks[self._fileIdx])
        self._fileIdx += 1

    # ── 客户端列表 ──────────────────────────────────────────────

    def _on_clients(self, clients: list):
        table = self.clientTable
        prev = self.targetCombo.currentData()
        table.setRowCount(len(clients))
        for row, addr in enumerate(clients):
            table.setItem(row, 0, QTableWidgetItem(addr))
        self.targetCombo.clear()
        if clients:
            self.targetCombo.addItem("全部客户端", "ALL")
        for addr in clients:
            self.targetCombo.addItem(addr, addr)
        if prev and prev != "ALL" and prev in clients:
            self.targetCombo.setCurrentText(prev)
        elif prev == "ALL" and clients:
            self.targetCombo.setCurrentText("全部客户端")

    def _clear_clients(self):
        self.clientTable.setRowCount(0)
        self.targetCombo.clear()

    def _on_disconnect_selected(self):
        row = self.clientTable.currentRow()
        if row < 0:
            InfoBar.warning(title="未选中客户端", duration=2500,
                            parent=self.receivePanel)
            return
        item = self.clientTable.item(row, 0)
        if item is not None:
            self._close_clients(item.text())

    def _close_clients(self, addr: str):
        self.tp.sigCloseClient.emit(addr)

    # ── 日志 / 外观 / 窗口保存 ──────────────────────────────────

    def _on_log_toggled(self, on: bool):
        if on:
            logDir = qconfig.get(cfg.logDir) or str(APP_DIR / "logs")
            try:
                Path(logDir).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                self.logSwitch.setChecked(False)
                InfoBar.error(title="无法创建日志目录", content=str(e),
                              duration=5000, parent=self.receivePanel)
                return
            ts = time.strftime("%Y%m%d_%H%M%S")
            path = str(Path(logDir) / f"tcpip_{ts}.bin")
            self.tp.sigSetLogFile.emit(path)
            self._logActive = True
            self.logPathLabel.setText(path)
            InfoBar.success(title="开始记录", content=path, duration=3000,
                            parent=self.receivePanel)
        elif self._logActive:
            self.tp.sigSetLogFile.emit("")
            self._logActive = False
            self.logPathLabel.setText("")

    def _on_appearance(self):
        dlg = AppearanceDialog(self)
        if dlg.exec():
            self._apply_appearance()

    def _apply_appearance(self):
        """应用收发区外观。

        - 字号始终通过 setFont 应用（主题 QSS 无字体规则，不冲突）。
        - 文字色/背景色仅在用户自定义过（值 != 默认值）时，在保留主题
          样式的基础上「追加」覆盖规则；未自定义时恢复主题 QSS。
        - 注意：绝不能用 setStyleSheet("") 清空——qfluentwidgets 控件
          （LineEdit/PlainTextEdit）的主题 QSS 挂在局部样式表上，清空会
          丢失主题边框/文字色/聚焦态，导致浅色主题下外观异常。
        """
        rx = self.receivePanel.view
        rx.setFont(QFont("Consolas", qconfig.get(cfg.rxFontSize)))
        tx = self.sendPanel.input
        tx.setFont(QFont("Consolas", qconfig.get(cfg.txFontSize)))

        def _apply_colors(widget, item_color, item_bg, selector):
            # 先恢复干净的主题 QSS（移除上次可能追加的自定义规则）
            FluentStyleSheet.LINE_EDIT.apply(widget)
            rules = []
            if qconfig.get(item_color) != item_color.defaultValue:
                rules.append(f"color: {qconfig.get(item_color)};")
            if qconfig.get(item_bg) != item_bg.defaultValue:
                rules.append(f"background-color: {qconfig.get(item_bg)};")
            if rules:
                # 保留主题 QSS，追加颜色规则（追加在后优先级更高）
                widget.setStyleSheet(
                    widget.styleSheet() + f"\n{selector} {{ {' '.join(rules)} }}")

        _apply_colors(rx, cfg.rxTextColor, cfg.rxBgColor, "QPlainTextEdit")
        _apply_colors(tx, cfg.txTextColor, cfg.txBgColor, "QLineEdit")

    def _load_window(self):
        w = loadData().get(WINDOW_KEY) or {}
        mode = w.get("mode")
        if mode in MODE_TO_KEY:
            self.modeCombo.setCurrentText(mode)
        self.localHostEdit.setText(str(w.get("local_host") or ""))
        try:
            self.localPortSpin.setValue(int(w.get("local_port") or 0))
        except (TypeError, ValueError):
            pass
        self.remoteHostEdit.setText(str(w.get("remote_host") or ""))
        try:
            self.remotePortSpin.setValue(int(w.get("remote_port") or 80))
        except (TypeError, ValueError):
            pass

    def _save_window(self):
        data = loadData()
        data[WINDOW_KEY] = {
            "mode": self.modeCombo.currentText(),
            "local_host": self.localHostEdit.text().strip(),
            "local_port": self.localPortSpin.value(),
            "remote_host": self.remoteHostEdit.text().strip(),
            "remote_port": self.remotePortSpin.value(),
        }
        saveData(data)

    # ── 对外 ────────────────────────────────────────────────────

    def shutdown(self):
        self.sendPanel.stopPeriodic()
        self._fileTimer.stop()
        self._filePacks = []
        if self._logActive:
            self.tp.sigSetLogFile.emit("")
            self._logActive = False
        if self._running:
            self.tp.sigStop.emit()
