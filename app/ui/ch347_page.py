# coding: utf-8
"""CH347 调试页（WCH USB2.0 转 SPI/I2C/GPIO/SPI Flash/EEPROM/LCD）。

功能对标官方 CH347Demo，按 Pivot 子页组织：
- SPI：接口参数初始化 + HEX 流收发（读&写/批量读/批量写）
- I2C：速率/ stretch 初始化 + 原始流收发
- I2C 器件：地址扫描、通用寄存器读写、初始化脚本（可保存/循环执行）
- GPIO：8 引脚方向/电平/别名 + 序列宏（复位时序等一键操作）
- Flash：JEDEC 识别、读/写(先擦后写)/分粒度擦除/BlankCheck/文件读写校验
- EEPROM：24Cxx 读写

所有设备操作经 Ch347Thread 请求-应答（op/id 令牌防串扰），自定义脚本
与别名持久化到 data.json 的 ch347 字段。
"""
import json

from PyQt6.QtCore import Qt, QSize, QTimer
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QColorDialog, QFileDialog, QHBoxLayout, QTableWidgetItem, QStackedWidget,
    QVBoxLayout, QWidget,
)

from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, CheckBox, ComboBox, FluentIcon,
    InfoBar, LineEdit, MessageBox, PlainTextEdit, Pivot, PrimaryPushButton,
    ProgressBar, PushButton, SingleDirectionScrollArea, SpinBox,
    SubtitleLabel, SwitchButton, TableWidget, ToolButton,
)

from app import ch347_core as cc
from app.ch347_worker import Ch347Thread
from app.config import loadData, saveData
from app.ui.console_style import setup_log_view
from app.ui.searchable_text_edit import SearchablePlainTextEdit

MAX_HEX_VIEW = 4096        # 输出视图最多显示的字节数


