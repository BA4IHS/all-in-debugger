# coding: utf-8
"""Modbus 调试页：对标 Modbus Poll（主站）。

- 连接：RTU / TCP
- 读写配置表：每行一条定义（是否启用 / 从站地址 / 数据地址 / 功能码 / 数据类型 /
  别名 / 数据 / 间隔时间），行内「读取 / 写入」按钮即时操作，
  启用且为读功能码的行按各自间隔自动轮询
- 配置导出：整表配置导出为 JSON 文件
- 通信监视：请求/响应 Trace 日志
"""
import json
import struct
import time

from PyQt6.QtCore import QEvent, QObject, QRegularExpression, Qt, QTimer
from PyQt6.QtGui import QFont, QRegularExpressionValidator
from PyQt6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView,
    QPlainTextEdit, QSplitter, QTableWidgetItem, QVBoxLayout, QWidget,
)

from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, CheckBox, ComboBox, FluentIcon,
    InfoBar, LineEdit, PrimaryPushButton, PushButton,
    SingleDirectionScrollArea, SpinBox, SubtitleLabel, TableWidget,
    ToolButton,
)

from app import modbus_core as mb
from app import serial_utils as su
from app.ui.console_style import setup_log_view

# 功能码选项（显示名 → 码）
FC_ITEMS = [
    ("01 读线圈", 1), ("02 读离散输入", 2),
    ("03 读保持寄存器", 3), ("04 读输入寄存器", 4),
    ("05 写单个线圈", 5), ("06 写单个寄存器", 6),
    ("15 写多个线圈", 15), ("16 写多个寄存器", 16),
]
FC_CODES = [code for _name, code in FC_ITEMS]

# 数据类型选项（显示名 → (内部标识, 寄存器数)），按数据宽度由小到大排序；
# 32 位及以上区分寄存器大端/小端（_be 高字在前，_le 低字在前）
DT_ITEMS = [
    ("bin（原始二进制）", "bin", 1),
    ("bit（1位）", "bit", 1),
    ("uint8（8位）", "uint8", 1),
    ("int8（8位）", "int8", 1),
    ("uint16（16位）", "uint16", 1),
    ("int16（16位）", "int16", 1),
    ("uint32 大端（32位）", "uint32_be", 2),
    ("uint32 小端（32位）", "uint32_le", 2),
    ("int32 大端（32位）", "int32_be", 2),
    ("int32 小端（32位）", "int32_le", 2),
    ("float 大端（32位）", "float_be", 2),
    ("float 小端（32位）", "float_le", 2),
    ("ASCII（4字符）", "ascii", 2),
    ("double 大端（64位）", "double_be", 4),
    ("double 小端（64位）", "double_le", 4),
]
DT_NAMES = [name for name, _id, _n in DT_ITEMS]        # 下拉显示名
DT_IDS = {name: _id for name, _id, _n in DT_ITEMS}      # 显示名 → 内部标识
DT_SIZES = {_id: _n for _name, _id, _n in DT_ITEMS}     # 内部标识 → 寄存器数
PAIR_TYPES = {_id for _name, _id, _n in DT_ITEMS if _n == 2}  # 兼容旧引用

# 旧版本 dtype 标识 → 新标识（旧版无大小端概念，默认大端）
_DTYPE_ALIAS = {
    "uint32": "uint32_be", "int32": "int32_be",
    "float": "float_be", "ASCII": "ascii",
}

# 新行默认值（点击「添加」追加）
DEFAULT_ROW = {
    "enabled": True, "slave": 1, "addr": 0, "fc": 3,
    "dtype": "uint16", "alias": "", "value": "", "interval": 1000,
}
# 初始示例行（与需求文档一致）
DEFAULT_ROWS = [
    {"enabled": True, "slave": 1, "addr": 4, "fc": 6,
     "dtype": "uint16", "alias": "测试 1", "value": "65535", "interval": 100},
    {"enabled": True, "slave": 1, "addr": 40, "fc": 6,
     "dtype": "uint16", "alias": "测试 1", "value": "65535", "interval": 100},
    {"enabled": True, "slave": 1, "addr": 50, "fc": 6,
     "dtype": "float_be", "alias": "测试 1", "value": "55655", "interval": 100},
]

