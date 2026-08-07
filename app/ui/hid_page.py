# coding: utf-8
"""HID 调试页（对标 HIDAssist 类工具，依赖 hidapi.dll）。

功能：
- 设备枚举（VID/PID 过滤）/ 打开 / 关闭
- 中断报告收发：HEX/ASCII 显示、时间戳开关、关键字高亮
- 特征报告：获取 + 发送（写）
- 多命令模板：命名报文 + 延时/重复次数，批量发送与循环发送，可持久化
"""
import json

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QHBoxLayout, QHeaderView, QPlainTextEdit,
    QSplitter, QTableWidgetItem, QVBoxLayout, QWidget,
)

from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, CheckBox, ComboBox, FluentIcon,
    InfoBar, LineEdit, PrimaryPushButton, PushButton,
    SingleDirectionScrollArea, SpinBox, SubtitleLabel, SwitchButton,
    TableWidget, ToolButton,
)

from app import serial_utils as su
from app.hid_worker import HidThread
from app.native import LIBS_DIR, NativeError
from app import hid_binding
from app.ui.console_style import setup_log_view

MAX_CHARS = 200_000
TEMPLATE_FILE = LIBS_DIR.parent / "hid_templates.json"
HILITE_COLORS = ["#ffd54f", "#80deea", "#a5d6a7", "#f48fb1", "#ce93d8"]


