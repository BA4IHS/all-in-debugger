# coding: utf-8
"""Modbus 调试页：对标 Modbus Poll（主站）。

- 连接：RTU / TCP + 从站地址
- 轮询定义：功能码 / 起始地址 / 数量 / 轮询间隔，连接后自动刷新数据表
- 数据表：多种显示格式（Unsigned/Signed/Hex/Float/ASCII），
  双击单元格可直接写入（寄存器 FC06 / 线圈 FC05）
- 通信监视：请求/响应 Trace 日志
"""
import struct

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QPlainTextEdit,
    QSplitter, QTableWidgetItem, QVBoxLayout, QWidget,
)

from qfluentwidgets import (
    Action, BodyLabel, CaptionLabel, CardWidget, ComboBox, FluentIcon,
    InfoBar, LineEdit, PrimaryPushButton, PushButton, RoundMenu,
    SingleDirectionScrollArea, SpinBox, SubtitleLabel, SwitchButton,
    TableWidget, ToolButton,
)

from app import modbus_core as mb
from app import serial_utils as su
from app.ui.console_style import setup_log_view

READ_FCS = {
    "01 读线圈": 1,
    "02 读离散输入": 2,
    "03 读保持寄存器": 3,
    "04 读输入寄存器": 4,
}
FORMATS = ["Unsigned", "Signed", "Hex", "Float (2 reg)", "ASCII"]
PAIR_FMTS = ("Float (2 reg)", "ASCII")
# 网格：每列固定 10 个寄存器（列头 = 组起始地址，行 0~9 为组内偏移）
ROWS_PER_GROUP = 10


def cell_address(base: int, row: int, col: int) -> int:
    """网格 (行, 列) → 绝对寄存器地址。"""
    return base + col * ROWS_PER_GROUP + row


def format_value(v: int, fmt: str) -> str:
    if fmt == "Signed":
        return str(v - 0x10000 if v >= 0x8000 else v)
    if fmt == "Hex":
        return f"{v:04X}"
    if fmt == "Unsigned":
        return str(v)
    return str(v)


def format_float_pair(hi: int, lo: int) -> str:
    word = ((hi & 0xFFFF) << 16) | (lo & 0xFFFF)
    return f"{struct.unpack('>f', struct.pack('>I', word))[0]:.6g}"


def format_ascii_pair(hi: int, lo: int) -> str:
    raw = struct.pack(">HH", hi & 0xFFFF, lo & 0xFFFF)
    return "".join(chr(b) if 32 <= b < 127 else "." for b in raw)


def parse_float_pair(text: str) -> tuple:
    """文本 → (高字, 低字) IEEE-754 大端编码；与 format_float_pair 互逆。"""
    word = struct.unpack(">I", struct.pack(">f", float(text)))[0]
    return (word >> 16) & 0xFFFF, word & 0xFFFF


def parse_ascii_pair(text: str) -> tuple:
    """文本（最多 4 字符，不足补 \\0）→ (高字, 低字)。"""
    raw = text.encode("ascii", "ignore")[:4]
    if not raw:
        raise ValueError("empty ascii")
    raw = raw.ljust(4, b"\x00")
    return struct.unpack(">HH", raw)