# ── 编解码纯函数（tests/test_modbus_grid.py 依赖以下旧网格函数）──
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


def encode_value(text: str, dtype: str) -> list:
    """按数据类型把「数据」列文本编码为寄存器值列表。

    - bin：hex 字节流（空格分隔），每 2 字节一个寄存器（大端，奇数补 \\x00）
    - bit：0/1 单寄存器
    - uint8/int8：低 8 位；uint16/int16：整字
    - *_be：高字在前；*_le：低字在前；double 占 4 寄存器
    """
    text = text.strip()
    dtype = _DTYPE_ALIAS.get(dtype, dtype)
    if dtype == "bin":
        raw = bytes.fromhex(text.replace(" ", ""))
        if len(raw) % 2:
            raw += b"\x00"
        return [int.from_bytes(raw[i:i + 2], "big")
                for i in range(0, len(raw), 2)]
    if dtype == "bit":
        return [1 if int(text, 0) else 0]
    if dtype in ("uint8", "int8"):
        return [int(text, 0) & 0xFF]
    if dtype in ("uint16", "int16"):
        return [int(text, 0) & 0xFFFF]
    if dtype.endswith("_be") or dtype.endswith("_le"):
        little = dtype.endswith("_le")
        kind = dtype[:-3]
        n = DT_SIZES[dtype]
        if kind == "float":
            word = struct.unpack(">I", struct.pack(">f", float(text)))[0]
        elif kind == "double":
            word = struct.unpack(">Q", struct.pack(">d", float(text)))[0]
        else:
            word = int(text, 0) & ((1 << (16 * n)) - 1)
        if little:
            return [(word >> (16 * i)) & 0xFFFF for i in range(n)]
        return [(word >> (16 * (n - 1 - i))) & 0xFFFF for i in range(n)]
    if dtype == "ascii":
        return list(parse_ascii_pair(text))
    raise ValueError(f"未知数据类型 {dtype}")


def decode_value(values: list, dtype: str) -> str:
    """读取结果按数据类型解码为「数据」列显示文本。"""
    dtype = _DTYPE_ALIAS.get(dtype, dtype)
    if dtype == "bin":
        raw = b"".join(struct.pack(">H", int(v) & 0xFFFF)
                       for v in values)
        return " ".join(f"{b:02X}" for b in raw)
    if dtype == "bit":
        return "1" if int(values[0]) & 1 else "0"
    if dtype == "uint8":
        return str(int(values[0]) & 0xFF)
    if dtype == "int8":
        v = int(values[0]) & 0xFF
        return str(v - 0x100 if v >= 0x80 else v)
    if dtype in ("uint16", "int16"):
        return format_value(int(values[0]) & 0xFFFF,
                            "Signed" if dtype == "int16" else "Unsigned")
    if dtype.endswith("_be") or dtype.endswith("_le"):
        little = dtype.endswith("_le")
        kind = dtype[:-3]
        n = DT_SIZES[dtype]
        word = 0
        if little:
            for i, v in enumerate(values[:n]):
                word |= (int(v) & 0xFFFF) << (16 * i)
        else:
            for v in values[:n]:
                word = (word << 16) | (int(v) & 0xFFFF)
        if kind == "uint32":
            return str(word & 0xFFFFFFFF)
        if kind == "int32":
            v = word & 0xFFFFFFFF
            return str(v - 0x100000000 if v >= 0x80000000 else v)
        if kind == "float":
            # float32 有效数字约 7 位：%.7g 才能还原用户输入的小数，
            # %.6g 会截断（如 123.4567 → 123.457）
            return f"{struct.unpack('>f', struct.pack('>I', word & 0xFFFFFFFF))[0]:.7g}"
        if kind == "double":
            return f"{struct.unpack('>d', struct.pack('>Q', word & 0xFFFFFFFFFFFFFFFF))[0]:.15g}"
    if dtype == "ascii":
        return format_ascii_pair(int(values[0]), int(values[1]))
    return str(values[0])