class HidPage(QWidget):

    def __init__(self, ht: HidThread, parent=None):
        super().__init__(parent)
        self.ht = ht
        self._devices = []           # 枚举结果列表，与 combo 下标对应
        self._rx = 0
        self._tx = 0
        self._paused = False
        self._rows = []              # 模板行数据 [{sel,name,data,delay,repeat}]
        self._seq = []               # 批量发送队列 [(bytes, delay_ms)]
        self._seq_idx = 0
        self._seq_loop = False

        # ── 布局 ────────────────────────────────────────────────
        scroll = SingleDirectionScrollArea(self)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(12)
        ll.addWidget(self._build_device_card())
        ll.addWidget(self._build_display_card())
        ll.addWidget(self._build_feature_card())
        ll.addWidget(self._build_template_card())
        ll.addStretch(1)
        scroll.setWidget(left)
        scroll.setFixedWidth(340)
        scroll.setWidgetResizable(True)
        scroll.enableTransparentBackground()
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        splitter = QSplitter(Qt.Orientation.Vertical, self)
        splitter.addWidget(self._build_receive_card())
        splitter.addWidget(self._build_send_card())
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 0)
        splitter.setChildrenCollapsible(False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 40, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(scroll)
        layout.addWidget(splitter, 1)

        self._connect_signals()
        self._set_opened(False)
        self._refresh_dll_label()
        self._load_templates()

    # ── 左：设备卡 ─────────────────────────────────────────────

    def _build_device_card(self) -> CardWidget:
        card = CardWidget()
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(10)
        v.addWidget(SubtitleLabel("HID 设备", card))

        self.dllLabel = CaptionLabel("", card)
        self.dllLabel.setWordWrap(True)
        v.addWidget(self.dllLabel)

        frow = QHBoxLayout()
        self.vidEdit = LineEdit(card)
        self.vidEdit.setPlaceholderText("VID 可选")
        self.pidEdit = LineEdit(card)
        self.pidEdit.setPlaceholderText("PID 可选")
        frow.addWidget(self.vidEdit, 1)
        frow.addWidget(self.pidEdit, 1)
        v.addWidget(BodyLabel("过滤（十六进制）", card))
        v.addLayout(frow)

        erow = QHBoxLayout()
        self.deviceCombo = ComboBox(card)
        refreshBtn = ToolButton(FluentIcon.UPDATE, card)
        refreshBtn.setToolTip("枚举 HID 设备")
        refreshBtn.clicked.connect(lambda _=False: self.enumerate(notify=True))
        erow.addWidget(self.deviceCombo, 1)
        erow.addWidget(refreshBtn)
        v.addWidget(BodyLabel("设备", card))
        v.addLayout(erow)

        brow = QHBoxLayout()
        self.openBtn = PrimaryPushButton("打开设备", card)
        self.closeBtn = PushButton("关闭", card)
        self.closeBtn.setEnabled(False)
        brow.addWidget(self.openBtn, 1)
        brow.addWidget(self.closeBtn, 1)
        v.addLayout(brow)

        self.infoLabel = CaptionLabel("未打开", card)
        self.infoLabel.setWordWrap(True)
        v.addWidget(self.infoLabel)
        return card

    # ── 左：显示选项卡 ─────────────────────────────────────────

    def _build_display_card(self) -> CardWidget:
        card = CardWidget()
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(8)
        v.addWidget(SubtitleLabel("显示选项", card))

        r1 = QHBoxLayout()
        r1.addWidget(BodyLabel("显示格式", card))
        self.dispCombo = ComboBox(card)
        self.dispCombo.addItems(["HEX", "ASCII"])
        r1.addWidget(self.dispCombo, 1)
        v.addLayout(r1)

        self.tsCheck = CheckBox("时间戳", card)
        self.tsCheck.setChecked(True)
        v.addWidget(self.tsCheck)

        krow = QHBoxLayout()
        self.kwEdit = LineEdit(card)
        self.kwEdit.setPlaceholderText("关键字，如 34 或 OK（可留空）")
        krow.addWidget(self.kwEdit, 1)
        v.addWidget(CaptionLabel("接收关键字高亮", card))
        v.addLayout(krow)
        return card

    # ── 左：特征报告卡 ─────────────────────────────────────────

    def _build_feature_card(self) -> CardWidget:
        card = CardWidget()
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(8)
        v.addWidget(SubtitleLabel("特征报告", card))

        grow = QHBoxLayout()
        self.featIdBox = SpinBox(card)
        self.featIdBox.setRange(0, 255)
        self.featIdBox.setMinimumWidth(64)
        self.featIdBox.setToolTip("报告 ID")
        self.featLenBox = SpinBox(card)
        self.featLenBox.setRange(1, 512)
        self.featLenBox.setValue(64)
        self.featLenBox.setMinimumWidth(64)
        self.featLenBox.setToolTip("缓冲长度")
        self.featGetBtn = PushButton("获取", card)
        grow.addWidget(self.featIdBox)
        grow.addWidget(self.featLenBox)
        grow.addWidget(self.featGetBtn)
        v.addLayout(grow)

        self.featSendEdit = LineEdit(card)
        self.featSendEdit.setPlaceholderText("HEX：首字节为报告 ID + 数据")
        self.featSendBtn = PushButton("发送（写）", card)
        v.addWidget(self.featSendEdit)
        v.addWidget(self.featSendBtn)
        return card

    # ── 左：发送模板卡 ─────────────────────────────────────────

    def _build_template_card(self) -> CardWidget:
        card = CardWidget()
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(8)
        v.addWidget(SubtitleLabel("发送模板", card))

        self.tplTable = TableWidget(card)
        self.tplTable.setColumnCount(5)
        self.tplTable.setRowCount(0)
        # 表头缩短以适配 340px 左栏，避免表格横向滚动截断内容
        self.tplTable.setHorizontalHeaderLabels(
            ["✓", "名称", "HEX", "延时", "重复"])
        hh = self.tplTable.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.tplTable.setColumnWidth(0, 24)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.tplTable.setColumnWidth(3, 44)
        self.tplTable.setColumnWidth(4, 36)
        hh.setStretchLastSection(True)
        self.tplTable.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tplTable.verticalHeader().setVisible(False)
        self.tplTable.setMinimumHeight(120)
        self.tplTable.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        v.addWidget(self.tplTable)

        nrow = QHBoxLayout()
        self.tplNameEdit = LineEdit(card)
        self.tplNameEdit.setPlaceholderText("名称")
        self.tplDataEdit = LineEdit(card)
        self.tplDataEdit.setPlaceholderText("HEX 数据")
        nrow.addWidget(self.tplNameEdit, 1)
        nrow.addWidget(self.tplDataEdit, 2)
        v.addLayout(nrow)

        prow = QHBoxLayout()
        self.tplDelayBox = SpinBox(card)
        self.tplDelayBox.setRange(0, 600_000)
        self.tplDelayBox.setValue(100)
        self.tplDelayBox.setSuffix(" ms")
        self.tplDelayBox.setMinimumWidth(70)
        self.tplRepeatBox = SpinBox(card)
        self.tplRepeatBox.setRange(1, 10000)
        self.tplRepeatBox.setValue(1)
        self.tplRepeatBox.setMinimumWidth(60)
        prow.addWidget(CaptionLabel("延时", card))
        prow.addWidget(self.tplDelayBox, 1)
        prow.addWidget(CaptionLabel("重复", card))
        prow.addWidget(self.tplRepeatBox, 1)
        v.addLayout(prow)

        brow = QHBoxLayout()
        self.tplAddBtn = PushButton("添加", card)
        self.tplDelBtn = PushButton("删除选中", card)
        brow.addWidget(self.tplAddBtn, 1)
        brow.addWidget(self.tplDelBtn, 1)
        v.addLayout(brow)

        srow = QHBoxLayout()
        self.tplSendBtn = PrimaryPushButton("批量发送", card)
        self.tplStopBtn = PushButton("停止", card)
        self.tplStopBtn.setEnabled(False)
        
        self.tplLoopSwitch = SwitchButton(card)
        self.tplLoopSwitch.setOnText("循环")
        self.tplLoopSwitch.setOffText("单次")
        srow.addWidget(self.tplSendBtn, 1)
        srow.addWidget(self.tplStopBtn)
        srow.addWidget(self.tplLoopSwitch)
        v.addLayout(srow)
        return card

    # ── 右：接收区 ─────────────────────────────────────────────

    def _build_receive_card(self) -> CardWidget:
        card = CardWidget()
        v = QVBoxLayout(card)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(6)

        bar = QHBoxLayout()
        bar.addWidget(SubtitleLabel("接收", card))
        bar.addStretch(1)
        self.hexSendSwitch = SwitchButton(card)
        self.hexSendSwitch.setOnText("HEX 发送")
        self.hexSendSwitch.setOffText("文本发送")
        self.hexSendSwitch.setChecked(True)
        bar.addWidget(self.hexSendSwitch)
        self.autoRidCheck = CheckBox("自动补报告 ID 0x00", card)
        # 默认不补：带编号报告的设备（报告 ID 非 0）补 0x00 反而触发
        # WriteFile 0x57 参数错误；写入失败时底层会自动换形式重试
        self.autoRidCheck.setChecked(False)
        self.autoRidCheck.setToolTip(
            "仅用于「设备不使用报告 ID」的场景：发送数据前补 0x00 首字节。"
            "带编号报告的设备请关闭；写入失败会自动用另一种形式重试")
        bar.addWidget(self.autoRidCheck)
        self.countLabel = CaptionLabel("RX: 0 B  TX: 0 B", card)
        bar.addWidget(self.countLabel)
        pauseBtn = ToolButton(FluentIcon.PAUSE, card)
        pauseBtn.setToolTip("暂停/恢复显示")
        pauseBtn.setCheckable(True)
        pauseBtn.toggled.connect(self._on_pause)
        clearBtn = ToolButton(FluentIcon.DELETE, card)
        clearBtn.setToolTip("清屏")
        clearBtn.clicked.connect(lambda _=False: self.rxView.clear())
        bar.addWidget(pauseBtn)
        bar.addWidget(clearBtn)
        v.addLayout(bar)

        self.rxView = QPlainTextEdit(card)
        self.rxView.setReadOnly(True)
        self.rxView.setFont(QFont("Consolas", 10))
        setup_log_view(self.rxView)
        v.addWidget(self.rxView, 1)
        return card

    # ── 右：发送区 ─────────────────────────────────────────────

    def _build_send_card(self) -> CardWidget:
        card = CardWidget()
        h = QHBoxLayout(card)
        h.setContentsMargins(12, 10, 12, 10)
        h.setSpacing(8)
        self.sendEdit = LineEdit(card)
        self.sendEdit.setPlaceholderText("HEX：AA BB CC（支持空格/逗号/0x 前缀）")
        self.sendEdit.returnPressed.connect(self._on_send)
        self.sendBtn = PrimaryPushButton("发送", card)
        self.sendBtn.setFixedWidth(90)
        self.sendBtn.clicked.connect(self._on_send)
        h.addWidget(self.sendEdit, 1)
        h.addWidget(self.sendBtn)
        return card

    # ── 信号接线 ───────────────────────────────────────────────

    def _connect_signals(self):
        w = self.ht.worker
        w.deviceOpened.connect(self._on_opened)
        w.openFailed.connect(self._on_open_failed)
        w.deviceClosed.connect(lambda: self._set_opened(False))
        w.dataReceived.connect(self._on_rx)
        w.dataWritten.connect(self._on_tx)
        w.featureData.connect(self._on_feature_data)
        w.errorOccurred.connect(
            lambda msg: InfoBar.error(title="HID 错误", content=msg,
                                      duration=5000, parent=self.rxView))

        self.openBtn.clicked.connect(self._on_open)
        self.closeBtn.clicked.connect(lambda _=False: self.ht.sigClose.emit())
        self.featGetBtn.clicked.connect(
            lambda _=False: self.ht.sigFeatureGet.emit(
                self.featIdBox.value(), self.featLenBox.value()))
        self.featSendBtn.clicked.connect(self._on_feature_send)

        self.tplAddBtn.clicked.connect(self._tpl_add)
        self.tplDelBtn.clicked.connect(self._tpl_delete)
        self.tplSendBtn.clicked.connect(self._tpl_start)
        self.tplStopBtn.clicked.connect(self._tpl_stop)

        self._periodTimer = QTimer(self)
        self._periodTimer.setSingleShot(False)
        self._periodTimer.timeout.connect(self._tpl_tick)

    # ── 枚举 / 打开 ────────────────────────────────────────────

    def enumerate(self, notify=False):
        vid = self._parse_filter(self.vidEdit.text())
        pid = self._parse_filter(self.pidEdit.text())
        if vid is None or pid is None:
            InfoBar.warning(title="过滤无效", content="VID/PID 需为十六进制数",
                            duration=4000, parent=self)
            return
        try:
            devs = hid_binding.enumerate_devices(vid, pid)
        except NativeError as e:
            InfoBar.error(title="枚举失败", content=str(e),
                          duration=6000, parent=self)
            return
        self._devices = devs
        cur = self.deviceCombo.currentIndex()
        self.deviceCombo.clear()
        for d in devs:
            label = (f"{d['vid']:04X}:{d['pid']:04X}"
                     + (f"  {d['product']}" if d["product"] else ""))
            self.deviceCombo.addItem(label)
        if 0 <= cur < self.deviceCombo.count():
            self.deviceCombo.setCurrentIndex(cur)
        if notify:
            InfoBar.success(title="枚举完成", content=f"发现 {len(devs)} 个 HID 设备",
                            duration=3000, parent=self)

    @staticmethod
    def _parse_filter(text: str):
        text = (text or "").strip()
        if not text:
            return 0
        try:
            return int(text, 16) & 0xFFFF
        except ValueError:
            return None

    def _on_open(self):
        idx = self.deviceCombo.currentIndex()
        if idx < 0 or idx >= len(self._devices):
            InfoBar.warning(title="未选择设备", content="请先枚举并选择 HID 设备",
                            duration=4000, parent=self)
            return
        d = self._devices[idx]
        self.openBtn.setEnabled(False)
        self.ht.sigOpen.emit({
            "path": d["path"], "vid": d["vid"], "pid": d["pid"],
            "product": d.get("product", ""),
        })

    def _on_opened(self, info: dict):
        self._set_opened(True)
        rep = info.get("report_lengths") or {}
        rep_txt = ""
        if rep:
            rep_txt = (f"\n报告长度(含ID)：OUT={rep.get('output', 0)} "
                       f"IN={rep.get('input', 0)} FEAT={rep.get('feature', 0)}")
        self.infoLabel.setText(
            f"{info.get('product') or 'HID 设备'}\n"
            f"VID:PID = {info['vid']:04X}:{info['pid']:04X}"
            + (f"\nSN: {info['serial']}" if info.get('serial') else "")
            + rep_txt)
        InfoBar.success(title="设备已打开", content="", duration=2000, parent=self)

    def _on_open_failed(self, msg: str):
        self.openBtn.setEnabled(True)
        InfoBar.error(title="打开失败", content=msg, duration=6000, parent=self)

    def _set_opened(self, opened: bool):
        self.openBtn.setEnabled(not opened)
        self.closeBtn.setEnabled(opened)
        self.sendBtn.setEnabled(opened)
        self.featGetBtn.setEnabled(opened)
        self.featSendBtn.setEnabled(opened)
        self.tplSendBtn.setEnabled(opened)
        if not opened:
            self._tpl_stop()
            self.infoLabel.setText("未打开")

    # ── 单包发送 ───────────────────────────────────────────────

    def _apply_rid(self, data: bytes) -> bytes:
        """按「自动补报告 ID」开关处理首字节；单包/模板发送共用。"""
        if data and self.autoRidCheck.isChecked() and data[0] != 0x00:
            return b"\x00" + data
        return data

    def _build_payload(self):
        """解析发送框；返回 bytes 或 None（已提示）。"""
        text = self.sendEdit.text()
        if self.hexSendSwitch.isChecked():
            data, err = su.parse_hex_input(text)
            if data is None:
                InfoBar.warning(title="HEX 无效", content=err or "HEX 输入不合法",
                                duration=4000, parent=self)
                return None
        else:
            data = text.encode("utf-8")
        if not data:
            return None
        return self._apply_rid(data)

    def _on_send(self):
        if not self.ht.worker.opened:
            InfoBar.warning(title="未打开", content="HID 设备未打开",
                            duration=3000, parent=self)
            return
        data = self._build_payload()
        if data is None:
            return
        self.ht.sigWrite.emit(data)

    def _on_feature_send(self):
        if not self.ht.worker.opened:
            InfoBar.warning(title="未打开", content="HID 设备未打开",
                            duration=3000, parent=self)
            return
        data, err = su.parse_hex_input(self.featSendEdit.text())
        if data is None:
            InfoBar.warning(title="HEX 无效",
                            content=err or "特征报告需为 HEX（首字节为报告 ID）",
                            duration=4000, parent=self)
            return
        self.ht.sigFeatureSend.emit(data)
        self._append(f"{su.timestamp_str()} FEATURE-TX(ID={data[0]})  "
                     + su.format_hex(data))

    # ── 模板管理 ───────────────────────────────────────────────

    def _tpl_add(self):
        name = self.tplNameEdit.text().strip() or f"命令{len(self._rows) + 1}"
        data, err = su.parse_hex_input(self.tplDataEdit.text())
        if data is None:
            InfoBar.warning(title="HEX 无效", content=err or "模板数据需为 HEX",
                            duration=4000, parent=self)
            return
        self._rows.append({
            "sel": True, "name": name,
            "data": su.format_hex(data),
            "delay": self.tplDelayBox.value(),
            "repeat": self.tplRepeatBox.value(),
        })
        self.tplNameEdit.clear()
        self.tplDataEdit.clear()
        self._tpl_sync()
        self._save_templates()

    def _tpl_delete(self):
        sel = {i.index() for i in self.tplTable.selectionModel().selectedRows()}
        if not sel:
            return
        self._rows = [r for i, r in enumerate(self._rows) if i not in sel]
        self._tpl_sync()
        self._save_templates()

    def _tpl_sync(self):
        """按 self._rows 重建表格。"""
        self.tplTable.setRowCount(len(self._rows))
        for i, r in enumerate(self._rows):
            cb = QCheckBox()
            cb.setChecked(r["sel"])
            cb.stateChanged.connect(
                lambda st, i=i: self._rows[i].update(
                    {"sel": st == Qt.CheckState.Checked.value}))
            holder = QWidget()
            hl = QHBoxLayout(holder)
            hl.addWidget(cb)
            hl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hl.setContentsMargins(0, 0, 0, 0)
            self.tplTable.setCellWidget(i, 0, holder)

            nameItem = QTableWidgetItem(r["name"])
            dataItem = QTableWidgetItem(r["data"])
            delayItem = QTableWidgetItem(str(r["delay"]))
            repeatItem = QTableWidgetItem(str(r["repeat"]))
            for col, item in ((1, nameItem), (2, dataItem),
                              (3, delayItem), (4, repeatItem)):
                self.tplTable.setItem(i, col, item)

    def _tpl_commit_edits(self):
        """把表格里的手动编辑回写到 self._rows。"""
        for i in range(self.tplTable.rowCount()):
            if i >= len(self._rows):
                break
            r = self._rows[i]
            r["name"] = self.tplTable.item(i, 1).text().strip() or r["name"]
            data, _err = su.parse_hex_input(self.tplTable.item(i, 2).text())
            if data is not None:
                r["data"] = su.format_hex(data)
            try:
                r["delay"] = max(0, int(self.tplTable.item(i, 3).text()))
            except ValueError:
                pass
            try:
                r["repeat"] = max(1, int(self.tplTable.item(i, 4).text()))
            except ValueError:
                pass

    def _tpl_start(self):
        if not self.ht.worker.opened:
            InfoBar.warning(title="未打开", content="HID 设备未打开",
                            duration=3000, parent=self)
            return
        self._tpl_commit_edits()
        seq = self._build_seq([r for r in self._rows if r["sel"]])
        if not seq:
            InfoBar.warning(title="无可发送项",
                            content="请添加模板并勾选后再批量发送",
                            duration=4000, parent=self)
            return
        self._seq = seq
        self._seq_idx = 0
        self._seq_loop = self.tplLoopSwitch.isChecked()
        self.tplSendBtn.setEnabled(False)
        self.tplStopBtn.setEnabled(True)
        self._tpl_tick(first=True)

    def _build_seq(self, rows):
        seq = []
        for r in rows:
            data, _err = su.parse_hex_input(r["data"])
            if data is None:
                continue
            data = self._apply_rid(data)
            for _ in range(int(r["repeat"])):
                seq.append((data, int(r["delay"])))
        return seq

    def _tpl_tick(self, first=False):
        if self._seq_idx >= len(self._seq):
            if self._seq_loop and self.ht.worker.opened:
                self._seq_idx = 0
            else:
                self._tpl_stop()
                return
        data, delay = self._seq[self._seq_idx]
        self._seq_idx += 1
        self.ht.sigWrite.emit(data)
        self._periodTimer.start(delay if not first else max(delay, 1))

    def _tpl_stop(self):
        self._periodTimer.stop()
        self._seq = []
        self._seq_idx = 0
        self.tplStopBtn.setEnabled(False)
        self.tplSendBtn.setEnabled(self.ht.worker.opened)

    def _save_templates(self):
        try:
            with open(TEMPLATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._rows, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    def _load_templates(self):
        try:
            with open(TEMPLATE_FILE, encoding="utf-8") as f:
                rows = json.load(f)
            self._rows = [r for r in rows
                          if isinstance(r, dict) and "data" in r]
        except (OSError, ValueError):
            self._rows = []
        self._tpl_sync()

    # ── 收发显示 ───────────────────────────────────────────────

    def _on_rx(self, data: bytes, ts: float):
        self._rx += len(data)
        self._update_counts()
        if self._paused:
            return
        if self.dispCombo.currentText() == "ASCII":
            body = data.decode("utf-8", "replace")
        else:
            body = su.format_hex(data)
        prefix = f"{su.timestamp_str(ts)} " if self.tsCheck.isChecked() else ""
        self._append(f"{prefix}RX  {body}", body)

    def _on_tx(self, n: int):
        self._tx += int(n)
        self._update_counts()

    def _on_feature_data(self, data: bytes):
        self._append(f"{su.timestamp_str()} FEATURE-RX(ID={self.featIdBox.value()})  "
                     + su.format_hex(data), su.format_hex(data))

    def _on_pause(self, on: bool):
        self._paused = on

    def _append(self, line: str, match_text: str = ""):
        cur = self.rxView
        at_bottom = cur.verticalScrollBar().value() >= \
            cur.verticalScrollBar().maximum() - 2
        kw = self.kwEdit.text().strip()
        if kw and kw.lower() in (match_text or line).lower():
            color = HILITE_COLORS[len(kw) % len(HILITE_COLORS)]
            cur.appendHtml(
                f'<span style="background-color:{color};color:#202020;">'
                f"{line.replace('&', '&amp;').replace('<', '&lt;')}</span>")
        else:
            cur.appendPlainText(line)
        if cur.blockCount() > 0 and len(cur.toPlainText()) > MAX_CHARS:
            # 容量截断：删前半
            tc = cur.textCursor()
            tc.movePosition(tc.MoveOperation.Start)
            tc.movePosition(tc.MoveOperation.Down, tc.MoveMode.KeepAnchor,
                            cur.blockCount() // 2)
            tc.removeSelectedText()
        if at_bottom:
            cur.verticalScrollBar().setValue(cur.verticalScrollBar().maximum())

    def _update_counts(self):
        self.countLabel.setText(
            f"RX: {su.fmt_bytes(self._rx)}  TX: {su.fmt_bytes(self._tx)}")

    # ── 其它 ───────────────────────────────────────────────────

    def _refresh_dll_label(self):
        self.dllLabel.setText(hid_binding.load_info())

    def shutdown(self):
        self._periodTimer.stop()
        self._tpl_commit_edits()
        self._save_templates()
