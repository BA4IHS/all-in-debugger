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
from PyQt6.QtGui import QFont, QIntValidator
from PyQt6.QtWidgets import (
    QFileDialog, QHBoxLayout, QPlainTextEdit, QRadioButton, QSplitter,
    QVBoxLayout, QWidget,
)

from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, CheckBox, ComboBox, EditableComboBox,
    FluentIcon, InfoBar, LineEdit, PrimaryPushButton, PushButton,
    SingleDirectionScrollArea, SubtitleLabel, ToolButton,
    isDarkTheme, qconfig, themeColor,
)

from app import serial_utils as su
from app import dap_core
from app import dap_rtt
from app import chip_profile
from app.dap_worker import DapThread
from app.ui.console_style import setup_log_view

MAX_CHARS = 400_000


class DapPage(QWidget):

    def __init__(self, dt: DapThread, parent=None):
        super().__init__(parent)
        self.dt = dt
        self._probes = []
        self._channels = []          # rttFound 后的通道列表
        self._max_up = 0             # 固件实际 UP 通道数（描述符个数）
        self._max_down = 0           # 固件实际 DOWN 通道数
        self._all_buf = ""           # "所有通道" 模式视图缓冲（带 [编号]-> 前缀）
        self._buffers = {}           # 通道编号 → 文本缓冲
        self._rx = 0
        self._tx = 0
        self._chip_items = []        # [(stem, name, data), ...] 芯片包列表
        self._active_chip = None     # 当前选中的芯片包数据
        self._clock_touched = False  # 用户手动改过 SWD 速度则不再被芯片包覆盖

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
        scroll.setFixedWidth(330)
        scroll.setWidgetResizable(True)
        scroll.enableTransparentBackground()
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # 与串口/网络/HID 页一致：接收+发送用可拖动 QSplitter
        splitter = QSplitter(Qt.Orientation.Vertical, self)
        splitter.addWidget(self._build_terminal_card())
        splitter.addWidget(self._build_input_card())
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 0)
        splitter.setChildrenCollapsible(False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 40, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(scroll)
        layout.addWidget(splitter, 1)

        self._connect_signals()
        self._set_connected(False)
        self.dllLabel.setText(dap_core.load_info())
        self._on_cb_mode()
        self._reload_chip_combo()  # 填充芯片/内核下拉（依赖全部控件已建）
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
        v.setSpacing(8)
        v.addWidget(SubtitleLabel("RTT连接设置", card))

        self.dllLabel = CaptionLabel("", card)
        self.dllLabel.setWordWrap(True)
        v.addWidget(self.dllLabel)

        prow = QHBoxLayout()
        self.probeCombo = ComboBox(card)
        self.probeCombo.setFixedWidth(240)  # 锁定宽度，避免长调试器名被裁切
        refresh = ToolButton(FluentIcon.UPDATE, card)
        refresh.setToolTip("枚举 CMSIS-DAP 调试器（在线验证，排除假冒设备）")
        refresh.clicked.connect(lambda _=False: self._enum_probes(notify=True))
        prow.addWidget(self.probeCombo)
        prow.addWidget(refresh)
        v.addWidget(BodyLabel("调试器", card))
        v.addLayout(prow)

        crow = QHBoxLayout()
        crow.addWidget(BodyLabel("SWD 速度", card))
        # 常见 SWD 时钟（kHz）：下拉可选，也可直接输入任意值
        self.clockCombo = EditableComboBox(card)
        self.clockCombo.addItems(
            ["100", "400", "1000", "2000", "4000", "8000",
             "10000", "12000", "20000", "50000"])
        self.clockCombo.setCurrentText("4000")
        self.clockCombo.setFixedWidth(110)
        self.clockCombo.setToolTip("SWD 时钟频率（kHz），下拉选常见值或直接输入")
        self.clockCombo.setValidator(QIntValidator(1, 50_000, self.clockCombo))
        crow.addWidget(self.clockCombo)
        crow.addWidget(CaptionLabel("kHz", card))
        crow.addStretch(1)
        v.addLayout(crow)

        krow = QHBoxLayout()
        self.kernelCombo = ComboBox(card)
        self.kernelCombo.setToolTip(dap_rtt.KERNELS[0]["desc"])
        self.kernelCombo.currentIndexChanged.connect(self._on_kernel_changed)
        self.kernelCombo.setFixedWidth(240)  # 锁定宽度，避免长芯片名被裁切
        chip_refresh = ToolButton(FluentIcon.UPDATE, card)
        chip_refresh.setToolTip(
            "重载芯片包文件（app/chip_profiles/*.json，可自定义添加）")
        chip_refresh.clicked.connect(
            lambda _=False: self._reload_chip_combo(notify=True))
        krow.addWidget(self.kernelCombo)
        krow.addWidget(chip_refresh)
        v.addWidget(BodyLabel("芯片 / 内核", card))
        v.addLayout(krow)

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

        self.regionHint = CaptionLabel("", card)
        self.regionHint.setWordWrap(True)
        v.addWidget(self.regionHint)

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
        self._update_region_hint()

    def _reload_chip_combo(self, notify=False):
        """填充「芯片 / 内核」下拉：内置内核 + 用户芯片包（可自定义添加）。"""
        from collections import Counter
        self._chip_items = chip_profile.list_profiles()
        prev = self._current_kernel_key()
        cnt = Counter(name for _s, name, _d in self._chip_items)
        self.kernelCombo.blockSignals(True)
        self.kernelCombo.clear()
        for k in dap_rtt.KERNELS:
            self.kernelCombo.addItem(f"内核：{k['name']}",
                                     userData=("kernel", k["key"]))
        for stem, name, _data in self._chip_items:
            label = (f"芯片：{name}  [{stem}]" if cnt[name] > 1
                     else f"芯片：{name}")
            self.kernelCombo.addItem(label, userData=("chip", stem))
        idx = self._find_combo_index(prev)
        self.kernelCombo.setCurrentIndex(idx)
        self.kernelCombo.blockSignals(False)
        if notify:
            InfoBar.success(title="芯片包已重载",
                            content=f"共 {len(self._chip_items)} 个芯片包",
                            duration=3000, parent=self)
        self._on_kernel_changed(idx)

    def _find_combo_index(self, target: tuple) -> int:
        for i in range(self.kernelCombo.count()):
            if self.kernelCombo.itemData(i) == target:
                return i
        return 0

    def _current_kernel_key(self) -> tuple:
        """返回 ("kernel"|"chip", key/stem)。"""
        data = self.kernelCombo.currentData()
        if isinstance(data, (tuple, list)) and len(data) == 2:
            return (str(data[0]), str(data[1]))
        return ("kernel", "auto")

    def _chip_config(self) -> dict:
        """当前选择（内核或芯片包）生效的配置。"""
        kind, key = self._current_kernel_key()
        cfg = {"kernel": "auto", "regions": None, "clock": 0, "cb_addr": 0}
        if kind == "kernel":
            cfg["kernel"] = dap_rtt.get_kernel(key)["key"]
            return cfg
        data = next((d for s, _n, d in self._chip_items if s == key), None)
        if not data:
            return cfg
        cfg["kernel"] = str(data.get("kernel") or "auto")
        regions = data.get("ram_regions")
        if isinstance(regions, list) and regions:
            cfg["regions"] = [[int(a), int(b)] for a, b in regions]
        cfg["clock"] = int(data.get("swd_speed_khz") or 0)
        cfg["cb_addr"] = int(data.get("cb_addr") or 0)
        return cfg

    def _on_kernel_changed(self, _idx: int):
        kind, key = self._current_kernel_key()
        self._active_chip = None
        if kind == "kernel":
            self.kernelCombo.setToolTip(dap_rtt.get_kernel(key)["desc"])
        else:
            data = next((d for s, _n, d in self._chip_items if s == key), None)
            if data is None:
                return
            self._active_chip = data
            self.kernelCombo.setToolTip(data.get("desc") or "")
        cfg = self._chip_config()
        # 芯片包带默认 SWD 速度：用户未手动改过速度时自动应用
        if cfg.get("clock") and not self._clock_touched:
            self.clockCombo.blockSignals(True)
            self.clockCombo.setCurrentText(str(cfg["clock"]))
            self.clockCombo.blockSignals(False)
        # 芯片包带固定控制块地址：预填并切到「固定地址」模式
        if kind == "chip" and cfg.get("cb_addr"):
            self.cbAddrEdit.setText(f"{cfg['cb_addr']:x}")
            if not self.cbAddrRadio.isChecked():
                self.cbAddrRadio.setChecked(True)
        # Cortex-A 且自动模式：提示无通用 RAM 布局
        k = dap_rtt.get_kernel(cfg["kernel"])
        if (k["family"] == "a" and self.cbAutoRadio.isChecked()
                and not cfg.get("regions")):
            InfoBar.warning(
                title="Cortex-A 提示",
                content="应用处理器无通用 RAM 布局，自动检测将直接失败，"
                        "请改用「固定地址」或「指定 RAM 区间」",
                duration=6000, parent=self)
        self._update_region_hint()

    def _update_region_hint(self):
        """RTT 控制块卡：自动模式下显示当前生效的扫描区间摘要。"""
        if not self.cbAutoRadio.isChecked():
            self.regionHint.setText("")
            return
        cfg = self._chip_config()
        k = dap_rtt.get_kernel(cfg["kernel"])
        if k["family"] == "a" and not cfg.get("regions"):
            self.regionHint.setText(
                "Cortex-A：无通用 RAM 布局，请手动指定地址/区间")
            return
        regions = (cfg.get("regions") or k.get("ram_regions")
                   or dap_rtt.DEFAULT_RAM_REGIONS)
        text = "；".join(f"{s:#x}-{e:#x}" for s, e in regions)
        self.regionHint.setText(f"自动模式将扫描：{text}")

    def _on_clock_text(self, _text: str):
        """用户手动改过 SWD 速度后，不再被芯片包默认速度覆盖。"""
        self._clock_touched = True

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
        # 与其他页发送按钮统一固定宽度
        self.sendBtn.setFixedWidth(90)
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
        self.clockCombo.currentTextChanged.connect(self._on_clock_text)
        self.probeCombo.currentIndexChanged.connect(self._update_probe_tooltip)

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
            tag = "v2" if p.get("transport") == "winusb" else "v1"
            self.probeCombo.addItem(
                f"{p['vid']:04X}:{p['pid']:04X}  "
                f"{p.get('product') or 'CMSIS-DAP'}  [{tag}]")
        self._update_probe_tooltip()
        if notify:
            InfoBar.success(title="枚举完成",
                            content=f"发现 {len(self._probes)} 个调试器",
                            duration=3000, parent=self)

    def _update_probe_tooltip(self, *_):
        """调试器下拉文本过长被裁时，悬停可看完整项文本。"""
        self.probeCombo.setToolTip(self.probeCombo.currentText())

    def _on_open(self):
        idx = self.probeCombo.currentIndex()
        if idx < 0 or idx >= len(self._probes):
            InfoBar.warning(title="未选择调试器", content="请先枚举并选择调试器",
                            duration=4000, parent=self)
            return
        p = self._probes[idx]
        try:
            clock_khz = int(self.clockCombo.currentText().strip() or 0)
        except ValueError:
            clock_khz = 0
        if not 1 <= clock_khz <= 50_000:
            InfoBar.warning(title="速度无效",
                            content="请输入 1~50000 之间的 SWD 速度（kHz）",
                            duration=4000, parent=self)
            return
        chip = self._chip_config()
        cfg = {
            "path": p["path"],
            "clock": clock_khz * 1000,
            "reset": self.resetCheck.isChecked(),
            "ram_start": 0, "ram_size": 0, "cb_addr": 0,
            "kernel": chip["kernel"],
            "regions": chip["regions"],
        }
        if self.cbAddrRadio.isChecked():
            cfg["cb_addr"] = self._parse_hex(self.cbAddrEdit.text())
            cfg["regions"] = None
            if not cfg["cb_addr"]:
                InfoBar.warning(title="地址无效",
                                content="请填写有效的控制块地址（十六进制）",
                                duration=4000, parent=self)
                return
        elif self.cbRegionRadio.isChecked():
            cfg["ram_start"] = self._parse_hex(self.ramStartEdit.text())
            cfg["ram_size"] = self._parse_hex(self.ramSizeEdit.text())
            cfg["regions"] = None
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
        self._max_up = rtt["max_up"]
        self._max_down = rtt["max_down"]
        self._buffers = {}
        self._all_buf = ""
        self._set_connected(True)
        # 通道选择固定 0-15 共 16 槽位；查看框顶部加 "所有通道"
        self._fill_channel_combo(self.chCombo, self._channels, "UP",
                                 all_option=True)
        self._fill_channel_combo(self.sendChCombo, self._channels, "DOWN")
        desc = (f"控制块 @ {rtt['addr']:#010x}   "
                f"UP×{rtt['max_up']} / DOWN×{rtt['max_down']}")
        self.chLabel.setText(desc)
        self.statusLabel.setText(
            f"RTT 已连接  控制块 @ {rtt['addr']:#010x}")
        self.dt.sigStartRtt.emit()

    @staticmethod
    def _fill_channel_combo(combo, channels: list, direction: str,
                            all_option: bool = False):
        """填充通道下拉：固定 0-15 共 16 槽位。

        已配置通道显示 "编号: 名称"，未配置显示 "编号: -"；
        userData 存编号字符串，收发/缓冲一律按编号索引。
        all_option=True 时顶部加 "所有通道"（userData="*"），
        仅查看框使用（发送必须选具体通道）。
        """
        names = {c["index"]: c["name"] for c in channels
                 if c["direction"] == direction}
        combo.blockSignals(True)
        combo.clear()
        if all_option:
            combo.addItem("所有通道", userData="*")
        for i in range(dap_rtt.MAX_CHANNELS):
            name = names.get(i)
            combo.addItem(f"{i}: {name}" if name else f"{i}: -",
                          userData=str(i))
        combo.blockSignals(False)

    def _set_connected(self, on: bool):
        self.openBtn.setEnabled(not on)
        self.closeBtn.setEnabled(on)
        self.resetBtn.setEnabled(on)
        self.sendBtn.setEnabled(on)
        if not on:
            self.statusLabel.setText("未连接")
            self.chLabel.setText("通道：-")
            self._channels = []
            self._max_up = 0
            self._max_down = 0
            self._all_buf = ""

    def _on_reset_done(self):
        self.statusLabel.setText("目标已硬件复位（RTT 连接保持）")

    # ── 通道视图 ───────────────────────────────────────────────

    def _current_channel(self) -> str:
        return str(self.chCombo.currentData() or "")

    def _on_channel_changed(self, _name: str):
        cur = self._current_channel()
        if cur == "*":
            # 所有通道：显示拼接视图（_all_buf 实时维护，为空则重建）
            text = self._all_buf or self._all_view_text()
        else:
            text = self._buffers.get(cur, "")
        self.rxView.setPlainText(text)
        sb = self.rxView.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _all_view_text(self) -> str:
        """按编号升序拼接各通道缓冲，每条带 [编号]-> 前缀。"""
        parts = []
        for i in range(dap_rtt.MAX_CHANNELS):
            buf = self._buffers.get(str(i), "")
            if buf:
                parts.append(f"[{i}]-> {buf}")
        return "".join(parts)

    def _clear_view(self):
        if self._current_channel() == "*":
            self._all_buf = ""
            for i in range(dap_rtt.MAX_CHANNELS):
                self._buffers.pop(str(i), None)
        else:
            self._buffers[self._current_channel()] = ""
        self.rxView.clear()
        # 清屏同时清零收发统计（右上角 RX/TX 计数）
        self._rx = 0
        self._tx = 0
        self._update_counts()

    def _save_log(self):
        cur = self._current_channel()
        if cur == "*":
            text = self._all_buf or self._all_view_text()
            tag = "all"
        else:
            text = self._buffers.get(cur, "")
            tag = cur
        if not text:
            InfoBar.info(title="无内容", content="当前通道日志为空",
                         duration=3000, parent=self)
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "保存 RTT 日志", f"rtt_{tag}_{time.strftime('%Y%m%d_%H%M%S')}.log",
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
        cur = self._current_channel()
        # 显示文本：所有通道模式每条数据加 [编号]-> 前缀标识来源
        disp = text
        if cur == "*":
            disp = f"[{ch_name}]-> {text}"
            self._all_buf += disp
            if len(self._all_buf) > MAX_CHARS:
                self._all_buf = self._all_buf[len(self._all_buf) - MAX_CHARS // 2:]
        elif ch_name != cur:
            return
        at_bottom = self.rxView.verticalScrollBar().value() >= \
            self.rxView.verticalScrollBar().maximum() - 2
        self.rxView.moveCursor(self.rxView.textCursor().MoveOperation.End)
        self.rxView.insertPlainText(disp)
        if len(self.rxView.toPlainText()) > MAX_CHARS:
            self.rxView.setPlainText(self._all_buf if cur == "*" else buf)
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
        return str(self.sendChCombo.currentData() or "")

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
        if not ch or ch == "*":   # 所有通道模式无单一归属，跳过回显
            return
        buf = self._buffers.get(ch, "") + text
        self._buffers[ch] = buf
        if ch == self._current_channel():
            self.rxView.setPlainText(buf)
            sb = self.rxView.verticalScrollBar()
            sb.setValue(sb.maximum())

    def shutdown(self):
        pass