class _RowFocusWatcher(QObject):
    """行内输入控件焦点监听：聚焦视为编辑中，暂停该行自动轮询。"""

    def __init__(self, idx: int, page: "ModbusPage"):
        super().__init__(page)
        self.idx = idx
        self.page = page

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.FocusIn:
            self.page._on_row_editing(self.idx)
        return False


class ModbusPage(QWidget):

    def __init__(self, mt: "mb.ModbusThread", parent=None):
        super().__init__(parent)
        self.mt = mt
        self._connected = False
        self._rows = []   # 读写配置行 [{enabled, slave, addr, fc, dtype, alias, value, interval, last_poll}]

        scroll = SingleDirectionScrollArea(self)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(12)
        ll.addWidget(self._build_connect_card())
        ll.addStretch(1)
        scroll.setWidget(left)
        scroll.setFixedWidth(330)
        scroll.setWidgetResizable(True)
        scroll.enableTransparentBackground()
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        splitter = QSplitter(Qt.Orientation.Vertical, self)
        splitter.addWidget(self._build_config_card())
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
        for cfg in DEFAULT_ROWS:
            self._add_row(dict(cfg))

    # ── 左：连接卡 ─────────────────────────────────────────────

    def _build_connect_card(self) -> CardWidget:
        card = CardWidget()
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(8)
        v.addWidget(SubtitleLabel("Modbus连接设置", card))

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

    # ── 左：连接设置（读写定义并入右侧表格，逐行独立配置） ──

    # ── 右：读写配置表 ─────────────────────────────────────────

    def _build_config_card(self) -> CardWidget:
        card = CardWidget()
        v = QVBoxLayout(card)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(6)

        bar = QHBoxLayout()
        bar.addWidget(SubtitleLabel("读写配置", card))
        self.resultInfo = CaptionLabel("", card)
        bar.addWidget(self.resultInfo)
        bar.addStretch(1)
        importBtn = ToolButton(FluentIcon.FOLDER, card)
        importBtn.setToolTip("配置导入")
        importBtn.clicked.connect(lambda _=False: self._import_config())
        bar.addWidget(importBtn)
        exportBtn = ToolButton(FluentIcon.SAVE, card)
        exportBtn.setToolTip("配置导出")
        exportBtn.clicked.connect(lambda _=False: self._export_config())
        bar.addWidget(exportBtn)
        v.addLayout(bar)

        self.table = TableWidget(card)
        self.table.setColumnCount(10)
        self.table.setHorizontalHeaderLabels([
            "是否启用", "从站地址", "数据地址", "功能码", "数据类型",
            "别名", "数据", "间隔时间", "读取", "写入"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for c, w in enumerate([80, 86, 96, 150, 138, 130, 140, 104, 62, 62]):
            self.table.setColumnWidth(c, w)
        v.addWidget(self.table, 1)

        brow = QHBoxLayout()
        addBtn = PushButton(FluentIcon.ADD, "添加", card)
        addBtn.clicked.connect(lambda _=False: self._add_row())
        delBtn = PushButton(FluentIcon.DELETE, "删除选中", card)
        delBtn.clicked.connect(lambda _=False: self._delete_selected())
        brow.addWidget(addBtn)
        brow.addWidget(delBtn)
        brow.addStretch(1)
        hint = CaptionLabel(
            "勾选启用且为读功能码的行按间隔自动轮询；「读取 / 写入」立即执行。",
            card)
        brow.addWidget(hint)
        v.addLayout(brow)
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
        self._pollTimer.setInterval(200)
        self._pollTimer.timeout.connect(self._poll_tick)

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
        # 重置各行轮询计时基准并启动自动轮询
        for row in self._rows:
            row["last_poll"] = time.monotonic()
        self._pollTimer.start()

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

    # ── 读写配置表 ─────────────────────────────────────────────

    def _add_row(self, cfg: dict = None):
        """追加一行配置（cfg 缺失时用默认值），并建立行内控件。"""
        if cfg is None:
            cfg = dict(DEFAULT_ROW)
        row = dict(cfg)
        row["last_poll"] = 0.0
        idx = len(self._rows)
        self._rows.append(row)
        t = self.table
        t.insertRow(idx)
        t.setRowHeight(idx, 38)
        item = QTableWidgetItem()
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        t.setItem(idx, 0, item)
        t.setCellWidget(idx, 0, self._mk_enabled(idx, row))
        t.setCellWidget(idx, 1, self._mk_slave(idx, row))
        t.setCellWidget(idx, 2, self._mk_addr(idx, row))
        t.setCellWidget(idx, 3, self._mk_fc(idx, row))
        t.setCellWidget(idx, 4, self._mk_dtype(idx, row))
        t.setCellWidget(idx, 5, self._mk_alias(idx, row))
        t.setCellWidget(idx, 6, self._mk_value(idx, row))
        t.setCellWidget(idx, 7, self._mk_interval(idx, row))
        t.setCellWidget(idx, 8, self._mk_action(idx, "读取", self._row_read))
        t.setCellWidget(idx, 9, self._mk_action(idx, "写入", self._row_write))
        # 输入控件焦点监听：聚焦视为编辑中，暂停该行自动轮询
        watcher = _RowFocusWatcher(idx, self)
        for c in range(1, 8):
            w = t.cellWidget(idx, c)
            if w is not None:
                w.installEventFilter(watcher)
        row["_watcher"] = watcher

    def _set_row(self, idx: int, field: str, value):
        self._rows[idx][field] = value
        if field in ("interval", "enabled"):
            # 间隔/启用变更：重置计时基准，避免刚改完立即读
            self._rows[idx]["last_poll"] = time.monotonic()

    def _on_row_editing(self, idx: int):
        """行内输入控件获得焦点：编辑期间暂停自动读取，并关闭该行启用勾选。"""
        if not (0 <= idx < len(self._rows)):
            return
        row = self._rows[idx]
        if row["enabled"]:
            row["enabled"] = False
            cb = self.table.cellWidget(idx, 0)
            if cb is not None:
                cb.blockSignals(True)
                cb.setChecked(False)
                cb.blockSignals(False)

    def _mk_enabled(self, idx: int, row: dict):
        cb = CheckBox(self.table)
        cb.setChecked(bool(row["enabled"]))
        cb.toggled.connect(lambda on, i=idx: self._set_row(i, "enabled", on))
        return cb

    def _no_text_cursor(self, w):
        """悬浮显示箭头光标而非文本光标（点击聚焦后才进入输入态）。"""
        # LineEdit / ComboBox：直接设置
        w.setCursor(Qt.CursorShape.ArrowCursor)
        # SpinBox 内部编辑区也要改（qfluentwidgets 显式设了 IBeamCursor）
        inner = getattr(w, "lineEdit", None)
        if callable(inner):
            le = inner()
            if le is not None:
                le.setCursor(Qt.CursorShape.ArrowCursor)

    def _mk_slave(self, idx: int, row: dict):
        sb = SpinBox(self.table)
        sb.setRange(0, 247)
        sb.setValue(int(row["slave"]))
        sb.setFixedHeight(30)
        # 地址类输入：隐藏上下箭头（qfluentwidgets 自定义绘制，需 setSymbolVisible）
        sb.setSymbolVisible(False)
        # 悬浮不显示文本光标（点击聚焦才进入输入态）
        self._no_text_cursor(sb)
        sb.valueChanged.connect(lambda v, i=idx: self._set_row(i, "slave", v))
        return sb

    def _mk_addr(self, idx: int, row: dict):
        ed = LineEdit(self.table)
        ed.setText(str(row["addr"]))
        ed.setFixedHeight(30)
        ed.setPlaceholderText("十进制或 0x 十六进制")
        ed.setClearButtonEnabled(False)
        # 只允许十进制数字或 0x 十六进制（含输入中间态 0x / 0X）
        ed.setValidator(QRegularExpressionValidator(
            QRegularExpression(r"(0[xX][0-9a-fA-F]{0,4}|\d{0,5})"), ed))
        self._no_text_cursor(ed)
        ed.textChanged.connect(lambda t, i=idx: self._on_addr_edited(i, t))
        return ed

    def _on_addr_edited(self, idx: int, text: str):
        """地址列文本解析：十进制或 0x 十六进制，范围 0–65535，非法保持旧值。"""
        t = text.strip()
        if not t:
            return
        try:
            v = int(t, 16) if t.lower().startswith("0x") else int(t, 10)
        except ValueError:
            return
        if 0 <= v <= 65535:
            self._set_row(idx, "addr", v)

    def _mk_fc(self, idx: int, row: dict):
        cb = ComboBox(self.table)
        for name, code in FC_ITEMS:
            # qfluentwidgets addItem(text, icon=None, userData=None)：
            # 码值必须走 userData，放第二参数会被当 icon
            cb.addItem(name, userData=code)
        try:
            cb.setCurrentIndex(FC_CODES.index(int(row["fc"])))
        except ValueError:
            cb.setCurrentIndex(2)  # 默认 03
        cb.setFixedHeight(30)
        self._no_text_cursor(cb)
        cb.currentIndexChanged.connect(lambda _n, i=idx: self._on_fc_changed(i))
        return cb

    def _mk_dtype(self, idx: int, row: dict):
        cb = ComboBox(self.table)
        for name in DT_NAMES:
            cb.addItem(name, userData=DT_IDS[name])
        cb.setCurrentIndex(self._dtype_index(row["dtype"]))
        cb.setFixedHeight(30)
        self._no_text_cursor(cb)
        cb.currentIndexChanged.connect(lambda _n, i=idx: self._on_dtype_changed(i))
        return cb

    def _on_dtype_changed(self, idx: int):
        cb = self.table.cellWidget(idx, 4)
        self._rows[idx]["dtype"] = cb.currentData()  # 内部标识

    def _mk_alias(self, idx: int, row: dict):
        ed = LineEdit(self.table)
        ed.setText(row["alias"])
        ed.setFixedHeight(30)
        ed.setPlaceholderText("别名")
        self._no_text_cursor(ed)
        ed.textChanged.connect(
            lambda t, i=idx: self._set_row(i, "alias", t))
        return ed

    def _mk_value(self, idx: int, row: dict):
        ed = LineEdit(self.table)
        ed.setText(str(row["value"]))
        ed.setFixedHeight(30)
        ed.setPlaceholderText("数据")
        self._no_text_cursor(ed)
        ed.textChanged.connect(
            lambda t, i=idx: self._set_row(i, "value", t))
        return ed

    def _mk_interval(self, idx: int, row: dict):
        sb = SpinBox(self.table)
        sb.setRange(50, 600_000)
        sb.setValue(int(row["interval"]))
        sb.setSuffix(" ms")
        sb.setFixedHeight(30)
        self._no_text_cursor(sb)
        sb.valueChanged.connect(
            lambda v, i=idx: self._set_row(i, "interval", v))
        return sb

    def _mk_action(self, idx: int, text: str, slot):
        btn = PushButton(text, self.table)
        btn.setFixedHeight(30)
        btn.clicked.connect(lambda _=False, i=idx: slot(i))
        return btn

    def _on_fc_changed(self, idx: int):
        cb = self.table.cellWidget(idx, 3)
        code = cb.currentData()
        self._rows[idx]["fc"] = code
        is_read = code <= 4
        read_btn = self.table.cellWidget(idx, 8)
        write_btn = self.table.cellWidget(idx, 9)
        if read_btn is not None:
            read_btn.setEnabled(is_read)
        if write_btn is not None:
            write_btn.setEnabled(not is_read)

    def _delete_selected(self):
        # 用 selectionModel 取选中行（col0 占位 item 无 flags，selectedItems 为空）
        sm = self.table.selectionModel()
        rows = sorted({i.row() for i in sm.selectedRows()}, reverse=True)
        if not rows:
            InfoBar.warning(title="未选中", content="请先选中要删除的行",
                            duration=3000, parent=self)
            return
        for r in rows:
            self.table.removeRow(r)
            self._rows.pop(r)

    def _export_config(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "配置导出", "modbus_config.json", "JSON 文件 (*.json)")
        if not path:
            return
        data = {"rows": [
            {k: v for k, v in row.items() if not k.startswith("_")}
            for row in self._rows]}
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            InfoBar.error(title="配置导出失败", content=str(e),
                          duration=5000, parent=self)
            return
        InfoBar.success(title="配置导出",
                        content=f"已导出 {len(self._rows)} 行 → {path}",
                        duration=4000, parent=self)

    def _import_config(self):
        """从导出的 JSON 文件导入整表配置（替换当前表格）。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "配置导入", "", "JSON 文件 (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            InfoBar.error(title="配置导入失败", content=str(e),
                          duration=5000, parent=self)
            return
        raw_rows = data.get("rows") if isinstance(data, dict) else None
        if not isinstance(raw_rows, list) or not raw_rows:
            InfoBar.warning(title="配置导入失败",
                            content="文件中没有有效的 rows 配置",
                            duration=4000, parent=self)
            return
        # 只接受含任一配置字段的行，纯噪音对象（如 {"foo": 1}）跳过
        valid_keys = {"enabled", "slave", "addr", "fc", "dtype",
                      "alias", "value", "interval"}
        rows = [self._sanitize_row(r) for r in raw_rows
                if isinstance(r, dict) and (set(r) & valid_keys)]
        if not rows:
            InfoBar.warning(title="配置导入失败",
                            content="文件中没有可用的配置行",
                            duration=4000, parent=self)
            return
        # 替换整表
        for r in range(len(self._rows) - 1, -1, -1):
            self.table.removeRow(r)
        self._rows.clear()
        for r in rows:
            self._add_row(r)
        # 已连接时重置轮询计时基准，避免导入后立即触发轮询
        now = time.monotonic()
        for row in self._rows:
            row["last_poll"] = now if self._connected else 0.0
        InfoBar.success(title="配置导入",
                        content=f"已导入 {len(self._rows)} 行 ← {path}",
                        duration=4000, parent=self)

    @staticmethod
    def _dtype_index(dtype: str) -> int:
        """任意 dtype 表示（内部标识/旧标识）→ 下拉索引，未知回退 0。"""
        dtype = _DTYPE_ALIAS.get(dtype, dtype)
        for i, (_name, _id, _n) in enumerate(DT_ITEMS):
            if _id == dtype:
                return i
        return 0

    @staticmethod
    def _dtype_id(value) -> str:
        """任意 dtype 表示（内部标识/旧标识/显示名）→ 内部标识，非法回退 uint16。"""
        if isinstance(value, str):
            value = _DTYPE_ALIAS.get(value, value)
            if value in DT_SIZES:
                return value
            for name, _id, _n in DT_ITEMS:
                if name == value:
                    return _id
        return "uint16"

    @staticmethod
    def _sanitize_row(r: dict) -> dict:
        """导入行字段清洗：类型强转 + 范围钳制，非法值回退默认。"""
        row = dict(DEFAULT_ROW)
        try:
            en = r.get("enabled", True)
            if isinstance(en, str):
                row["enabled"] = en.strip().lower() in ("1", "true", "yes", "on")
            else:
                row["enabled"] = bool(en)
            row["slave"] = max(0, min(247, int(r.get("slave", 1))))
            row["addr"] = max(0, min(65535, int(r.get("addr", 0))))
            fc = int(r.get("fc", 3))
            row["fc"] = fc if fc in FC_CODES else 3
            row["dtype"] = ModbusPage._dtype_id(r.get("dtype", "uint16"))
            row["alias"] = str(r.get("alias", ""))
            row["value"] = str(r.get("value", ""))
            row["interval"] = max(50, min(600_000, int(r.get("interval", 1000))))
        except (TypeError, ValueError):
            pass
        return row

    # ── 读取 / 写入 ────────────────────────────────────────────

    def _row_read(self, idx: int):
        row = self._rows[idx]
        if not self._connected:
            return
        fc = row["fc"]
        if fc not in (1, 2, 3, 4):
            return
        count = DT_SIZES.get(row["dtype"], 1)
        if row["dtype"] == "bin":
            # bin 模式：按「数据」列字节数推寄存器数（每寄存器 2 字节）
            try:
                raw = bytes.fromhex(str(row["value"]).replace(" ", ""))
                count = max(1, (len(raw) + 1) // 2)
            except ValueError:
                count = 1
        name = row["alias"] or f"@{row['addr']}"
        self._trace(f"TX  FC{fc:02d} @{row['addr']} ×{count} "
                    f"(从站 {row['slave']} · {name})")
        self.mt.sigRead.emit({
            "fc": fc, "addr": row["addr"], "count": count,
            "slave": row["slave"]})

    def _row_write(self, idx: int):
        row = self._rows[idx]
        if not self._connected:
            return
        fc = row["fc"]
        if fc not in (5, 6, 15, 16):
            return
        text = str(row["value"]).strip()
        try:
            if fc == 5:
                up = text.upper()
                if up not in ("ON", "OFF", "1", "0"):
                    raise ValueError("线圈值需为 ON/OFF/1/0")
                values = [1 if up in ("ON", "1") else 0]
            elif fc == 15:
                parts = [p.strip() for p in text.split(",") if p.strip()]
                if not parts:
                    raise ValueError("线圈列表为空")
                values = [1 if p.upper() in ("ON", "1") else 0
                          for p in parts]
            else:
                values = encode_value(text, row["dtype"])
                if fc == 6:
                    values = values[:1]   # 单寄存器
        except (ValueError, OverflowError) as e:
            InfoBar.warning(title="写入值无效",
                            content=str(e) or "解析失败",
                            duration=4000, parent=self)
            return
        name = row["alias"] or f"@{row['addr']}"
        self._trace(f"TX  FC{fc:02d} @{row['addr']} = {values} "
                    f"(从站 {row['slave']} · {name})")
        self.mt.sigWrite.emit({
            "fc": fc, "addr": row["addr"], "values": values,
            "slave": row["slave"]})

    def _poll_tick(self):
        """按各行间隔轮询启用且为读功能码的行。"""
        if not self._connected:
            return
        now = time.monotonic()
        for idx, row in enumerate(self._rows):
            if not row["enabled"] or row["fc"] not in (1, 2, 3, 4):
                continue
            if now - row["last_poll"] >= max(0.05, row["interval"] / 1000):
                row["last_poll"] = now
                self._row_read(idx)

    def _on_read_result(self, r: dict):
        vals = " ".join(f"{v:04X}" for v in r["values"]) \
            if r["fc"] in (3, 4) \
            else "".join("1" if v else "0" for v in r["values"])
        self._trace(f"RX  FC{r['fc']:02d} @{r['addr']} ×{len(r['values'])}  "
                    f"{r['ms']} ms  [{vals}]")
        self.resultInfo.setText(
            f"FC{r['fc']:02d} @ {r['addr']} ×{len(r['values'])}  {r['ms']} ms")
        # 回填第一个匹配的读行「数据」列
        for idx, row in enumerate(self._rows):
            if row["fc"] == r["fc"] and row["addr"] == r["addr"]:
                try:
                    text = decode_value(r["values"], row["dtype"])
                except (ValueError, OverflowError, IndexError):
                    text = "?"
                ed = self.table.cellWidget(idx, 6)
                if ed is not None:
                    ed.setText(text)
                break

    def _on_write_result(self, r: dict):
        self.resultInfo.setText(
            f"FC{r['fc']:02d} 写 @{r['addr']} ×{r['count']} 完成")
        self._trace(f"RX  FC{r['fc']:02d} 写成功 @{r['addr']} ×{r['count']}")

    # ── 日志 ───────────────────────────────────────────────────

    def _on_error(self, msg: str):
        self._trace(f"错误：{msg}")
        InfoBar.error(title="Modbus 错误", content=msg,
                      duration=5000, parent=self)

    def _trace(self, text: str):
        self.traceView.appendPlainText(f"{su.timestamp_str()} {text}")

    def shutdown(self):
        self._pollTimer.stop()