class Ch347Page(QWidget):
    """CH347 多功能调试页；thread 为唯一设备持有者。"""

    def sizeHint(self):
        # 子页内容（尤其 GPIO 八列卡片）可能给出较大的自然宽度；
        # 页面内容已有滚动容器，不能让自然宽度反向撑大主窗口。
        return QSize(640, 480)

    def minimumSizeHint(self):
        # 各子页表格/编辑框的 setMinimumHeight 累加使默认
        # minimumSizeHint 过高，被 QStackedLayout 放大为主窗口最小
        # 尺寸：lazy 构造完成后拖动窗口会被 clamp 回该最小尺寸
        # （表现为"突然变大并锁定、只能调大"）。这里声明可缩小的
        # hint，页内溢出交给 SingleDirectionScrollArea 垂直滚动。
        return QSize(640, 480)

    def __init__(self, cct: Ch347Thread, parent=None):
        super().__init__(parent)
        self.cct = cct
        self._opened = False
        self._devinfo = {}
        self._seq = 0
        self._pending = {}          # rid -> callback(ok, data|error)
        self._prog = {}             # rid -> callback(progress dict)
        self._scan_once = False
        self._device_signature = None
        self._scan_busy = False
        # 持久化区（data.json["ch347"]）
        self._store = dict(loadData().get("ch347") or {})

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 44, 20, 12)  # 左右统一留白，避免贴边
        root.setSpacing(10)
        root.addWidget(self._build_device_card())

        body = QWidget(self)
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(8)
        self.pivot = Pivot(body)
        self.stack = QStackedWidget(body)
        self._tabIdx = {}
        self._tabHost = {}        # key -> 空占位容器（首次激活时才填充）
        self._tabBuilders = {}    # key -> builder
        self._tabBuilt = set()    # 已构建内容的标签 key
        bl.addWidget(self.pivot)
        bl.addWidget(self.stack, 1)
        root.addWidget(body, 1)

        # 标签懒构建：这里只为每个标签放一个空占位容器并预约 builder，
        # 首次点到该标签时才真正构建内容，降低 CH347 页的初始构造成本。
        for key, title, builder in (
                ("spi", "SPI", self._build_spi_tab),
                ("i2c", "I2C", self._build_i2c_tab),
                ("i2cdev", "I2C 器件", self._build_i2c_dev_tab),
                ("gpio", "GPIO", self._build_gpio_tab),
                ("flash", "Flash", self._build_flash_tab),
            ("eeprom", "EEPROM", self._build_eeprom_tab)):
            host = QWidget()
            hl = QVBoxLayout(host)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setSpacing(0)
            idx = self.stack.addWidget(host)
            self._tabIdx[key] = idx
            self._tabHost[key] = host
            self._tabBuilders[key] = builder
            self.pivot.addItem(key, title,
                               onClick=lambda checked=False, k=key:
                               self._activateTab(k))
        self._activateTab("spi")        # 首个标签立即构建，默认可用
        self.pivot.setCurrentItem("spi")

        self.worker = cct.worker
        self.worker.sigOpResult.connect(self._on_result)
        self.worker.sigProgress.connect(self._on_progress)

        # 设备插入检测（1.5s，仅未打开设备且页面可见时请求）
        self._deviceTimer = QTimer(self)
        self._deviceTimer.setInterval(1500)
        self._deviceTimer.timeout.connect(self._device_tick)
        self._deviceTimer.start()

    def _activateTab(self, key: str):
        """切到指定标签；首次激活时懒构建其内容（见 __init__ 注释）。

        builder 返回的已是 self._scroll(page) 滚动容器，直接加入占位
        host 的布局即可（Qt 自动重 parenting）。已构建过则仅切页。
        """
        if key not in self._tabBuilt:
            self._tabBuilt.add(key)
            content = self._tabBuilders[key]()
            self._tabHost[key].layout().addWidget(content)
        self.stack.setCurrentIndex(self._tabIdx[key])

    # ── 通用：请求-应答 ────────────────────────────────────────

    def _req(self, op, cb=None, params=None, progress=None):
        self._seq += 1
        rid = f"ui{self._seq}"
        if cb is not None:
            self._pending[rid] = cb
        if progress is not None:
            self._prog[rid] = progress
        req = {"op": op, "id": rid}
        req.update(params or {})
        self.cct.sigRequest.emit(req)
        return rid

    def _on_result(self, msg: dict):
        rid = str(msg.get("id", ""))
        self._prog.pop(rid, None)
        cb = self._pending.pop(rid, None)
        if cb is None:
            return
        if msg.get("ok"):
            cb(True, msg.get("data") or {})
        else:
            cb(False, msg.get("error") or "未知错误")

    def _on_progress(self, msg: dict):
        fn = self._prog.get(str(msg.get("id", "")))
        if fn is not None:
            fn(msg)

    def _err(self, label, err):
        InfoBar.error(title=label, content=str(err), duration=6000,
                      parent=self)

    def _need_ready(self, label: str = "操作") -> bool:
        if not self._opened:
            InfoBar.warning(title="设备未打开",
                            content=f"请先扫描并打开 CH347 设备，再执行{label}",
                            duration=4000, parent=self)
            return False
        return True

    @staticmethod
    def _hexfmt(data_hex: str) -> str:
        b = bytes.fromhex(data_hex) if data_hex else b""
        if len(b) > MAX_HEX_VIEW:
            return cc.format_hex(b[:MAX_HEX_VIEW]) + \
                f"\n…（共 {len(b)} 字节，仅显示前 {MAX_HEX_VIEW}）"
        return cc.format_hex(b)

    def _persist(self):
        data = loadData()
        data["ch347"] = self._store
        saveData(data)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._scan_once:
            self._scan_once = True
            self._scan()

    def shutdown(self):
        self._deviceTimer.stop()
        self._pending.clear()
        self._prog.clear()

    # ── 顶部：设备卡 ───────────────────────────────────────────

    def _build_device_card(self) -> CardWidget:
        card = CardWidget()
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 12, 16, 12)
        v.setSpacing(6)
        row = QHBoxLayout()
        row.addWidget(BodyLabel("设备", card))
        self.deviceCombo = ComboBox(card)
        self.deviceCombo.setMinimumWidth(300)
        self.scanBtn = PushButton("扫描", card, icon=FluentIcon.SEARCH)
        self.scanBtn.clicked.connect(lambda _=False: self._scan(notify=True))
        self.openBtn = PrimaryPushButton("打开", card)
        self.openBtn.clicked.connect(self._open)
        self.closeBtn = PushButton("关闭", card)
        self.closeBtn.clicked.connect(self._close)
        self.closeBtn.setEnabled(False)
        row.addWidget(self.deviceCombo, 1)
        row.addWidget(self.scanBtn)
        row.addWidget(self.openBtn)
        row.addWidget(self.closeBtn)
        v.addLayout(row)
        self.dllLabel = CaptionLabel(cc.dll_info(), card)
        self.dllLabel.setWordWrap(True)
        self.infoLabel = CaptionLabel("未打开", card)
        self.infoLabel.setWordWrap(True)
        v.addWidget(self.dllLabel)
        v.addWidget(self.infoLabel)
        return card

    def _scan(self, notify=False):
        if self._scan_busy:
            return
        self._scan_busy = True
        if notify:
            self.scanBtn.setEnabled(False)
        self._req("scan", cb=lambda ok, data: self._on_scan(ok, data, notify))

    @staticmethod
    def _device_list_signature(devices):
        return tuple(
            (d.get("index"), d.get("chip_mode"), d.get("func_desc"),
             d.get("chip_type_name"))
            for d in devices)

    def _device_tick(self):
        """检测设备列表变化，只有插拔发生时才刷新下拉框。"""
        if (not self.isVisible() or self._opened
                or "scanBtn" not in self.__dict__
                or self._scan_busy):
            return
        self._scan()

    def _on_scan(self, ok, data, notify=False):
        self._scan_busy = False
        if notify:
            self.scanBtn.setEnabled(True)
        if not ok:
            self.dllLabel.setText(str(data))
            if notify:
                self._err("扫描失败", data)
            return
        self.dllLabel.setText(data.get("library") or "")
        devices = data.get("devices") or []
        signature = self._device_list_signature(devices)
        changed = signature != self._device_signature
        self._device_signature = signature
        if not changed and not notify:
            return
        self._devices = devices
        cur = self.deviceCombo.currentData()
        self.deviceCombo.clear()
        for d in self._devices:
            mode = d["chip_mode"]
            self.deviceCombo.addItem(
                f"{d['index']}# {d['func_desc'] or d['chip_type_name']}"
                f"（Mode{mode}）", userData=d["index"])
        if not self._devices:
            InfoBar.info(title="未发现设备", content="扫描 0~15 索引无可用设备",
                         duration=3000, parent=self)
        elif cur is not None:
            i = self.deviceCombo.findData(cur)
            if i >= 0:
                self.deviceCombo.setCurrentIndex(i)

    def _open(self):
        idx = self.deviceCombo.currentData()
        if idx is None:
            InfoBar.warning(title="未选择设备", content="请先扫描并选择设备",
                            duration=3000, parent=self)
            return
        self.openBtn.setEnabled(False)
        self._req("open", params={"index": int(idx)}, cb=self._on_opened)

    def _on_opened(self, ok, data):
        self.openBtn.setEnabled(True)
        if not ok:
            self._err("打开失败", data)
            return
        self._opened = True
        self._devinfo = data
        d = data
        self.infoLabel.setText(
            f"已打开 {d['index']}#：{d['chip_type_name']}  "
            f"{d['func_desc']}  ChipMode={d['chip_mode']}  "
            f"固件 v{d['fw_ver'] / 10:.1f}  {'USB-HS' if d['hs'] else 'USB-FS'}"
            f"  {d['device_id']}")
        self.openBtn.setEnabled(False)
        self.closeBtn.setEnabled(True)
        self.deviceCombo.setEnabled(False)
        self.scanBtn.setEnabled(False)

    def _close(self):
        def done(ok, data):
            self._set_closed()
            if not ok:
                self._err("关闭失败", data)
        self._req("close", cb=done)

    def _set_closed(self):
        self._opened = False
        self._devinfo = {}
        self.infoLabel.setText("未打开")
        self.openBtn.setEnabled(True)
        self.closeBtn.setEnabled(False)
        self.deviceCombo.setEnabled(True)
        self.scanBtn.setEnabled(True)

    # ── 小工具 ─────────────────────────────────────────────────

    def _log(self, view: SearchablePlainTextEdit, text: str):
        view.appendHtml(
            f'<span style="color:#4fc3f7;">&gt;&gt;</span> {text}')

    def _out(self, view: SearchablePlainTextEdit, title: str, data: dict,
             hex_key: str = "hex"):
        view.appendHtml(f'<span style="color:#4fc3f7;">{title} '
                        f'({data.get("length", "?")} B)</span>')
        view.appendPlainText(self._hexfmt(data.get(hex_key, "")))

    def _make_view(self, parent) -> SearchablePlainTextEdit:
        v = SearchablePlainTextEdit(parent)
        v.setReadOnly(True)
        setup_log_view(v)
        v.setMinimumHeight(140)
        return v

    def _scroll(self, widget) -> SingleDirectionScrollArea:
        s = SingleDirectionScrollArea(self)
        s.setWidget(widget)
        s.setWidgetResizable(True)
        s.enableTransparentBackground()
        s.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return s

    # ── SPI 子页 ───────────────────────────────────────────────

    def _build_spi_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 8, 0, 0)
        v.setSpacing(10)

        cfg = CardWidget()
        cv = QVBoxLayout(cfg)
        cv.setContentsMargins(16, 12, 16, 12)
        cv.setSpacing(8)
        cv.addWidget(SubtitleLabel("SPI 接口", cfg))
        r1 = QHBoxLayout()
        self.spiMode = ComboBox(cfg)
        for i in range(4):
            self.spiMode.addItem(f"Mode{i}", userData=i)
        self.spiClk = ComboBox(cfg)
        for i, n in enumerate(cc.SPI_CLK_NAMES):
            self.spiClk.addItem(n, userData=i)
        self.spiClk.setCurrentIndex(2)          # 默认 15MHz 稳妥
        self.spiOrder = ComboBox(cfg)
        self.spiOrder.addItem("MSB 先行", userData=1)
        self.spiOrder.addItem("LSB 先行", userData=0)
        self.spiCs = ComboBox(cfg)
        self.spiCs.addItem("CS1", userData=0)
        self.spiCs.addItem("CS2", userData=1)
        r1.addWidget(BodyLabel("模式", cfg))
        r1.addWidget(self.spiMode, 1)
        r1.addWidget(BodyLabel("时钟", cfg))
        r1.addWidget(self.spiClk, 1)
        r1.addWidget(BodyLabel("字节序", cfg))
        r1.addWidget(self.spiOrder, 1)
        r1.addWidget(BodyLabel("片选", cfg))
        r1.addWidget(self.spiCs, 1)
        cv.addLayout(r1)
        r2 = QHBoxLayout()
        self.spiCsEnable = CheckBox("使用硬件片选", cfg)
        self.spiCsEnable.setChecked(True)
        self.spiAutoDeact = CheckBox("传输后自动撤销", cfg)
        self.spiAutoDeact.setChecked(True)
        self.spiActDelay = SpinBox(cfg)
        self.spiActDelay.setRange(0, 65535)
        self.spiDeactDelay = SpinBox(cfg)
        self.spiDeactDelay.setRange(0, 65535)
        r2.addWidget(self.spiCsEnable)
        r2.addWidget(self.spiAutoDeact)
        r2.addWidget(BodyLabel("建立延时us", cfg))
        r2.addWidget(self.spiActDelay, 1)
        r2.addWidget(BodyLabel("撤销延时us", cfg))
        r2.addWidget(self.spiDeactDelay, 1)
        self.spiInitBtn = PrimaryPushButton("初始化 SPI", cfg)
        self.spiInitBtn.clicked.connect(self._spi_init)
        r2.addWidget(self.spiInitBtn)
        cv.addLayout(r2)
        v.addWidget(cfg)

        xfer = CardWidget()
        xv = QVBoxLayout(xfer)
        xv.setContentsMargins(16, 12, 16, 12)
        xv.setSpacing(8)
        xv.addWidget(SubtitleLabel("数据传输", xfer))
        self.spiTx = PlainTextEdit(xfer)
        self.spiTx.setPlaceholderText("发送 HEX：AA 55 01 02（可留空做纯时钟读）")
        self.spiTx.setMaximumHeight(64)
        xv.addWidget(self.spiTx)
        row = QHBoxLayout()
        row.addWidget(BodyLabel("读取长度", xfer))
        self.spiRxLen = SpinBox(xfer)
        self.spiRxLen.setRange(0, 65535)
        # 默认 0 = 纯交换：只交换发送框里的字节，与原厂 StreamSpi 行为一致
        # （原厂 OutData(2):AA BB → InData(2):AA BB）。需要「先写后读」时
        # 填非 0，会在发送数据之后再交换相应字节数用于接收。
        self.spiRxLen.setValue(0)
        self.spiRxLen.setToolTip(
            "0 = 仅交换发送框内容（同原厂 StreamSpi）；\n"
            "非 0 = 发送后再交换该字节数用于接收（如 Flash 读时序）")
        row.addWidget(self.spiRxLen)
        # 读阶段填充字节：SPI 全双工，要收时钟就必须发数据；读阶段发什么、
        # 短接回环就收回什么。默认 00 会让回环显示全 0（那是发出的 dummy，
        # 不是故障），自测回环时改成 FF/AA 更直观。
        row.addWidget(BodyLabel("读填充", xfer))
        self.spiDummy = LineEdit(xfer)
        self.spiDummy.setText("00")
        self.spiDummy.setFixedWidth(52)
        self.spiDummy.setToolTip(
            "读阶段（超出发送数据的部分）发出的填充字节(HEX)。"
            "SPI 全双工：要收时钟就必须发数据")
        row.addWidget(self.spiDummy)
        for text, fn in (("读 & 写", self._spi_write_read),
                         ("批量读", self._spi_bulk_read),
                         ("批量写", self._spi_bulk_write)):
            b = PushButton(text, xfer)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        xv.addLayout(row)
        self.spiOut = self._make_view(xfer)
        xv.addWidget(self.spiOut)
        v.addWidget(xfer)
        v.addStretch(1)
        return self._scroll(page)

    def _spi_params(self) -> dict:
        return {
            "mode": self.spiMode.currentData(),
            "clk": self.spiClk.currentData(),
            "msb_first": bool(self.spiOrder.currentData()),
            "cs_index": self.spiCs.currentData(),
            "cs_enable": self.spiCsEnable.isChecked(),
            "auto_deactive_cs": self.spiAutoDeact.isChecked(),
            "active_delay_us": self.spiActDelay.value(),
            "delay_deactive_us": self.spiDeactDelay.value(),
        }

    def _spi_init(self):
        if not self._need_ready("SPI 初始化"):
            return
        def done(ok, data):
            if ok:
                p = self._spi_params()
                self._log(self.spiOut,
                          f"SPI 初始化成功：Mode{p['mode']} "
                          f"{cc.SPI_CLK_NAMES[p['clk']]} "
                          f"{'MSB' if p['msb_first'] else 'LSB'}")
            else:
                self._err("SPI 初始化失败", data)
        self._req("spi_init", cb=done, params=self._spi_params())

    def _spi_tx_bytes(self):
        return self.spiTx.toPlainText().strip()

    def _spi_write_read(self):
        self._spi_xfer("读&写")

    def _spi_bulk_read(self):
        if not self.spiRxLen.value():
            self._err("批量读", "请先填写读取长度")
            return
        self._spi_xfer("批量读", tx="")

    def _spi_bulk_write(self):
        self._spi_xfer("批量写", rx=0)

    def _spi_dummy_byte(self) -> int:
        """读阶段填充字节；非法输入回退 0，不因此中断传输。"""
        text = self.spiDummy.text().strip() or "00"
        try:
            return int(text, 16) & 0xFF
        except ValueError:
            return 0

    def _spi_xfer(self, label, tx=None, rx=None):
        if not self._need_ready(label):
            return
        dummy = self._spi_dummy_byte()
        tx_hex = self._spi_tx_bytes() if tx is None else tx
        rx_len = self.spiRxLen.value() if rx is None else rx
        params = {"tx_hex": tx_hex, "rx_len": rx_len, "dummy": dummy}

        def done(ok, data):
            if not ok:
                self._err(f"SPI {label}失败", data)
                return
            # 对齐原厂 Demo 的呈现：OutData（发出）+ InData（整块回读）。
            # CH347 的 SPI 是「一次交换 N 字节」，InData 内含发送阶段的
            # 回读——短接回环时前段就等于 OutData。
            out_hex = data.get("tx_len", 0)
            if tx_hex.strip():
                self._out(self.spiOut, f"{label} OutData",
                          {"length": data.get("tx_len", 0),
                           "hex": tx_hex.replace(" ", "").replace("\n", "")})
            in_hex = data.get("in_hex") or data.get("rx_hex", "")
            if in_hex:
                self._out(self.spiOut, f"{label} InData",
                          {"length": len(in_hex) // 2, "hex": in_hex})
            else:
                self._log(self.spiOut, f"{label} 完成（{out_hex}B 已发送）")
        self._req("spi_xfer", cb=done, params=params)

    # ── I2C 子页 ───────────────────────────────────────────────

    def _build_i2c_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 8, 0, 0)
        v.setSpacing(10)

        cfg = CardWidget()
        cv = QVBoxLayout(cfg)
        cv.setContentsMargins(16, 12, 16, 12)
        cv.setSpacing(8)
        cv.addWidget(SubtitleLabel("I2C 接口", cfg))
        row = QHBoxLayout()
        self.i2cSpeed = ComboBox(cfg)
        for name, code in cc.I2C_SPEEDS:
            self.i2cSpeed.addItem(name, userData=code)
        self.i2cSpeed.setCurrentIndex(2)        # 默认 400KHz
        self.i2cStretch = ComboBox(cfg)
        self.i2cStretch.addItem("关闭", userData=False)
        self.i2cStretch.addItem("启用", userData=True)
        self.i2cDelay = SpinBox(cfg)
        self.i2cDelay.setRange(0, 1000)
        row.addWidget(BodyLabel("速率", cfg))
        row.addWidget(self.i2cSpeed, 1)
        row.addWidget(BodyLabel("SCL 拉伸", cfg))
        row.addWidget(self.i2cStretch, 1)
        row.addWidget(BodyLabel("操作延时 ms", cfg))
        row.addWidget(self.i2cDelay, 1)
        self.i2cInitBtn = PrimaryPushButton("初始化 I2C", cfg)
        self.i2cInitBtn.clicked.connect(self._i2c_init)
        row.addWidget(self.i2cInitBtn)
        cv.addLayout(row)
        v.addWidget(cfg)

        xfer = CardWidget()
        xv = QVBoxLayout(xfer)
        xv.setContentsMargins(16, 12, 16, 12)
        xv.setSpacing(8)
        xv.addWidget(SubtitleLabel("原始传输", xfer))
        self.i2cTx = PlainTextEdit(xfer)
        self.i2cTx.setPlaceholderText(
            "写入 HEX，首字节为 8bit 器件地址（如 50 00 = 写 0x28 器件寄存器 0）")
        self.i2cTx.setMaximumHeight(64)
        xv.addWidget(self.i2cTx)
        row = QHBoxLayout()
        row.addWidget(BodyLabel("读取长度", xfer))
        self.i2cRxLen = SpinBox(xfer)
        self.i2cRxLen.setRange(0, 4096)
        self.i2cRxLen.setValue(8)
        row.addWidget(self.i2cRxLen)
        btn = PushButton("传输", xfer)
        btn.clicked.connect(self._i2c_xfer)
        row.addWidget(btn)
        row.addStretch(1)
        xv.addLayout(row)
        self.i2cOut = self._make_view(xfer)
        xv.addWidget(self.i2cOut)
        v.addWidget(xfer)
        v.addStretch(1)
        return self._scroll(page)

    def _i2c_init(self):
        if not self._need_ready("I2C 初始化"):
            return
        params = {"speed": self.i2cSpeed.currentData(),
                  "stretch": bool(self.i2cStretch.currentData()),
                  "delay_ms": self.i2cDelay.value()}

        def done(ok, data):
            if ok:
                self._log(self.i2cOut, f"I2C 初始化成功（{params['speed']} 档）")
            else:
                self._err("I2C 初始化失败", data)
        self._req("i2c_init", cb=done, params=params)

    def _i2c_xfer(self):
        if not self._need_ready("I2C 传输"):
            return
        text = self.i2cTx.toPlainText().strip()
        if not text:
            self._err("I2C 传输", "写入数据不能为空（首字节=器件地址）")
            return
        params = {"write_hex": text.replace("\n", " "),
                  "read_len": self.i2cRxLen.value()}

        def done(ok, data):
            if not ok:
                self._err("I2C 传输失败", data)
                return
            self._log(self.i2cOut,
                      f"传输完成，NACK 字节数 {data.get('ack_miss', 0)}")
            if data.get("read_hex"):
                self._out(self.i2cOut, "读取",
                          {"length": len(data["read_hex"]) // 2,
                           "hex": data["read_hex"]})
        self._req("i2c_xfer", cb=done, params=params)

    # ── I2C 器件子页 ───────────────────────────────────────────

    def _build_i2c_dev_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 8, 0, 0)
        v.setSpacing(10)

        scan = CardWidget()
        sv = QHBoxLayout(scan)
        sv.setContentsMargins(16, 12, 16, 12)
        sv.addWidget(SubtitleLabel("在线器件", scan))
        btn = PushButton("地址扫描", scan)
        btn.clicked.connect(self._i2c_scan)
        sv.addWidget(btn)
        self.i2cdevFound = CaptionLabel("未扫描", scan)
        self.i2cdevFound.setWordWrap(True)
        sv.addWidget(self.i2cdevFound, 1)
        v.addWidget(scan)

        reg = CardWidget()
        rv = QVBoxLayout(reg)
        rv.setContentsMargins(16, 12, 16, 12)
        rv.setSpacing(8)
        rv.addWidget(SubtitleLabel("寄存器工具", reg))
        r1 = QHBoxLayout()
        self.i2cdevAddr = LineEdit(reg)
        self.i2cdevAddr.setText("50")
        self.i2cdevAddr.setPlaceholderText("器件地址(7bit HEX)")
        self.i2cdevReg = LineEdit(reg)
        self.i2cdevReg.setText("00")
        self.i2cdevReg.setPlaceholderText("寄存器(HEx)")
        self.i2cdevRegWide = ComboBox(reg)
        self.i2cdevRegWide.addItem("8bit", userData=8)
        self.i2cdevRegWide.addItem("16bit", userData=16)
        self.i2cdevWidth = ComboBox(reg)
        self.i2cdevWidth.addItem("8bit", userData=8)
        self.i2cdevWidth.addItem("16bit", userData=16)
        self.i2cdevCount = SpinBox(reg)
        self.i2cdevCount.setRange(1, 256)
        self.i2cdevCount.setValue(8)
        for lbl, w in (("器件", self.i2cdevAddr), ("寄存器", self.i2cdevReg),
                       ("寄存器宽", self.i2cdevRegWide),
                       ("数据宽", self.i2cdevWidth), ("数量", self.i2cdevCount)):
            r1.addWidget(BodyLabel(lbl, reg))
            r1.addWidget(w, 1)
        rv.addLayout(r1)
        r2 = QHBoxLayout()
        btnR = PushButton("读取", reg)
        btnR.clicked.connect(self._i2c_reg_read)
        self.i2cdevVal = LineEdit(reg)
        self.i2cdevVal.setPlaceholderText("写入值 HEX（如 A0 / 1F 80）")
        btnW = PushButton("写入", reg)
        btnW.clicked.connect(self._i2c_reg_write)
        r2.addWidget(btnR)
        r2.addWidget(self.i2cdevVal, 1)
        r2.addWidget(btnW)
        rv.addLayout(r2)
        self.i2cdevTable = TableWidget(reg)
        self.i2cdevTable.setColumnCount(2)
        self.i2cdevTable.setHorizontalHeaderLabels(["寄存器", "值"])
        self.i2cdevTable.verticalHeader().setVisible(False)
        self.i2cdevTable.setEditTriggers(
            TableWidget.EditTrigger.NoEditTriggers)
        self.i2cdevTable.setMinimumHeight(120)
        rv.addWidget(self.i2cdevTable)
        v.addWidget(reg)

        scr = CardWidget()
        tv = QVBoxLayout(scr)
        tv.setContentsMargins(16, 12, 16, 12)
        tv.setSpacing(8)
        tv.addWidget(SubtitleLabel("初始化脚本（JSON 步骤数组）", scr))
        tip = CaptionLabel(
            '每步：{"addr7":8bit 器件地址含读写位的首字节由 write 提供, '
            '"write_hex":"A0 00", "read_len":1, "delay_ms":0}；'
            "write 首字节 = 地址<<1", scr)
        tip.setWordWrap(True)
        tv.addWidget(tip)
        self.i2cdevScript = PlainTextEdit(scr)
        self.i2cdevScript.setPlaceholderText(
            '[{"write_hex": "A0 00", "read_len": 1, "delay_ms": 1}]')
        self.i2cdevScript.setMinimumHeight(110)
        tv.addWidget(self.i2cdevScript)
        r3 = QHBoxLayout()
        self.i2cdevLoop = SpinBox(scr)
        self.i2cdevLoop.setRange(1, 1000)
        r3.addWidget(BodyLabel("循环次数", scr))
        r3.addWidget(self.i2cdevLoop)
        self.i2cdevScriptName = LineEdit(scr)
        self.i2cdevScriptName.setPlaceholderText("脚本名")
        r3.addWidget(self.i2cdevScriptName, 1)
        self.i2cdevScriptSel = ComboBox(scr)
        r3.addWidget(self.i2cdevScriptSel, 1)
        bSave = PushButton("保存", scr)
        bSave.clicked.connect(self._i2c_script_save)
        bDel = PushButton("删除", scr)
        bDel.clicked.connect(self._i2c_script_del)
        bRun = PrimaryPushButton("执行", scr)
        bRun.clicked.connect(self._i2c_script_run)
        r3.addWidget(bSave)
        r3.addWidget(bDel)
        r3.addWidget(bRun)
        tv.addLayout(r3)
        self.i2cdevOut = self._make_view(scr)
        tv.addWidget(self.i2cdevOut)
        v.addWidget(scr)
        v.addStretch(1)
        self.i2cdevScriptSel.activated.connect(self._i2c_script_sel)
        self._reload_combo(self.i2cdevScriptSel,
                           self._store.get("i2c_scripts") or {})
        return self._scroll(page)

    @staticmethod
    def _reload_combo(combo: ComboBox, mapping: dict):
        cur = combo.currentData()
        combo.clear()
        for k in mapping:
            combo.addItem(k, userData=k)
        if cur is not None:
            i = combo.findData(cur)
            if i >= 0:
                combo.setCurrentIndex(i)

    def _i2c_scan(self):
        if not self._need_ready("地址扫描"):
            return
        self.i2cdevFound.setText("扫描中…（逐地址探测，请稍候）")

        def done(ok, data):
            if not ok:
                self.i2cdevFound.setText("扫描失败")
                self._err("地址扫描失败", data)
                return
            addrs = data.get("addrs") or []
            self.i2cdevFound.setText(
                f"发现 {len(addrs)} 个器件：" + " ".join(addrs)
                if addrs else "未发现 ACK 器件（检查上电/接线/地址）")

        def prog(msg):
            self.i2cdevFound.setText(
                f"扫描中… {msg.get('done', 0)}/{msg.get('total', 0)} 地址")

        self._req("i2c_scan", cb=done, progress=prog)

    def _i2c_reg_params(self):
        try:
            addr = int(self.i2cdevAddr.text().strip() or "0", 16)
            reg = int(self.i2cdevReg.text().strip() or "0", 16)
        except ValueError:
            self._err("寄存器工具", "地址/寄存器须为 HEX")
            return None
        return {"addr": addr, "reg": reg,
                "reg_wide": self.i2cdevRegWide.currentData(),
                "width": self.i2cdevWidth.currentData(),
                "count": self.i2cdevCount.value()}

    def _i2c_reg_read(self):
        if not self._need_ready("寄存器读取"):
            return
        p = self._i2c_reg_params()
        if p is None:
            return

        def done(ok, data):
            if not ok:
                self._err("寄存器读取失败", data)
                return
            rows = data.get("rows") or []
            self.i2cdevTable.setRowCount(len(rows))
            for i, r in enumerate(rows):
                self.i2cdevTable.setItem(
                    i, 0, QTableWidgetItem(f"0x{r['reg']:04X}"))
                self.i2cdevTable.setItem(
                    i, 1, QTableWidgetItem(
                        r["hex"].upper() + ("" if r["ack"] else "  (NACK)")))
        self._req("i2c_reg_read", cb=done, params=p)

    def _i2c_reg_write(self):
        if not self._need_ready("寄存器写入"):
            return
        p = self._i2c_reg_params()
        if p is None:
            return
        val = self.i2cdevVal.text().strip()
        if not val:
            self._err("寄存器写入", "写入值为空")
            return
        p = dict(p)
        p["value_hex"] = val

        def done(ok, data):
            if ok:
                self._log(self.i2cdevOut, "寄存器写入成功")
            else:
                self._err("寄存器写入失败", data)
        self._req("i2c_reg_write", cb=done, params=p)

    def _i2c_script_save(self):
        name = self.i2cdevScriptName.text().strip()
        if not name:
            self._err("保存脚本", "请先填写脚本名")
            return
        text = self.i2cdevScript.toPlainText().strip()
        try:
            steps = json.loads(text or "[]")
            [cc.I2cScriptStep.from_dict(s) for s in steps]
        except (ValueError, TypeError) as e:
            self._err("脚本格式错误", e)
            return
        scripts = self._store.setdefault("i2c_scripts", {})
        scripts[name] = text
        self._persist()
        self._reload_combo(self.i2cdevScriptSel, scripts)
        InfoBar.success(title="已保存", content=f"脚本 {name}", duration=2000,
                        parent=self)

    def _i2c_script_del(self):
        name = self.i2cdevScriptSel.currentData()
        if name:
            (self._store.get("i2c_scripts") or {}).pop(name, None)
            self._persist()
            self._reload_combo(self.i2cdevScriptSel,
                               self._store.get("i2c_scripts") or {})

    def _i2c_script_run(self):
        if not self._need_ready("脚本执行"):
            return
        text = self.i2cdevScript.toPlainText().strip()
        try:
            steps = json.loads(text or "[]")
        except ValueError as e:
            self._err("脚本格式错误", e)
            return

        def done(ok, data):
            if not ok:
                self._err("脚本执行失败", data)
                return
            reads = data.get("reads") or []
            self._log(self.i2cdevOut,
                      f"脚本完成 {data.get('ran_steps', 0)} 步，"
                      f"读回 {len(reads)} 条")
            for h in reads[:16]:
                self.i2cdevOut.appendPlainText(h.upper())
        self._req("i2c_script", cb=done,
                  params={"steps": steps, "loop": self.i2cdevLoop.value()})

    def _i2c_script_sel(self, _text=""):
        name = self.i2cdevScriptSel.currentData()
        text = (self._store.get("i2c_scripts") or {}).get(name)
        if text:
            self.i2cdevScript.setPlainText(text)

    # ── HEX 输入解析 ───────────────────────────────────────────

    def _parse_hex_field(self, text, name, required=True):
        try:
            if not str(text).strip():
                if required:
                    raise ValueError("内容为空")
                return b""
            return cc.parse_hex(text)
        except ValueError as e:
            self._err(name, e)
            return None

    def _parse_hex_int(self, text, name, lo=0, hi=0xFFFFFFFF):
        try:
            v = int(str(text).strip() or "0", 16)
        except ValueError:
            self._err(name, "须为 HEX 整数")
            return None
        if not lo <= v <= hi:
            self._err(name, f"超出范围 {lo}~{hi:#X}")
            return None
        return v

    # ── GPIO 子页 ──────────────────────────────────────────────

    def _build_gpio_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 8, 0, 0)
        v.setSpacing(10)

        card = CardWidget()
        cv = QVBoxLayout(card)
        cv.setContentsMargins(16, 12, 16, 12)
        cv.setSpacing(8)
        header = QHBoxLayout()
        header.addWidget(SubtitleLabel("状态 · GPIO0-7", card))
        header.addStretch(1)
        header.addWidget(CaptionLabel("大号 1/0 = 当前电平 · 灰色 = 已关闭", card))
        cv.addLayout(header)

        self.gpioRows = []
        names = self._store.get("gpio_names") or {}
        pinRow = QHBoxLayout()
        pinRow.setSpacing(4)
        for pin in range(8):
            pinCard = CardWidget(card)
            pv = QVBoxLayout(pinCard)
            pv.setContentsMargins(4, 8, 4, 8)
            pv.setSpacing(6)

            title = BodyLabel(str(pin), pinCard)
            title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pv.addWidget(title)
            alias = LineEdit(pinCard)
            alias.setPlaceholderText(f"GPIO{pin}")
            alias.setText(str(names.get(str(pin), "")))
            alias.setFixedWidth(48)
            alias.setToolTip("引脚别名")
            alias.editingFinished.connect(
                lambda p=pin: self._gpio_name_saved(p))
            # 空别名不占用卡片空间；已有别名仍通过卡片提示保留可见性。
            if alias.text().strip():
                title.setToolTip(alias.text().strip())
            alias.hide()

            en = CheckBox("使能", pinCard)
            en.setChecked(True)
            en.setToolTip("启用该 GPIO 引脚")
            pv.addWidget(en, 0, Qt.AlignmentFlag.AlignHCenter)

            lv = PushButton("0", pinCard)
            lv.setMinimumHeight(96)
            lv.setFont(QFont("Segoe UI", 24, QFont.Weight.Bold))
            lv.setStyleSheet(
                "QPushButton { background: transparent; border: none; "
                "color: #f5f5f5; }"
                "QPushButton:disabled { color: #8a8a8a; }")
            lv.setToolTip("OUT 模式下点击切换输出电平")
            lv.setProperty("level", 0)
            self._gpio_set_level_display(lv, 0)
            lv.clicked.connect(
                lambda _=False, p=pin: self._gpio_toggle(p))
            pv.addWidget(lv, 1)

            dr = SwitchButton(pinCard)
            dr.setOnText("OUT")
            dr.setOffText("IN")
            dr.setFixedWidth(72)
            dr.setToolTip("切换输入/输出模式")
            pv.addWidget(dr, 0, Qt.AlignmentFlag.AlignHCenter)
            pv.setStretch(2, 1)
            pinRow.addWidget(pinCard, 1)
            self.gpioRows.append((en, dr, lv, alias))
            en.stateChanged.connect(
                lambda _state, p=pin: self._gpio_pin_changed(p))
            dr.checkedChanged.connect(
                lambda _checked, p=pin: self._gpio_pin_changed(p))
            self._gpio_update_level_enabled(pin)
        cv.addLayout(pinRow)

        brow = QHBoxLayout()
        bSet = PrimaryPushButton("应用 GPIO", card)
        bSet.clicked.connect(lambda: self._gpio_apply())
        bGet = PushButton("读取状态", card)
        bGet.clicked.connect(self._gpio_read)
        brow.addWidget(bSet)
        brow.addWidget(bGet)
        brow.addStretch(1)
        cv.addLayout(brow)
        v.addWidget(card)

        m = CardWidget()
        mv = QVBoxLayout(m)
        mv.setContentsMargins(16, 12, 16, 12)
        mv.setSpacing(8)
        mv.addWidget(SubtitleLabel("序列宏（引脚/电平/延时，一键时序）", m))
        self.gpioMacroTable = TableWidget(m)
        self.gpioMacroTable.setColumnCount(3)
        self.gpioMacroTable.setHorizontalHeaderLabels(
            ["引脚(0-7)", "电平(0/1)", "延时ms(0-5000)"])
        self.gpioMacroTable.setMinimumHeight(120)
        mv.addWidget(self.gpioMacroTable)
        r1 = QHBoxLayout()
        bAdd = PushButton("添加步", m)
        bAdd.clicked.connect(self._macro_add_row)
        bDelRow = PushButton("删除选中步", m)
        bDelRow.clicked.connect(self._macro_del_row)
        r1.addWidget(bAdd)
        r1.addWidget(bDelRow)
        r1.addStretch(1)
        mv.addLayout(r1)
        r2 = QHBoxLayout()
        self.gpioMacroName = LineEdit(m)
        self.gpioMacroName.setPlaceholderText("宏名称")
        self.gpioMacroSel = ComboBox(m)
        bSave = PushButton("保存", m)
        bSave.clicked.connect(self._macro_save)
        bDel = PushButton("删除", m)
        bDel.clicked.connect(self._macro_delete)
        bRun = PrimaryPushButton("执行", m)
        bRun.clicked.connect(self._macro_run)
        for w in (self.gpioMacroName, self.gpioMacroSel, bSave, bDel, bRun):
            r2.addWidget(w, 1)
        mv.addLayout(r2)
        self.gpioOut = self._make_view(m)
        mv.addWidget(self.gpioOut)
        v.addWidget(m)
        v.addStretch(1)
        self.gpioMacroSel.activated.connect(self._macro_sel)
        self._reload_combo(self.gpioMacroSel,
                           self._store.get("gpio_macros") or {})
        self._gpio_poll_busy = False
        return self._scroll(page)

    def _gpio_name_saved(self, pin):
        alias = self.gpioRows[pin][3].text().strip()
        names = self._store.setdefault("gpio_names", {})
        if alias:
            names[str(pin)] = alias
        else:
            names.pop(str(pin), None)
        self._persist()

    def _gpio_masks(self):
        en = dr = data = 0
        for i, (e, d, lv, _a) in enumerate(self.gpioRows):
            if e.isChecked():
                en |= 1 << i
            if d.isChecked():
                dr |= 1 << i
            if lv.property("level"):
                data |= 1 << i
        return en, dr, data

    def _gpio_update_level_enabled(self, pin):
        en, dr, lv, _a = self.gpioRows[pin]
        lv.setEnabled(en.isChecked() and dr.isChecked())

    @staticmethod
    def _gpio_set_level_display(button, level):
        level = int(bool(level))
        button.setText(f"{level}\n{'HIGH' if level else 'LOW'}")

    def _gpio_pin_changed(self, pin):
        self._gpio_update_level_enabled(pin)
        if self._opened:
            self._gpio_apply(silent=True)

    def _gpio_toggle(self, pin):
        if not self._need_ready("电平切换"):
            return
        _e, d, lv, _a = self.gpioRows[pin]
        if not d.isChecked():
            return
        new = 0 if lv.property("level") else 1
        lv.setProperty("level", new)
        self._gpio_set_level_display(lv, new)
        en, dr, data = self._gpio_masks()
        self._req("gpio_set", params={"enable": en, "dir": dr, "data": data})

    def _gpio_apply(self, silent=False):
        if not self._need_ready("GPIO 输出"):
            return
        en, dr, data = self._gpio_masks()

        def done(ok, err):
            if ok:
                if not silent:
                    self._log(self.gpioOut,
                              f"输出已应用 enable={en:02X} dir={dr:02X} "
                              f"data={data:02X}")
            else:
                self._err("GPIO 设置失败", err)
        self._req("gpio_set", cb=done,
                  params={"enable": en, "dir": dr, "data": data})

    def _gpio_read(self):
        if not self._need_ready("GPIO 读取"):
            return

        def done(ok, data):
            self._gpio_poll_busy = False
            if not ok:
                self._err("GPIO 读取失败", data)
                return
            dr = int(data.get("dir", 0))
            val = int(data.get("data", 0))
            for i, (_e, d, lv, _a) in enumerate(self.gpioRows):
                d.blockSignals(True)
                d.setChecked(bool(dr >> i & 1))
                d.blockSignals(False)
                bit = (val >> i) & 1
                lv.setProperty("level", bit)
                self._gpio_set_level_display(lv, bit)
                self._gpio_update_level_enabled(i)
            self._log(self.gpioOut, f"dir={dr:02X} data={val:02X}")
        self._gpio_poll_busy = True
        self._req("gpio_get", cb=done)

    # 宏编辑：表列 (pin, level, delay)，存 data.json ch347.gpio_macros

    def _macro_add_row(self):
        t = self.gpioMacroTable
        row = t.rowCount()
        t.insertRow(row)
        for c, text in enumerate((str(min(row, 7)), "1", "10")):
            t.setItem(row, c, QTableWidgetItem(text))

    def _macro_del_row(self):
        row = self.gpioMacroTable.currentRow()
        if row >= 0:
            self.gpioMacroTable.removeRow(row)

    def _macro_rows(self):
        t = self.gpioMacroTable
        steps = []
        for i in range(t.rowCount()):
            def txt(c):
                it = t.item(i, c)
                return it.text().strip() if it else ""
            steps.append(cc.GpioMacroStep.from_dict({
                "pin": int(txt(0) or "0", 10),
                "level": int(txt(1) or "0", 10),
                "delay_ms": int(txt(2) or "0", 10)}).to_dict())
        return steps

    def _macro_save(self):
        name = self.gpioMacroName.text().strip()
        if not name:
            self._err("保存宏", "请先填写宏名称")
            return
        try:
            steps = self._macro_rows()
        except (ValueError, TypeError) as e:
            self._err("宏内容非法", e)
            return
        if not steps:
            self._err("保存宏", "表为空")
            return
        macros = self._store.setdefault("gpio_macros", {})
        macros[name] = steps
        self._persist()
        self._reload_combo(self.gpioMacroSel, macros)
        InfoBar.success(title="已保存", content=f"宏 {name} 共 {len(steps)} 步",
                        duration=2000, parent=self)

    def _macro_delete(self):
        name = self.gpioMacroSel.currentData()
        if name:
            (self._store.get("gpio_macros") or {}).pop(name, None)
            self._persist()
            self._reload_combo(self.gpioMacroSel,
                               self._store.get("gpio_macros") or {})

    def _macro_sel(self, _text=""):
        name = self.gpioMacroSel.currentData()
        steps = (self._store.get("gpio_macros") or {}).get(name)
        if steps is None:
            return
        t = self.gpioMacroTable
        t.setRowCount(0)
        for st in steps:
            row = t.rowCount()
            t.insertRow(row)
            for c, key in enumerate(("pin", "level", "delay_ms")):
                t.setItem(row, c, QTableWidgetItem(str(st[key])))
        self.gpioMacroName.setText(name)

    def _macro_run(self):
        if not self._need_ready("宏执行"):
            return
        try:
            steps = self._macro_rows()
        except (ValueError, TypeError) as e:
            self._err("宏内容非法", e)
            return
        if not steps:
            self._err("宏执行", "表为空，可先从列表载入")
            return

        def done(ok, data):
            if ok:
                self._log(self.gpioOut, f"宏执行完成（{data.get('ran', 0)} 步）")
            else:
                self._err("宏执行失败", data)
        self._req("gpio_macro", cb=done, params={"steps": steps})

    # ── Flash 子页 ─────────────────────────────────────────────

    def _build_flash_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 8, 0, 0)
        v.setSpacing(10)

        idc = CardWidget()
        iv = QHBoxLayout(idc)
        iv.setContentsMargins(16, 12, 16, 12)
        bId = PrimaryPushButton("识别芯片", idc)
        bId.clicked.connect(self._flash_identify)
        iv.addWidget(bId)
        self.flashIdLabel = CaptionLabel("未识别", idc)
        self.flashIdLabel.setWordWrap(True)
        iv.addWidget(self.flashIdLabel, 1)
        bSt = PushButton("刷新状态", idc)
        bSt.clicked.connect(self._flash_status)
        iv.addWidget(bSt)
        self.flashStatusLabel = CaptionLabel("SR: -", idc)
        iv.addWidget(self.flashStatusLabel)
        v.addWidget(idc)

        ops = CardWidget()
        ov = QVBoxLayout(ops)
        ov.setContentsMargins(16, 12, 16, 12)
        ov.setSpacing(8)
        ov.addWidget(SubtitleLabel("区域操作", ops))
        r1 = QHBoxLayout()
        self.flashAddr = LineEdit(ops)
        self.flashAddr.setText("0")
        self.flashAddr.setPlaceholderText("起始地址 HEX")
        self.flashLen = LineEdit(ops)
        self.flashLen.setText("100")
        self.flashLen.setPlaceholderText("长度 HEX")
        self.flashFast = CheckBox("快读(0x0B)", ops)
        bRd = PushButton("读取", ops)
        bRd.clicked.connect(self._flash_read)
        bRdFile = PushButton("读取存文件", ops)
        bRdFile.clicked.connect(self._flash_read_file)
        r1.addWidget(BodyLabel("地址", ops))
        r1.addWidget(self.flashAddr, 1)
        r1.addWidget(BodyLabel("长度", ops))
        r1.addWidget(self.flashLen, 1)
        r1.addWidget(self.flashFast)
        r1.addWidget(bRd)
        r1.addWidget(bRdFile)
        ov.addLayout(r1)
        r2 = QHBoxLayout()
        self.flashGran = ComboBox(ops)
        self.flashGran.addItem("扇区 4K", userData=4096)
        self.flashGran.addItem("块 32K", userData=32768)
        self.flashGran.addItem("块 64K", userData=65536)
        self.flashGran.addItem("全片擦除", userData=0)
        bEra = PushButton("擦除", ops)
        bEra.clicked.connect(self._flash_erase)
        bBl = PushButton("空白检查", ops)
        bBl.clicked.connect(self._flash_blank)
        r2.addWidget(BodyLabel("擦除粒度", ops))
        r2.addWidget(self.flashGran, 1)
        r2.addWidget(bEra)
        r2.addWidget(bBl)
        r2.addStretch(1)
        ov.addLayout(r2)
        v.addWidget(ops)

        datac = CardWidget()
        dv = QVBoxLayout(datac)
        dv.setContentsMargins(16, 12, 16, 12)
        dv.setSpacing(8)
        dv.addWidget(SubtitleLabel("数据（先擦后写，首尾扇区整块擦）", datac))
        self.flashData = PlainTextEdit(datac)
        self.flashData.setPlaceholderText("HEX 数据，如 AA 55 …（或由读取结果回填）")
        self.flashData.setMinimumHeight(72)
        dv.addWidget(self.flashData)
        r3 = QHBoxLayout()
        bWr = PushButton("写入数据区", datac)
        bWr.clicked.connect(self._flash_write_data)
        bWrFile = PushButton("写入文件", datac)
        bWrFile.clicked.connect(self._flash_write_file)
        bVf = PushButton("校验文件", datac)
        bVf.clicked.connect(self._flash_verify)
        r3.addWidget(bWr)
        r3.addWidget(bWrFile)
        r3.addWidget(bVf)
        r3.addStretch(1)
        dv.addLayout(r3)
        self.flashBar = ProgressBar(datac)
        self.flashBar.setRange(0, 100)
        self.flashBar.setValue(0)
        dv.addWidget(self.flashBar)
        self.flashOut = self._make_view(datac)
        dv.addWidget(self.flashOut)
        v.addWidget(datac)
        v.addStretch(1)
        return self._scroll(page)

    def _flash_addr_len(self):
        addr = self._parse_hex_int(self.flashAddr.text(), "起始地址")
        if addr is None:
            return None, None
        length = self._parse_hex_int(self.flashLen.text(), "长度", lo=1)
        if length is None:
            return None, None
        return addr, length

    def _flash_prog(self, msg):
        bar = self.flashBar
        total = max(1, int(msg.get("total", 1)))
        if bar.maximum() != total:
            bar.setRange(0, total)
        bar.setValue(int(msg.get("done", 0)))

    def _flash_done(self, label):
        self.flashBar.setRange(0, 100)
        self.flashBar.setValue(100)
        QTimer.singleShot(800, lambda: self.flashBar.setValue(0))
        self._log(self.flashOut, label)
        self._flash_status()

    def _flash_identify(self):
        if not self._need_ready("芯片识别"):
            return

        def done(ok, data):
            if not ok:
                self._err("识别失败", data)
                self.flashIdLabel.setText("未识别")
                return
            cap = data.get("capacity")
            cap_s = (f"  容量 {cap // 1024}KB" if cap else
                     "  容量未识别")
            self.flashIdLabel.setText(
                f"ID {data.get('id_hex')}  {data.get('vendor')}  "
                f"{data.get('model') or '?'}{cap_s}")
        self._req("flash_identify", cb=done)

    def _flash_status(self):
        if not self._need_ready("状态读取"):
            return

        def done(ok, data):
            if ok:
                self.flashStatusLabel.setText(
                    f"SR=0x{data['sr']:02X} "
                    f"BUSY={int(data['busy'])} WEL={int(data['wel'])}")
        self._req("flash_status", cb=done)

    def _flash_read_common(self, to_file=False):
        if not self._need_ready("Flash 读取"):
            return
        addr, length = self._flash_addr_len()
        if addr is None:
            return
        params = {"addr": addr, "len": length,
                  "fast": self.flashFast.isChecked()}
        if to_file:
            path, _ = QFileDialog.getSaveFileName(
                self, "保存 Flash 数据", "flash.bin", "Binary (*.bin);;所有文件 (*)")
            if not path:
                return
            params["file"] = path

        def done(ok, data):
            if not ok:
                self._err("Flash 读取失败", data)
                self.flashBar.setRange(0, 100)
                self.flashBar.setValue(0)
                return
            if "file" in data:
                self._flash_done(f"已保存 {data['length']}B → {data['file']}")
            else:
                self._out(self.flashOut, f"读取 @0x{addr:X}", data)
                self.flashData.setPlainText(
                    cc.format_hex(bytes.fromhex(data.get("hex", ""))))
                self.flashBar.setRange(0, 100)
                self.flashBar.setValue(100)
        self.flashBar.setRange(0, 1)
        self._req("flash_read", cb=done, params=params,
                  progress=self._flash_prog)

    def _flash_read(self):
        self._flash_read_common(False)

    def _flash_read_file(self):
        self._flash_read_common(True)

    def _flash_erase(self):
        if not self._need_ready("Flash 擦除"):
            return
        gran = self.flashGran.currentData()
        addr, length = self._flash_addr_len()
        if addr is None:
            return
        if gran == 0:
            box = MessageBox("全片擦除",
                             "将擦除整个 Flash 芯片内容（不可恢复），"
                             "确认继续？", self.window())
            box.yesButton.setText("确认擦除")
            box.cancelButton.setText("取消")
            if not box.exec():
                return
            length = 1

        def done(ok, data):
            if ok:
                self._flash_done(
                    f"擦除完成：{data.get('blocks', 0)} 块")
            else:
                self._err("擦除失败", data)
                self.flashBar.setRange(0, 100)
                self.flashBar.setValue(0)
        self.flashBar.setRange(0, 1)
        self._req("flash_erase", cb=done,
                  params={"addr": addr, "len": length, "gran": gran},
                  progress=self._flash_prog)

    def _flash_blank(self):
        if not self._need_ready("空白检查"):
            return
        addr, length = self._flash_addr_len()
        if addr is None:
            return

        def done(ok, data):
            if not ok:
                self._err("空白检查失败", data)
                return
            if data.get("blank"):
                self._flash_done(f"[0x{addr:X},+0x{length:X}) 全空白")
            else:
                self._log(self.flashOut,
                          f"非空白，首个非 0xFF 地址 "
                          f"0x{data.get('first'):X}")
                self.flashBar.setValue(0)
        self.flashBar.setRange(0, 1)
        self._req("flash_blank", cb=done,
                  params={"addr": addr, "len": length},
                  progress=self._flash_prog)

    def _flash_write_data(self):
        if not self._need_ready("Flash 写入"):
            return
        addr, _len = self._flash_addr_len()
        if addr is None:
            return
        data = self._parse_hex_field(self.flashData.toPlainText(), "写入数据")
        if data is None:
            return

        def done(ok, res):
            if ok:
                self._flash_done(f"写入完成 {res.get('written', 0)}B")
            else:
                self._err("写入失败", res)
                self.flashBar.setRange(0, 100)
                self.flashBar.setValue(0)
        self.flashBar.setRange(0, 1)
        self._req("flash_write", cb=done,
                  params={"addr": addr, "data_hex": data.hex()},
                  progress=self._flash_prog)

    def _flash_write_file(self):
        if not self._need_ready("文件写入"):
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "选择固件", "", "Binary (*.bin *.img);;所有文件 (*)")
        if not path:
            return
        addr, _len = self._flash_addr_len()
        if addr is None:
            return

        def done(ok, res):
            if ok:
                self._flash_done(f"文件写入完成 {res.get('written', 0)}B")
            else:
                self._err("文件写入失败", res)
                self.flashBar.setRange(0, 100)
                self.flashBar.setValue(0)
        self.flashBar.setRange(0, 1)
        self._req("flash_write", cb=done, params={"addr": addr, "file": path},
                  progress=self._flash_prog)

    def _flash_verify(self):
        if not self._need_ready("校验"):
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "选择校验文件", "", "Binary (*.bin *.img);;所有文件 (*)")
        if not path:
            return
        addr = self._parse_hex_int(self.flashAddr.text(), "起始地址")
        if addr is None:
            return

        def done(ok, res):
            if not ok:
                self._err("校验失败", res)
                return
            if res.get("ok"):
                self._flash_done(f"校验通过（{res.get('length', 0)}B）")
            else:
                self._log(self.flashOut,
                          f"校验不一致，首个差异 0x{res.get('first_diff'):X}")
                self.flashBar.setValue(0)
        self.flashBar.setRange(0, 1)
        self._req("flash_verify", cb=done,
                  params={"addr": addr, "file": path},
                  progress=self._flash_prog)

    # ── EEPROM 子页 ────────────────────────────────────────────

    def _build_eeprom_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 8, 0, 0)
        v.setSpacing(10)
        card = CardWidget()
        cv = QVBoxLayout(card)
        cv.setContentsMargins(16, 12, 16, 12)
        cv.setSpacing(8)
        cv.addWidget(SubtitleLabel("I2C EEPROM（24Cxx，经 DLL 内置驱动）", card))
        r1 = QHBoxLayout()
        self.eepModel = ComboBox(card)
        for m in cc.EEPROM_MODELS:
            self.eepModel.addItem(m["name"], userData=m["name"])
        self.eepModel.setCurrentIndex(1)          # 默认 24C02
        self.eepAddr = LineEdit(card)
        self.eepAddr.setText("0")
        self.eepLen = SpinBox(card)
        self.eepLen.setRange(1, 65536)
        self.eepLen.setValue(16)
        r1.addWidget(BodyLabel("型号", card))
        r1.addWidget(self.eepModel, 1)
        r1.addWidget(BodyLabel("地址 HEX", card))
        r1.addWidget(self.eepAddr, 1)
        r1.addWidget(BodyLabel("长度", card))
        r1.addWidget(self.eepLen, 1)
        cv.addLayout(r1)
        self.eepData = PlainTextEdit(card)
        self.eepData.setPlaceholderText("写入 HEX 数据（读取成功后自动回填）")
        self.eepData.setMinimumHeight(80)
        cv.addWidget(self.eepData)
        r2 = QHBoxLayout()
        bRd = PrimaryPushButton("读取", card)
        bRd.clicked.connect(self._eep_read)
        bWr = PushButton("写入", card)
        bWr.clicked.connect(self._eep_write)
        r2.addWidget(bRd)
        r2.addWidget(bWr)
        r2.addStretch(1)
        cv.addLayout(r2)
        self.eepOut = self._make_view(card)
        cv.addWidget(self.eepOut)
        v.addWidget(card)
        v.addStretch(1)
        return self._scroll(page)

    def _eep_common(self):
        if not self._need_ready("EEPROM 操作"):
            return None
        addr = self._parse_hex_int(self.eepAddr.text(), "地址")
        if addr is None:
            return None
        return {"model": self.eepModel.currentData(), "addr": addr}

    def _eep_read(self):
        p = self._eep_common()
        if p is None:
            return
        p = dict(p)
        p["len"] = self.eepLen.value()

        def done(ok, data):
            if not ok:
                self._err("EEPROM 读取失败", data)
                return
            self._out(self.eepOut, "读取", data)
            self.eepData.setPlainText(cc.format_hex(
                bytes.fromhex(data.get("hex", ""))))
        self._req("eeprom_read", cb=done, params=p)

    def _eep_write(self):
        p = self._eep_common()
        if p is None:
            return
        data = self._parse_hex_field(self.eepData.toPlainText(), "写入数据")
        if data is None:
            return
        p = dict(p)
        p["data_hex"] = data.hex()

        def done(ok, res):
            if ok:
                self._log(self.eepOut, f"写入完成 {res.get('written', 0)}B")
            else:
                self._err("EEPROM 写入失败", res)
        self._req("eeprom_write", cb=done, params=p)

    # ── LCD 屏幕子页 ───────────────────────────────────────────

    def _build_lcd_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 8, 0, 0)
        v.setSpacing(10)

        cfg = CardWidget()
        cv = QVBoxLayout(cfg)
        cv.setContentsMargins(16, 12, 16, 12)
        cv.setSpacing(8)
        cv.addWidget(SubtitleLabel("屏幕配置", cfg))
        r1 = QHBoxLayout()
        self.lcdTmpl = ComboBox(cfg)
        self.lcdTmpl.addItem("ST7789 240x320", userData="st7789")
        self.lcdTmpl.addItem("ILI9341 240x320", userData="ili9341")
        self.lcdTmpl.addItem("自定义", userData="custom")
        self.lcdW = SpinBox(cfg)
        self.lcdW.setRange(8, 480)
        self.lcdW.setValue(240)
        self.lcdH = SpinBox(cfg)
        self.lcdH.setRange(8, 480)
        self.lcdH.setValue(320)
        self.lcdRgb = ComboBox(cfg)
        self.lcdRgb.addItem("RGB", userData="RGB")
        self.lcdRgb.addItem("BGR", userData="BGR")
        self.lcdMx = CheckBox("X 镜像", cfg)
        self.lcdMy = CheckBox("Y 镜像", cfg)
        r1.addWidget(BodyLabel("模板", cfg))
        r1.addWidget(self.lcdTmpl, 2)
        r1.addWidget(BodyLabel("宽", cfg))
        r1.addWidget(self.lcdW, 1)
        r1.addWidget(BodyLabel("高", cfg))
        r1.addWidget(self.lcdH, 1)
        r1.addWidget(self.lcdRgb, 1)
        r1.addWidget(self.lcdMx)
        r1.addWidget(self.lcdMy)
        cv.addLayout(r1)
        r2 = QHBoxLayout()
        self.lcdDc = ComboBox(cfg)
        self.lcdRes = ComboBox(cfg)
        self.lcdBlk = ComboBox(cfg)
        self.lcdRes.addItem("不使用", userData=-1)
        self.lcdBlk.addItem("不使用", userData=-1)
        for p in range(8):
            self.lcdDc.addItem(f"GPIO{p}", userData=p)
            self.lcdRes.addItem(f"GPIO{p}", userData=p)
            self.lcdBlk.addItem(f"GPIO{p}", userData=p)
        self.lcdDc.setCurrentIndex(1)            # 默认 DC=GPIO0
        self.lcdRes.setCurrentIndex(2)           # 默认 RES=GPIO1
        self.lcdClk = ComboBox(cfg)
        for i, n in enumerate(cc.SPI_CLK_NAMES):
            self.lcdClk.addItem(n, userData=i)
        self.lcdClk.setCurrentIndex(2)
        r2.addWidget(BodyLabel("DC", cfg))
        r2.addWidget(self.lcdDc, 1)
        r2.addWidget(BodyLabel("RES", cfg))
        r2.addWidget(self.lcdRes, 1)
        r2.addWidget(BodyLabel("BLK", cfg))
        r2.addWidget(self.lcdBlk, 1)
        r2.addWidget(BodyLabel("SPI 时钟", cfg))
        r2.addWidget(self.lcdClk, 1)
        cv.addLayout(r2)
        r3 = QHBoxLayout()
        self.lcdProfileName = LineEdit(cfg)
        self.lcdProfileName.setPlaceholderText("配置名")
        self.lcdProfileSel = ComboBox(cfg)
        bSave = PushButton("保存", cfg)
        bSave.clicked.connect(self._lcd_save_profile)
        bDel = PushButton("删除", cfg)
        bDel.clicked.connect(self._lcd_del_profile)
        self.lcdInitBtn = PrimaryPushButton("发送初始化", cfg)
        self.lcdInitBtn.clicked.connect(self._lcd_init)
        for w in (self.lcdProfileName, self.lcdProfileSel, bSave, bDel,
                  self.lcdInitBtn):
            r3.addWidget(w, 1)
        cv.addLayout(r3)
        v.addWidget(cfg)

        steps = CardWidget()
        sv = QVBoxLayout(steps)
        sv.setContentsMargins(16, 12, 16, 12)
        sv.setSpacing(8)
        sv.addWidget(SubtitleLabel("初始化序列（JSON：kind=cmd/data/delay）",
                                   steps))
        self.lcdSeqEdit = PlainTextEdit(steps)
        self.lcdSeqEdit.setMinimumHeight(110)
        sv.addWidget(self.lcdSeqEdit)
        v.addWidget(steps)

        disp = CardWidget()
        dv = QVBoxLayout(disp)
        dv.setContentsMargins(16, 12, 16, 12)
        dv.setSpacing(8)
        dv.addWidget(SubtitleLabel("显示", disp))
        r4 = QHBoxLayout()
        self.lcdColorBtn = PushButton("选色", disp)
        self.lcdColorBtn.clicked.connect(self._lcd_pick_color)
        self._lcd_color = QColor(0, 128, 255)
        self._update_swatch()
        r4.addWidget(self.lcdColorBtn)
        self.lcdX = SpinBox(disp)
        self.lcdY = SpinBox(disp)
        self.lcdFW = SpinBox(disp)
        self.lcdFH = SpinBox(disp)
        for sp in (self.lcdX, self.lcdY, self.lcdFW, self.lcdFH):
            sp.setRange(0, 480)
        self.lcdFW.setValue(240)
        self.lcdFH.setValue(320)
        r4.addWidget(BodyLabel("x", disp))
        r4.addWidget(self.lcdX)
        r4.addWidget(BodyLabel("y", disp))
        r4.addWidget(self.lcdY)
        r4.addWidget(BodyLabel("宽", disp))
        r4.addWidget(self.lcdFW)
        r4.addWidget(BodyLabel("高", disp))
        r4.addWidget(self.lcdFH)
        bFill = PushButton("纯色填充", disp)
        bFill.clicked.connect(self._lcd_fill)
        r4.addWidget(bFill)
        dv.addLayout(r4)
        r5 = QHBoxLayout()
        self.lcdImgPath = LineEdit(disp)
        self.lcdImgPath.setPlaceholderText("图片文件（png/jpg/bmp）")
        bBrowse = PushButton("浏览", disp)
        bBrowse.clicked.connect(self._lcd_browse)
        self.lcdSwap = CheckBox("字节交换", disp)
        self.lcdSwap.setToolTip("部分屏/驱动颜色字节序相反时勾选")
        bImg = PushButton("发送图片", disp)
        bImg.clicked.connect(self._lcd_image)
        r5.addWidget(self.lcdImgPath, 1)
        r5.addWidget(bBrowse)
        r5.addWidget(self.lcdSwap)
        r5.addWidget(bImg)
        dv.addLayout(r5)
        self.lcdBar = ProgressBar(disp)
        self.lcdBar.setRange(0, 100)
        dv.addWidget(self.lcdBar)
        self.lcdOut = self._make_view(disp)
        dv.addWidget(self.lcdOut)
        v.addWidget(disp)
        v.addStretch(1)

        self.lcdTmpl.currentIndexChanged.connect(self._lcd_tmpl_changed)
        self.lcdProfileSel.activated.connect(self._lcd_profile_sel)
        self._lcd_tmpl_changed()                 # 预填默认模板序列
        self._reload_combo(self.lcdProfileSel,
                           self._store.get("lcd_profiles") or {})
        return self._scroll(page)

    def _update_swatch(self):
        self.lcdColorBtn.setStyleSheet(
            f"background-color: {self._lcd_color.name()};"
            "border: 1px solid #888; border-radius: 4px; min-width: 48px;")

    def _lcd_tmpl_changed(self):
        key = self.lcdTmpl.currentData()
        if key == "custom":
            return
        try:
            steps = [s.to_dict() for s in cc.lcd_template_steps(key)]
        except ValueError:
            return
        self.lcdSeqEdit.setPlainText(
            json.dumps(steps, ensure_ascii=False, indent=0))
        if key == "ili9341":
            self.lcdRgb.setCurrentIndex(1)       # ILI9341 常见为 BGR 面板
            self.lcdSwap.setChecked(True)
        else:
            self.lcdRgb.setCurrentIndex(0)
            self.lcdSwap.setChecked(False)

    def _lcd_profile(self):
        try:
            steps = json.loads(self.lcdSeqEdit.toPlainText().strip() or "[]")
            prof = {
                "name": self.lcdProfileName.text().strip() or "当前",
                "controller": self.lcdTmpl.currentData(),
                "width": self.lcdW.value(), "height": self.lcdH.value(),
                "rgb_order": self.lcdRgb.currentData(),
                "mirror_x": self.lcdMx.isChecked(),
                "mirror_y": self.lcdMy.isChecked(),
                "dc_pin": self.lcdDc.currentData(),
                "res_pin": self.lcdRes.currentData(),
                "blk_pin": self.lcdBlk.currentData(),
                "spi_mode": 0, "spi_clk": self.lcdClk.currentData(),
                "steps": steps,
            }
            cc.LcdProfile.from_dict(prof)        # 就地校验
            return prof
        except (ValueError, TypeError) as e:
            self._err("LCD 配置", e)
            return None

    def _lcd_save_profile(self):
        name = self.lcdProfileName.text().strip()
        if not name:
            self._err("保存配置", "请先填写配置名")
            return
        prof = self._lcd_profile()
        if prof is None:
            return
        profs = self._store.setdefault("lcd_profiles", {})
        profs[name] = prof
        self._persist()
        self._reload_combo(self.lcdProfileSel, profs)
        InfoBar.success(title="已保存", content=f"LCD 配置 {name}",
                        duration=2000, parent=self)

    def _lcd_del_profile(self):
        name = self.lcdProfileSel.currentData()
        if name:
            (self._store.get("lcd_profiles") or {}).pop(name, None)
            self._persist()
            self._reload_combo(self.lcdProfileSel,
                               self._store.get("lcd_profiles") or {})

    def _lcd_profile_sel(self, _text=""):
        name = self.lcdProfileSel.currentData()
        prof = (self._store.get("lcd_profiles") or {}).get(name)
        if not prof:
            return
        self.lcdProfileName.setText(name)
        i = self.lcdTmpl.findData(prof.get("controller", "custom"))
        if i >= 0:
            self.lcdTmpl.setCurrentIndex(i)
        self.lcdW.setValue(int(prof.get("width", 240)))
        self.lcdH.setValue(int(prof.get("height", 320)))
        self.lcdRgb.setCurrentIndex(
            max(0, self.lcdRgb.findData(prof.get("rgb_order", "RGB")) or 0))
        self.lcdMx.setChecked(bool(prof.get("mirror_x")))
        self.lcdMy.setChecked(bool(prof.get("mirror_y")))
        for combo, key in ((self.lcdDc, "dc_pin"), (self.lcdRes, "res_pin"),
                           (self.lcdBlk, "blk_pin"),
                           (self.lcdClk, "spi_clk")):
            i = combo.findData(prof.get(key))
            if i >= 0:
                combo.setCurrentIndex(i)
        self.lcdSeqEdit.setPlainText(
            json.dumps(prof.get("steps") or [], ensure_ascii=False,
                       indent=0))

    def _lcd_init(self):
        if not self._need_ready("LCD 初始化"):
            return
        prof = self._lcd_profile()
        if prof is None:
            return

        def done(ok, data):
            if ok:
                self._log(self.lcdOut,
                          f"初始化完成（{data.get('steps', 0)} 步）")
            else:
                self._err("LCD 初始化失败", data)
        self._req("lcd_init", cb=done, params={"profile": prof})

    def _lcd_pick_color(self):
        c = QColorDialog.getColor(self._lcd_color, self, "选择填充色")
        if c.isValid():
            self._lcd_color = c
            self._update_swatch()

    def _lcd_color565(self) -> int:
        r, g, b = self._lcd_color.red(), self._lcd_color.green(), \
            self._lcd_color.blue()
        return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)

    def _lcd_prog(self, msg):
        bar = self.lcdBar
        total = max(1, int(msg.get("total", 1)))
        if bar.maximum() != total:
            bar.setRange(0, total)
        bar.setValue(int(msg.get("done", 0)))

    def _lcd_fill(self):
        if not self._need_ready("屏幕填充"):
            return
        prof = self._lcd_profile()
        if prof is None:
            return
        params = {"profile": prof, "x": self.lcdX.value(),
                  "y": self.lcdY.value(), "w": self.lcdFW.value(),
                  "h": self.lcdFH.value(),
                  "color565": self._lcd_color565()}

        def done(ok, data):
            self.lcdBar.setRange(0, 100)
            self.lcdBar.setValue(100)
            if ok:
                self._log(self.lcdOut,
                          f"填充完成 {data.get('pixels', 0)} 像素")
            else:
                self._err("填充失败", data)
        self.lcdBar.setRange(0, 1)
        self._req("lcd_fill", cb=done, params=params, progress=self._lcd_prog)

    def _lcd_browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择图片", "", "图片 (*.png *.jpg *.jpeg *.bmp)")
        if path:
            self.lcdImgPath.setText(path)

    def _lcd_image(self):
        if not self._need_ready("图片发送"):
            return
        prof = self._lcd_profile()
        if prof is None:
            return
        path = self.lcdImgPath.text().strip()
        if not path:
            self._err("图片发送", "请先选择图片文件")
            return
        params = {"profile": prof, "x": self.lcdX.value(),
                  "y": self.lcdY.value(), "w": self.lcdFW.value(),
                  "h": self.lcdFH.value(), "file": path,
                  "swap": self.lcdSwap.isChecked()}

        def done(ok, data):
            self.lcdBar.setRange(0, 100)
            self.lcdBar.setValue(100)
            if ok:
                self._log(self.lcdOut, f"图片发送完成 {data.get('bytes', 0)}B")
            else:
                self._err("图片发送失败", data)
        self.lcdBar.setRange(0, 1)
        self._req("lcd_image", cb=done, params=params,
                  progress=self._lcd_prog)
