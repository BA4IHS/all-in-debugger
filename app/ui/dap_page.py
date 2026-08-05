# coding: utf-8
"""DAP-link RTT 调试页：对标 SEGGER J-Link RTT Viewer。

布局：
- 左：连接设置（调试器/SWD 时钟/复位）+ RTT 控制块（自动检测/固定地址/RAM 区间）+ 显示选项
- 右：终端式控制台（通道选择/时间戳/清屏/保存日志）+ 底部输入行
      （回车发送/逐字符发送 + Echo 回显，下行通道选择）

传输经 hidapi.dll USB HID 直连 CMSIS-DAP 调试器，无需厂商 DLL。
"""
import time

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QFileDialog, QHBoxLayout, QPlainTextEdit, QRadioButton, QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, CheckBox, ComboBox, FluentIcon,
    InfoBar, LineEdit, PrimaryPushButton, PushButton,
    SingleDirectionScrollArea, SpinBox, SubtitleLabel, ToolButton,
    isDarkTheme, qconfig, themeColor,
)

from app import serial_utils as su
from app import dap_core
from app.dap_worker import DapThread
from app.ui.console_style import setup_log_view

MAX_CHARS = 400_000


class DapPage(QWidget):

    def __init__(self, dt: DapThread, parent=None):
        super().__init__(parent)
        self.dt = dt
        self._probes = []
        self._channels = []          # rttFound 后的通道列表
        self._down_channels = []     # DOWN 通道名列表
        self._buffers = {}           # 通道名 → 文本缓冲
        self._rx = 0
        self._tx = 0

        scroll = SingleDirectionScrollArea(self)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(12)
        ll.addWidget(self._build_connect_card())
        ll.addWidget(self._build_cb_card())
        ll.addWidget(self._build_display_card())
        ll.addStretch(1)
        scroll.setWidget(left)
        scroll.setFixedWidth(316)
        scroll.setWidgetResizable(True)
        scroll.enableTransparentBackground()
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        right = QVBoxLayout()
        right.setSpacing(8)
        right.addWidget(self._build_terminal_card(), 1)
        right.addWidget(self._build_input_card())

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 40, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(scroll)
        layout.addLayout(right, 1)

        self._connect_signals()
        self._set_connected(False)
        self.dllLabel.setText(dap_core.load_info())
        self._on_cb_mode()
        self._apply_radio_style()
        qconfig.themeChangedFinished.connect(self._apply_radio_style)

    @staticmethod
    def _radio_qss() -> str:
        """原生 QRadioButton 在深色主题下无主题适配，选中圆点会"消失"。

        手动补 QSS：未选中为灰环，选中为实心主题色圆点（与其他控件配色一致），
        文字随主题切换深浅色。注意 checked 不能写 border: none —— Qt 样式表中
        border-radius 依赖 border 参与绘制，去掉 border 后圆角失效，圆点会渲染成方形，
        且 border-radius 需等于外框尺寸（width + 2*border）的一半才是标准圆。"""
        border = "#5a5a5a" if isDarkTheme() else "#999999"
        color = themeColor().name()
        text = "#ffffff" if isDarkTheme() else "#1f1f1f"
        return (
            f"QRadioButton {{ background-color: transparent; color: {text}; }}"
            "QRadioButton::indicator { width: 14px; height: 14px; "
            f"border-radius: 8px; border: 2px solid {border}; "
            "background-color: transparent; }"
            "QRadioButton::indicator:hover, "
            "QRadioButton::indicator:pressed { "
            f"border-radius: 8px; border: 2px solid {border}; "
            "background-color: transparent; }"
            "QRadioButton::indicator:checked, "
            "QRadioButton::indicator:checked:hover, "
            "QRadioButton::indicator:checked:pressed { "
            f"border-radius: 8px; border: 2px solid {color}; "
            f"background-color: {color}; }}"
        )

    def _apply_radio_style(self):
        qss = self._radio_qss()
        for r in (self.cbAutoRadio, self.cbAddrRadio, self.cbRegionRadio,
                  self.modeEndRadio, self.modeCharRadio):
            r.setStyleSheet(qss)

    # ── 左：连接设置 ───────────────────────────────────────────

    def _build_connect_card(self) -> CardWidget:
        card = CardWidget()
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(10)
        v.addWidget(SubtitleLabel("连接设置", card))

        self.dllLabel = CaptionLabel("", card)
        self.dllLabel.setWordWrap(True)
        v.addWidget(self.dllLabel)

        prow = QHBoxLayout()
        self.probeCombo = ComboBox(card)
        refresh = ToolButton(FluentIcon.UPDATE, card)
        refresh.setToolTip("枚举 CMSIS-DAP 调试器（在线验证，排除假冒设备）")
        refresh.clicked.connect(lambda _=False: self._enum_probes(notify=True))
        prow.addWidget(self.probeCombo, 1)
        prow.addWidget(refresh)
        v.addWidget(BodyLabel("调试器", card))
        v.addLayout(prow)

        crow = QHBoxLayout()
        crow.addWidget(BodyLabel("SWD 速度", card))
        crow.addStretch(1)
        self.clockBox = SpinBox(card)
        self.clockBox.setRange(10, 50_000)
        self.clockBox.setValue(4000)
        self.clockBox.setSuffix(" kHz")
        self.clockBox.setMinimumWidth(90)
        crow.addWidget(self.clockBox)
        v.addLayout(crow)

        self.resetCheck = CheckBox("连接后硬件复位", card)
        v.addWidget(self.resetCheck)

        brow = QHBoxLayout()
        self.openBtn = PrimaryPushButton("连接", card)
        self.closeBtn = PushButton("断开", card)
        self.closeBtn.setEnabled(False)
        self.resetBtn = PushButton("复位", card)
        self.resetBtn.setEnabled(False)
        self.resetBtn.setToolTip("硬件复位目标（需连接 RESET 线），保持 SWD/RTT 连接")
        brow.addWidget(self.openBtn, 1)
        brow.addWidget(self.closeBtn, 1)
        brow.addWidget(self.resetBtn, 1)
        v.addLayout(brow)

        self.statusLabel = CaptionLabel("未连接", card)
        self.statusLabel.setWordWrap(True)
        v.addWidget(self.statusLabel)
        return card

    # ── 左：RTT 控制块 ─────────────────────────────────────────

    def _build_cb_card(self) -> CardWidget:
        card = CardWidget()
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(8)
        v.addWidget(SubtitleLabel("RTT 控制块", card))

        self.cbAutoRadio = QRadioButton("自动检测（默认 RAM 区间扫描）", card)
        self.cbAddrRadio = QRadioButton("固定地址", card)
        self.cbRegionRadio = QRadioButton("指定 RAM 区间", card)
        self.cbAutoRadio.setChecked(True)
        for r in (self.cbAutoRadio, self.cbAddrRadio, self.cbRegionRadio):
            r.toggled.connect(lambda _=False: self._on_cb_mode())
            v.addWidget(r)

        v.addWidget(CaptionLabel("控制块地址（十六进制）", card))
        self.cbAddrEdit = LineEdit(card)
        self.cbAddrEdit.setPlaceholderText("如 20001000")
        v.addWidget(self.cbAddrEdit)

        v.addWidget(CaptionLabel("RAM 起始 / 大小（十六进制）", card))
        r1 = QHBoxLayout()
        self.ramStartEdit = LineEdit(card)
        self.ramStartEdit.setPlaceholderText("如 20000000")
        self.ramSizeEdit = LineEdit(card)
        self.ramSizeEdit.setPlaceholderText("如 20000")
        r1.addWidget(self.ramStartEdit, 1)
        r1.addWidget(self.ramSizeEdit, 1)
        v.addLayout(r1)

        self.chLabel = CaptionLabel("通道：-", card)
        self.chLabel.setWordWrap(True)
        v.addWidget(self.chLabel)
        return card

    def _on_cb_mode(self):
        addr = self.cbAddrRadio.isChecked()
        region = self.cbRegionRadio.isChecked()
        self.cbAddrEdit.setVisible(addr)
        self.ramStartEdit.setVisible(region)
        self.ramSizeEdit.setVisible(region)

    # ── 左：显示选项 ───────────────────────────────────────────

    def _build_display_card(self) -> CardWidget:
        card = CardWidget()
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(8)
        v.addWidget(SubtitleLabel("显示选项", card))
        self.tsCheck = CheckBox("时间戳", card)
        self.crlfCheck = CheckBox("换行转换（LF → CRLF）", card)
        self.crlfCheck.setChecked(True)
        self.scrollCheck = CheckBox("自动滚动", card)
        self.scrollCheck.setChecked(True)
        v.addWidget(self.tsCheck)
        v.addWidget(self.crlfCheck)
        v.addWidget(self.scrollCheck)
        return card

    # ── 右：终端 ───────────────────────────────────────────────

    def _build_terminal_card(self) -> CardWidget:
        card = CardWidget()
        v = QVBoxLayout(card)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(6)

        bar = QHBoxLayout()
        bar.addWidget(BodyLabel("通道", card))
        self.chCombo = ComboBox(card)
        self.chCombo.setFixedWidth(200)
        bar.addWidget(self.chCombo)
        bar.addStretch(1)
        self.countLabel = CaptionLabel("RX: 0 B  TX: 0 B", card)
        bar.addWidget(self.countLabel)
        saveBtn = ToolButton(FluentIcon.SAVE, card)
        saveBtn.setToolTip("保存当前通道日志")
        saveBtn.clicked.connect(self._save_log)
        clearBtn = ToolButton(FluentIcon.DELETE, card)
        clearBtn.setToolTip("清屏")
        clearBtn.clicked.connect(self._clear_view)
        bar.addWidget(saveBtn)
        bar.addWidget(clearBtn)
        v.addLayout(bar)

        self.rxView = QPlainTextEdit(card)
        self.rxView.setReadOnly(True)
        self.rxView.setFont(QFont("Consolas", 10))
        setup_log_view(self.rxView)
        v.addWidget(self.rxView, 1)
        return card

    # ── 右：输入行 ─────────────────────────────────────────────

    def _build_input_card(self) -> CardWidget:
        card = CardWidget()
        h = QHBoxLayout(card)
        h.setContentsMargins(12, 10, 12, 10)
        h.setSpacing(8)
        self.sendChCombo = ComboBox(card)
        self.sendChCombo.setFixedWidth(130)
        self.sendChCombo.setToolTip("下行（写入）通道")
        self.sendEdit = LineEdit(card)
        self.sendEdit.setPlaceholderText("输入文本，按发送模式写入目标")
        self.sendEdit.textChanged.connect(self._on_input_changed)
        self.sendEdit.returnPressed.connect(self._on_send_end)
        self.modeEndRadio = QRadioButton("回车发送", card)
        self.modeEndRadio.setChecked(True)
        self.modeCharRadio = QRadioButton("逐字符发送", card)
        self.echoCheck = CheckBox("Echo", card)
        self.sendBtn = PrimaryPushButton("发送", card)
        self.sendBtn.setFixedWidth(80)
        self.sendBtn.clicked.connect(self._on_send_end)
        h.addWidget(self.sendChCombo)
        h.addWidget(self.sendEdit, 1)
        h.addWidget(self.modeEndRadio)
        h.addWidget(self.modeCharRadio)
        h.addWidget(self.echoCheck)
        h.addWidget(self.sendBtn)
        return card

    # ── 信号接线 ───────────────────────────────────────────────

    def _connect_signals(self):
        w = self.dt.worker
        w.probeOpened.connect(self._on_probe_opened)
        w.openFailed.connect(self._on_open_failed)
        w.probeClosed.connect(lambda: self._set_connected(False))
        w.rttFound.connect(self._on_rtt_found)
        w.dataReceived.connect(self._on_rx)
        w.dataWritten.connect(self._on_tx)
        w.errorOccurred.connect(
            lambda msg: InfoBar.error(title="DAP/RTT 错误", content=msg,
                                      duration=6000, parent=self.rxView))
        w.resetDone.connect(self._on_reset_done)
        self.openBtn.clicked.connect(self._on_open)
        self.closeBtn.clicked.connect(lambda _=False: self.dt.sigClose.emit())
        self.resetBtn.clicked.connect(lambda _=False: self.dt.sigReset.emit())
        self.chCombo.currentTextChanged.connect(self._on_channel_changed)

    # ── 枚举 / 连接 ────────────────────────────────────────────

    def _enum_probes(self, notify=False):
        try:
            # verify=True：逐个候选发 DAP_Info，排除假冒 0xFF00 的触摸屏等设备
            self._probes = dap_core.enum_probes(verify=True)
        except Exception as e:
            InfoBar.error(title="枚举失败", content=str(e),
                          duration=5000, parent=self)
            return
        self.probeCombo.clear()
        for p in self._probes:
            self.probeCombo.addItem(
                f"{p['vid']:04X}:{p['pid']:04X}  {p.get('product') or 'CMSIS-DAP'}")
        if notify:
            InfoBar.success(title="枚举完成",
                            content=f"发现 {len(self._probes)} 个调试器",
                            duration=3000, parent=self)

    def _on_open(self):
        idx = self.probeCombo.currentIndex()
        if idx < 0 or idx >= len(self._probes):
            InfoBar.warning(title="未选择调试器", content="请先枚举并选择调试器",
                            duration=4000, parent=self)
            return
        p = self._probes[idx]
        cfg = {
            "path": p["path"],
            "clock": self.clockBox.value() * 1000,
            "reset": self.resetCheck.isChecked(),
            "ram_start": 0, "ram_size": 0, "cb_addr": 0,
        }
        if self.cbAddrRadio.isChecked():
            cfg["cb_addr"] = self._parse_hex(self.cbAddrEdit.text())
            if not cfg["cb_addr"]:
                InfoBar.warning(title="地址无效",
                                content="请填写有效的控制块地址（十六进制）",
                                duration=4000, parent=self)
                return
        elif self.cbRegionRadio.isChecked():
            cfg["ram_start"] = self._parse_hex(self.ramStartEdit.text())
            cfg["ram_size"] = self._parse_hex(self.ramSizeEdit.text())
            if not (cfg["ram_start"] and cfg["ram_size"]):
                InfoBar.warning(title="区间无效",
                                content="请填写 RAM 起始地址与大小（十六进制）",
                                duration=4000, parent=self)
                return
        self.openBtn.setEnabled(False)
        self.dt.sigOpen.emit(cfg)

    @staticmethod
    def _parse_hex(text: str) -> int:
        text = (text or "").strip()
        if not text:
            return 0
        try:
            return int(text, 16)
        except ValueError:
            return 0

    def _on_probe_opened(self, idcode_str: str):
        self.statusLabel.setText(f"SWD 已连接  {idcode_str}\n正在查找 RTT 控制块…")

    def _on_open_failed(self, msg: str):
        self.openBtn.setEnabled(True)
        InfoBar.error(title="连接失败", content=msg, duration=6000, parent=self)

    def _on_rtt_found(self, rtt: dict):
        self._channels = rtt["channels"]
        self._buffers = {}
        self._set_connected(True)
        ups = [c["name"] for c in self._channels
               if c["direction"] == "UP" and c["size"]]
        self._down_channels = [c["name"] for c in self._channels
                               if c["direction"] == "DOWN" and c["size"]]
        self.chCombo.blockSignals(True)
        self.chCombo.clear()
        self.chCombo.addItems(ups or ["（无上行通道）"])
        self.chCombo.blockSignals(False)
        self.sendChCombo.clear()
        self.sendChCombo.addItems(self._down_channels or ["（无下行通道）"])
        desc = (f"控制块 @ {rtt['addr']:#010x}   "
                f"UP×{rtt['max_up']} / DOWN×{rtt['max_down']}")
        self.chLabel.setText(desc)
        self.statusLabel.setText(
            f"RTT 已连接  控制块 @ {rtt['addr']:#010x}")
        self.dt.sigStartRtt.emit()

    def _set_connected(self, on: bool):
        self.openBtn.setEnabled(not on)
        self.closeBtn.setEnabled(on)
        self.resetBtn.setEnabled(on)
        self.sendBtn.setEnabled(on)
        if not on:
            self.statusLabel.setText("未连接")
            self.chLabel.setText("通道：-")
            self._channels = []
            self._down_channels = []

    def _on_reset_done(self):
        self.statusLabel.setText("目标已硬件复位（RTT 连接保持）")

    # ── 通道视图 ───────────────────────────────────────────────

    def _current_channel(self) -> str:
        return self.chCombo.currentText()

    def _on_channel_changed(self, _name: str):
        self.rxView.setPlainText(self._buffers.get(self._current_channel(), ""))
        sb = self.rxView.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _clear_view(self):
        self._buffers[self._current_channel()] = ""
        self.rxView.clear()

    def _save_log(self):
        name = self._current_channel()
        text = self._buffers.get(name, "")
        if not text:
            InfoBar.info(title="无内容", content="当前通道日志为空",
                         duration=3000, parent=self)
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "保存 RTT 日志", f"rtt_{name}_{time.strftime('%Y%m%d_%H%M%S')}.log",
            "文本文件 (*.log *.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            InfoBar.success(title="已保存", content=path, duration=4000, parent=self)
        except OSError as e:
            InfoBar.error(title="保存失败", content=str(e),
                          duration=5000, parent=self)

    # ── 收发 ───────────────────────────────────────────────────

    def _on_rx(self, ch_name: str, data: bytes, ts: float):
        self._rx += len(data)
        self._update_counts()
        text = data.decode("utf-8", "replace")
        if self.crlfCheck.isChecked():
            text = text.replace("\r\n", "\n").replace("\n", "\r\n")
        if self.tsCheck.isChecked():
            text = f"[{time.strftime('%H:%M:%S', time.localtime(ts))}] " + text
        buf = self._buffers.get(ch_name, "")
        buf += text
        if len(buf) > MAX_CHARS:
            buf = buf[len(buf) - MAX_CHARS // 2:]
        self._buffers[ch_name] = buf
        if ch_name != self._current_channel():
            return
        at_bottom = self.rxView.verticalScrollBar().value() >= \
            self.rxView.verticalScrollBar().maximum() - 2
        self.rxView.moveCursor(self.rxView.textCursor().MoveOperation.End)
        self.rxView.insertPlainText(text)
        if len(self.rxView.toPlainText()) > MAX_CHARS:
            self.rxView.setPlainText(buf)
        if at_bottom and self.scrollCheck.isChecked():
            self.rxView.verticalScrollBar().setValue(
                self.rxView.verticalScrollBar().maximum())

    def _on_tx(self, ch_name: str, n: int):
        self._tx += int(n)
        self._update_counts()

    def _update_counts(self):
        self.countLabel.setText(
            f"RX: {su.fmt_bytes(self._rx)}  TX: {su.fmt_bytes(self._tx)}")

    # ── 输入行 ─────────────────────────────────────────────────

    def _send_channel(self) -> str:
        name = self.sendChCombo.currentText()
        return name if name in self._down_channels else ""

    def _on_input_changed(self, text: str):
        """逐字符发送：每次按键立即写入新增字符。"""
        if not self.modeCharRadio.isChecked() or not text:
            return
        ch = self._send_channel()
        if not ch:
            return
        self.dt.sigWrite.emit(ch, text[-1].encode("utf-8"))
        if self.echoCheck.isChecked():
            self._echo(text[-1])

    def _on_send_end(self):
        """回车/按钮发送：整行写入，可附加换行。"""
        ch = self._send_channel()
        text = self.sendEdit.text()
        if not ch or not text:
            return
        self.dt.sigWrite.emit(ch, (text + "\n").encode("utf-8"))
        if self.echoCheck.isChecked():
            self._echo(text + "\n")
        self.sendEdit.clear()

    def _echo(self, text: str):
        """本地回显：写入当前查看的上行通道缓冲（绿色）。"""
        ch = self._current_channel()
        if not ch:
            return
        buf = self._buffers.get(ch, "") + text
        self._buffers[ch] = buf
        if ch == self._current_channel():
            self.rxView.setPlainText(buf)
            sb = self.rxView.verticalScrollBar()
            sb.setValue(sb.maximum())

    def shutdown(self):
        pass