class ModbusPage(QWidget):

    def __init__(self, mt: "mb.ModbusThread", parent=None):
        super().__init__(parent)
        self.mt = mt
        self._connected = False
        self._last_read = None        # 最近一次读结果 {fc, addr, values}
        self._cell_fmt = {}           # 逐格类型覆盖 {绝对地址: 格式}

        scroll = SingleDirectionScrollArea(self)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(12)
        ll.addWidget(self._build_connect_card())
        ll.addWidget(self._build_poll_card())
        ll.addStretch(1)
        scroll.setWidget(left)
        scroll.setFixedWidth(330)
        scroll.setWidgetResizable(True)
        scroll.enableTransparentBackground()
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        splitter = QSplitter(Qt.Orientation.Vertical, self)
        splitter.addWidget(self._build_table_card())
        splitter.addWidget(self._build_trace_card())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setChildrenCollapsible(False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 40, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(scroll)
        layout.addWidget(splitter, 1)

        self._connect_signals()
        self._on_transport_changed(self.transportCombo.currentText())

    # ── 左：连接卡 ─────────────────────────────────────────────

    def _build_connect_card(self) -> CardWidget:
        card = CardWidget()
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(8)
        v.addWidget(SubtitleLabel("连接", card))

        self.libLabel = CaptionLabel(mb.pymodbus_info(), card)
        self.libLabel.setWordWrap(True)
        v.addWidget(self.libLabel)

        r = QHBoxLayout()
        r.addWidget(BodyLabel("类型", card))
        self.transportCombo = ComboBox(card)
        self.transportCombo.addItems(["RTU（串口）", "TCP"])
        self.transportCombo.currentTextChanged.connect(self._on_transport_changed)
        r.addWidget(self.transportCombo, 1)
        v.addLayout(r)

        # RTU 参数
        self.portEdit = LineEdit(card)
        self.portEdit.setPlaceholderText("COM 口，如 COM5")
        v.addWidget(self.portEdit)
        br = QHBoxLayout()
        br.addWidget(BodyLabel("波特率", card))
        self.baudCombo = ComboBox(card)
        self.baudCombo.addItems(["9600", "19200", "38400", "57600",
                                 "115200", "230400"])
        self.baudCombo.setCurrentText("9600")
        br.addWidget(self.baudCombo, 1)
        v.addLayout(br)
        pr = QHBoxLayout()
        pr.addWidget(BodyLabel("校验", card))
        self.parityCombo = ComboBox(card)
        self.parityCombo.addItems(["None", "Even", "Odd"])
        pr.addWidget(self.parityCombo, 1)
        pr.addWidget(BodyLabel("停止位", card))
        self.stopCombo = ComboBox(card)
        self.stopCombo.addItems(["1", "1.5", "2"])
        pr.addWidget(self.stopCombo, 1)
        v.addLayout(pr)

        # TCP 参数
        tr = QHBoxLayout()
        self.hostEdit = LineEdit(card)
        self.hostEdit.setText("127.0.0.1")
        self.tcpPortBox = SpinBox(card)
        self.tcpPortBox.setRange(1, 65535)
        self.tcpPortBox.setValue(502)
        self.tcpPortBox.setMinimumWidth(80)
        tr.addWidget(self.hostEdit, 1)
        tr.addWidget(self.tcpPortBox)
        v.addLayout(tr)

        sr = QHBoxLayout()
        sr.addWidget(BodyLabel("从站地址", card))
        self.slaveBox = SpinBox(card)
        self.slaveBox.setRange(0, 247)
        self.slaveBox.setValue(1)
        self.slaveBox.setMinimumWidth(70)
        sr.addStretch(1)
        sr.addWidget(self.slaveBox)
        v.addLayout(sr)

        brow = QHBoxLayout()
        self.connectBtn = PrimaryPushButton("连接", card)
        self.closeBtn = PushButton("断开", card)
        self.closeBtn.setEnabled(False)
        brow.addWidget(self.connectBtn, 1)
        brow.addWidget(self.closeBtn, 1)
        v.addLayout(brow)

        self.statusLabel = CaptionLabel("未连接", card)
        self.statusLabel.setWordWrap(True)
        v.addWidget(self.statusLabel)
        return card

    # ── 左：轮询定义卡 ─────────────────────────────────────────

    def _build_poll_card(self) -> CardWidget:
        card = CardWidget()
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(8)
        v.addWidget(SubtitleLabel("轮询定义", card))

        r1 = QHBoxLayout()
        self.fcCombo = ComboBox(card)
        self.fcCombo.addItems(list(READ_FCS))
        self.fcCombo.setCurrentIndex(2)  # 03
        r1.addWidget(BodyLabel("功能码", card))
        r1.addWidget(self.fcCombo, 1)
        v.addLayout(r1)

        r2 = QHBoxLayout()
        r2.addWidget(BodyLabel("起始地址", card))
        self.addrBox = SpinBox(card)
        self.addrBox.setRange(0, 65535)
        self.addrBox.setMinimumWidth(128)
        r2.addWidget(self.addrBox, 1)

        # 挤掉了换行
        # r2.addWidget(BodyLabel("数量", card))  
        # self.countBox = SpinBox(card)
        # self.countBox.setRange(1, 125)
        # self.countBox.setValue(10)
        # self.countBox.setMinimumWidth(56)
        # r2.addWidget(self.countBox, 1)
        v.addLayout(r2)

        r3 = QHBoxLayout()
        r3.addWidget(BodyLabel("数量", card))
        self.countBox = SpinBox(card)
        self.countBox.setRange(1, 125)
        self.countBox.setValue(10)
        self.countBox.setMinimumWidth(56)
        r3.addWidget(self.countBox, 1)
        v.addLayout(r3)

        prow = QHBoxLayout()
        prow.addWidget(BodyLabel("轮询间隔", card))
        self.pollBox = SpinBox(card)
        self.pollBox.setRange(100, 600_000)
        self.pollBox.setValue(1000)
        self.pollBox.setSuffix(" ms")
        self.pollBox.setMinimumWidth(90)
        
        self.pollSwitch = SwitchButton(card)
        self.pollSwitch.setChecked(True)
        self.pollSwitch.setOnText("开")
        self.pollSwitch.setOffText("关")
        prow.addWidget(self.pollBox, 1)
        prow.addWidget(self.pollSwitch)
        v.addLayout(prow)

        hint = CaptionLabel(
            "连接成功后按定义自动轮询；数据表一个寄存器一个小格，"
            "双击可写入，右键可设置逐格数据类型。", card)
        hint.setWordWrap(True)
        v.addWidget(hint)
        return card

    # ── 右：数据表 ─────────────────────────────────────────────

    def _build_table_card(self) -> CardWidget:
        card = CardWidget()
        v = QVBoxLayout(card)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(6)
        bar = QHBoxLayout()
        bar.addWidget(SubtitleLabel("数据", card))
        self.formatCombo = ComboBox(card)
        self.formatCombo.addItems(FORMATS)
        self.formatCombo.setFixedWidth(130)
        self.formatCombo.setToolTip("全局默认数据类型（右键单元格可逐格设置）")
        self.formatCombo.currentIndexChanged.connect(self._on_format_changed)
        bar.addWidget(self.formatCombo)
        readBtn = ToolButton(FluentIcon.UPDATE, card)
        readBtn.setToolTip("立即读取一次")
        readBtn.clicked.connect(self._do_read)
        bar.addWidget(readBtn)
        bar.addStretch(1)
        self.resultInfo = CaptionLabel("", card)
        bar.addWidget(self.resultInfo)
        v.addLayout(bar)
        self.table = TableWidget(card)
        # 网格：每列 10 格，列头 = 组起始地址，行头 = 组内偏移 0~9
        self.table.setRowCount(ROWS_PER_GROUP)
        self.table.setColumnCount(0)
        self.table.setVerticalHeaderLabels(
            [str(i) for i in range(ROWS_PER_GROUP)])
        self.table.setFont(QFont("Consolas", 10))
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectItems)
        self.table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self.table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        v.addWidget(self.table, 1)
        return card

    # ── 右：通信监视 ───────────────────────────────────────────

    def _build_trace_card(self) -> CardWidget:
        card = CardWidget()
        v = QVBoxLayout(card)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(6)
        bar = QHBoxLayout()
        bar.addWidget(SubtitleLabel("通信监视", card))
        bar.addStretch(1)
        clearBtn = ToolButton(FluentIcon.DELETE, card)
        clearBtn.setToolTip("清空")
        clearBtn.clicked.connect(lambda _=False: self.traceView.clear())
        bar.addWidget(clearBtn)
        v.addLayout(bar)
        self.traceView = QPlainTextEdit(card)
        self.traceView.setReadOnly(True)
        self.traceView.setFont(QFont("Consolas", 10))
        setup_log_view(self.traceView)
        v.addWidget(self.traceView, 1)
        return card

    # ── 信号接线 ───────────────────────────────────────────────

    def _connect_signals(self):
        w = self.mt.worker
        w.connected.connect(self._on_connected)
        w.connectFailed.connect(self._on_connect_failed)
        w.closed.connect(lambda: self._set_connected_ui(False))
        w.readResult.connect(self._on_read_result)
        w.writeResult.connect(self._on_write_result)
        w.errorOccurred.connect(self._on_error)
        self.connectBtn.clicked.connect(self._on_connect)
        self.closeBtn.clicked.connect(lambda _=False: self.mt.sigClose.emit())
        self._pollTimer = QTimer(self)
        self._pollTimer.timeout.connect(self._do_read)
        self.pollSwitch.checkedChanged.connect(self._on_poll_toggled)
        self.fcCombo.currentIndexChanged.connect(self._on_definition_changed)
        self.addrBox.valueChanged.connect(self._on_definition_changed)
        self.countBox.valueChanged.connect(self._on_definition_changed)
        self.pollBox.valueChanged.connect(self._on_definition_changed)

    def _on_definition_changed(self, *_):
        """定义变更后立即读一次并按新间隔重启轮询。"""
        if self._connected:
            self._do_read()
            if self.pollSwitch.isChecked():
                self._pollTimer.start(max(100, self.pollBox.value()))

    # ── 连接 ───────────────────────────────────────────────────

    def _on_transport_changed(self, text: str):
        is_rtu = "RTU" in text
        self.portEdit.setVisible(is_rtu)
        self.baudCombo.setVisible(is_rtu)
        self.parityCombo.setVisible(is_rtu)
        self.stopCombo.setVisible(is_rtu)
        self.hostEdit.setVisible(not is_rtu)
        self.tcpPortBox.setVisible(not is_rtu)

    def _on_connect(self):
        is_rtu = "RTU" in self.transportCombo.currentText()
        parity = {"None": "N", "Even": "E", "Odd": "O"}[
            self.parityCombo.currentText()]
        cfg = {
            "transport": "rtu" if is_rtu else "tcp",
            "port": self.portEdit.text().strip(),
            "baudrate": int(self.baudCombo.currentText() or 9600),
            "parity": parity,
            "stopbits": float(self.stopCombo.currentText()),
            "host": self.hostEdit.text().strip() or "127.0.0.1",
            "tcp_port": self.tcpPortBox.value(),
        }
        if is_rtu and not cfg["port"]:
            InfoBar.warning(title="缺少串口", content="请填写 COM 口",
                            duration=4000, parent=self)
            return
        self.connectBtn.setEnabled(False)
        self.mt.sigConnect.emit(cfg)

    def _on_connected(self, msg: str):
        self._set_connected_ui(True)
        self.statusLabel.setText(msg)
        self._trace(f"已连接：{msg}")
        # 连接成功：按定义立即读一次并启动自动轮询
        self._do_read()
        if self.pollSwitch.isChecked():
            self._pollTimer.start(max(100, self.pollBox.value()))

    def _on_connect_failed(self, msg: str):
        self.connectBtn.setEnabled(True)
        self.statusLabel.setText("连接失败")
        InfoBar.error(title="连接失败", content=msg, duration=6000, parent=self)

    def _set_connected_ui(self, on: bool):
        self._connected = on
        self.connectBtn.setEnabled(not on)
        self.closeBtn.setEnabled(on)
        if not on:
            self.statusLabel.setText("未连接")
            self._pollTimer.stop()

    # ── 读取 ───────────────────────────────────────────────────

    def _current_fc(self) -> int:
        return READ_FCS.get(self.fcCombo.currentText(), 3)

    def _do_read(self):
        if not self._connected:
            self._pollTimer.stop()
            return
        req = {
            "fc": self._current_fc(),
            "addr": self.addrBox.value(),
            "count": self.countBox.value(),
            "slave": self.slaveBox.value(),
        }
        self._trace(f"TX  FC{req['fc']:02d} @{req['addr']} ×{req['count']} "
                    f"(从站 {req['slave']})")
        self.mt.sigRead.emit(req)

    def _on_poll_toggled(self, on: bool):
        if on:
            if not self._connected:
                # 未连接：保留"开"状态但不启动轮询，
                # 连接成功后由 _on_connected 按开关状态启动
                return
            self._pollTimer.start(max(100, self.pollBox.value()))
        else:
            self._pollTimer.stop()

    # ── 数据表 ─────────────────────────────────────────────────

    def _on_read_result(self, r: dict):
        self._last_read = r
        vals = " ".join(f"{v:04X}" for v in r["values"]) if r["fc"] in (3, 4) \
            else "".join("1" if v else "0" for v in r["values"])
        self._trace(f"RX  FC{r['fc']:02d} @{r['addr']} ×{len(r['values'])}  "
                    f"{r['ms']} ms  [{vals}]")
        self.resultInfo.setText(
            f"FC{r['fc']:02d} @ {r['addr']} ×{len(r['values'])}  {r['ms']} ms")
        self._refill_table()

    def _on_format_changed(self, _=0):
        """全局格式变更：清空逐格覆盖后重刷。"""
        self._cell_fmt.clear()
        self._refill_table()

    def _effective_fmts(self, base: int, n: int):
        """计算读区间内每格的生效类型。

        返回 (fmt_map, cont_set)：fmt_map {地址: 格式} 覆盖起始格/单词格，
        cont_set 为双字类型的延续格地址（显示占位、只读）。
        """
        gfmt = self.formatCombo.currentText()
        fmts = {}
        conts = set()
        # 逐格覆盖优先
        for a, f in self._cell_fmt.items():
            if base <= a < base + n:
                fmts[a] = f
                if f in PAIR_FMTS and base <= a + 1 < base + n:
                    conts.add(a + 1)
        if gfmt in PAIR_FMTS:
            # 全局双字：按读顺序两两配对，跳过已被覆盖/延续的地址
            i = 0
            while i < n:
                a = base + i
                if a in fmts or a in conts:
                    i += 1
                    continue
                fmts[a] = gfmt
                if base <= a + 1 < base + n and a + 1 not in fmts:
                    conts.add(a + 1)
                i += 2
        else:
            for i in range(n):
                a = base + i
                if a not in fmts and a not in conts:
                    fmts[a] = gfmt
        return fmts, conts

    def _refill_table(self):
        r = self._last_read
        if not r:
            return
        fc = r["fc"]
        base = r["addr"]
        values = r["values"]
        n = len(values)
        cols = max(1, (n + ROWS_PER_GROUP - 1) // ROWS_PER_GROUP)
        fmts, conts = self._effective_fmts(base, n)
        t = self.table
        t.blockSignals(True)
        t.setColumnCount(cols)
        t.setRowCount(ROWS_PER_GROUP)
        t.setHorizontalHeaderLabels(
            [f"{base + c * ROWS_PER_GROUP:05d}" for c in range(cols)])
        hh = t.horizontalHeader()
        for c in range(cols):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch)
        for c in range(cols):
            for row in range(ROWS_PER_GROUP):
                idx = c * ROWS_PER_GROUP + row
                addr = base + idx
                if idx >= n:
                    item = QTableWidgetItem("")
                    item.setFlags(Qt.ItemFlag.NoItemFlags)
                elif addr in conts:
                    item = QTableWidgetItem("↳")
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled
                                  | Qt.ItemFlag.ItemIsSelectable)
                    item.setForeground(QColor(128, 128, 128))
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignCenter)
                else:
                    fmt = fmts.get(addr, "Unsigned")
                    if fc in (1, 2):
                        text = "ON" if values[idx] else "OFF"
                    elif fmt in PAIR_FMTS:
                        hi = values[idx]
                        lo = values[idx + 1] if idx + 1 < n else 0
                        text = (format_float_pair(hi, lo)
                                if fmt.startswith("Float")
                                else format_ascii_pair(hi, lo))
                    else:
                        text = format_value(values[idx], fmt)
                    item = QTableWidgetItem(text)
                    flags = (Qt.ItemFlag.ItemIsEnabled
                             | Qt.ItemFlag.ItemIsSelectable)
                    # 可写：线圈 FC1 / 保持寄存器 FC3 的单词格
                    if fc in (1, 3) and fmt not in PAIR_FMTS:
                        flags |= Qt.ItemFlag.ItemIsEditable
                    item.setFlags(flags)
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight
                        | Qt.AlignmentFlag.AlignVCenter)
                t.setItem(row, c, item)
        t.blockSignals(False)

    def _on_context_menu(self, pos):
        """右键单元格 → 设置该格数据类型。"""
        r = self._last_read
        item = self.table.itemAt(pos)
        if not r or item is None:
            return
        addr = cell_address(r["addr"], item.row(), item.column())
        if addr >= r["addr"] + len(r["values"]):
            return
        menu = RoundMenu(title=f"地址 {addr} 的数据类型", parent=self)
        for name in FORMATS:
            menu.addAction(Action(
                name,
                triggered=lambda _=False, a=addr, f=name:
                    self._set_cell_fmt(a, f)))
        if addr in self._cell_fmt:
            menu.addSeparator()
            menu.addAction(Action(
                "恢复默认",
                triggered=lambda _=False, a=addr:
                    self._set_cell_fmt(a, None)))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _set_cell_fmt(self, addr: int, fmt):
        if fmt is None:
            self._cell_fmt.pop(addr, None)
        else:
            self._cell_fmt[addr] = fmt
        self._refill_table()

    def _on_cell_double_clicked(self, row: int, col: int):
        """双击：双字格（Float/ASCII）弹对话框写入，单词格走格内编辑。"""
        r = self._last_read
        if not r or not self._connected or r["fc"] != 3:
            return
        addr = cell_address(r["addr"], row, col)
        idx = addr - r["addr"]
        if idx < 0 or idx >= len(r["values"]):
            return
        fmts, conts = self._effective_fmts(r["addr"], len(r["values"]))
        fmt = fmts.get(addr)
        if addr in conts or fmt not in PAIR_FMTS:
            return
        is_float = fmt.startswith("Float")
        hi = r["values"][idx]
        lo = r["values"][idx + 1] if idx + 1 < len(r["values"]) else 0
        current = (format_float_pair(hi, lo) if is_float
                   else format_ascii_pair(hi, lo))
        dlg = QDialog(self)
        dlg.setWindowTitle("写入 Float" if is_float else "写入 ASCII")
        dv = QVBoxLayout(dlg)
        dv.addWidget(BodyLabel(
            f"地址 {addr}（占 2 个寄存器，FC16 写入）", dlg))
        edit = LineEdit(dlg)
        edit.setText(current)
        dv.addWidget(edit)
        ok = PrimaryPushButton("写入", dlg)
        ok.clicked.connect(dlg.accept)
        dv.addWidget(ok)
        if not dlg.exec():
            return
        text = edit.text().strip()
        try:
            hi, lo = (parse_float_pair(text) if is_float
                      else parse_ascii_pair(text))
        except (ValueError, OverflowError):
            InfoBar.warning(title="数值无效", content="写入值解析失败",
                            duration=3000, parent=self)
            return
        req = {"fc": 16, "addr": addr, "values": [hi, lo],
               "slave": self.slaveBox.value()}
        self._trace(f"TX  FC16 @{addr} = [{hi:04X} {lo:04X}]")
        self.mt.sigWrite.emit(req)

    def _on_item_changed(self, item: QTableWidgetItem):
        """格内编辑 → 写入（保持寄存器 FC06 / 线圈 FC05）。"""
        r = self._last_read
        if not r or not self._connected:
            return
        fc = r["fc"]
        if fc in (2, 4):            # 离散输入/输入寄存器只读
            self._refill_table()
            return
        addr = cell_address(r["addr"], item.row(), item.column())
        if addr >= r["addr"] + len(r["values"]):
            self._refill_table()
            return
        text = item.text().strip()
        try:
            if fc == 1:
                if text.upper() not in ("ON", "OFF", "0", "1"):
                    raise ValueError
                value = 1 if text.upper() in ("ON", "1") else 0
                req = {"fc": 5, "addr": addr, "values": [value],
                       "slave": self.slaveBox.value()}
            else:
                fmt = self._cell_fmt.get(
                    addr, self.formatCombo.currentText())
                if fmt == "Hex":
                    value = int(text, 16)
                else:
                    value = int(text, 0)
                req = {"fc": 6, "addr": addr,
                       "values": [value & 0xFFFF],
                       "slave": self.slaveBox.value()}
        except ValueError:
            InfoBar.warning(title="数值无效", content="写入值解析失败，已还原",
                            duration=3000, parent=self)
            self._refill_table()
            return
        self._trace(f"TX  FC{req['fc']:02d} @{addr} = {req['values'][0]}")
        self.mt.sigWrite.emit(req)

    def _on_write_result(self, r: dict):
        self.resultInfo.setText(
            f"FC{r['fc']:02d} 写 @{r['addr']} ×{r['count']} 完成")
        self._trace(f"RX  FC{r['fc']:02d} 写成功 @{r['addr']} ×{r['count']}")
        # 写入后立即回读刷新
        self._do_read()

    # ── 日志 ───────────────────────────────────────────────────

    def _on_error(self, msg: str):
        self._trace(f"错误：{msg}")
        InfoBar.error(title="Modbus 错误", content=msg,
                      duration=5000, parent=self)

    def _trace(self, text: str):
        self.traceView.appendPlainText(f"{su.timestamp_str()} {text}")

    def shutdown(self):
        self._pollTimer.stop()
