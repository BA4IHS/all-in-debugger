# coding: utf-8
"""预设命令页：TableWidget 多行命令，每行 启用/内容/HEX/周期/间隔/发送。

每行拥有独立的 QTimer 与控件束（_Row），不依赖表格行号闭包，
增删行不会导致信号错行。
"""
import json

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    CheckBox, FluentIcon, InfoBar, LineEdit, MessageBox, PrimaryPushButton,
    PushButton, SpinBox, TableWidget, TitleLabel, ToolButton,
)

from app import serial_utils as su
from app.config import loadData, saveData


def _centered(widget: QWidget) -> QWidget:
    """把控件放进居中容器，用于表格单元格。"""
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(4, 2, 4, 2)
    lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lay.addWidget(widget)
    return box


class _Row:
    """一行预设命令的全部控件与状态。"""

    def __init__(self, page: "PresetPage"):
        self.page = page
        self.enable = CheckBox()
        self.enable.setChecked(True)
        self.content = LineEdit()
        self.content.setPlaceholderText("命令内容（文本或 HEX）")
        self.hexCb = CheckBox()
        self.periodicCb = CheckBox()
        self.interval = SpinBox()
        self.interval.setRange(10, 3_600_000)
        self.interval.setValue(1000)
        self.sendBtn = PrimaryPushButton("发送")
        self.timer = QTimer()

        self.sendBtn.clicked.connect(lambda: page.sendRow(self))
        self.timer.timeout.connect(lambda: page.sendRow(self))
        # 内容 / HEX 变更：防抖重校验 + 重启周期定时器，
        # 否则编辑后定时器状态与内容脱节（停了不再启动）
        self.content.textChanged.connect(lambda _: page.onRowEdited(self))
        self.hexCb.stateChanged.connect(lambda _: page.onRowEdited(self))
        self.periodicCb.stateChanged.connect(lambda _: page.syncTimer(self))
        self.enable.stateChanged.connect(lambda _: page.syncTimer(self))
        self.interval.valueChanged.connect(lambda _: page.syncTimer(self))

    def widgets(self):
        return [
            _centered(self.enable), self.content, _centered(self.hexCb),
            _centered(self.periodicCb), self.interval, _centered(self.sendBtn),
        ]

    def toDict(self) -> dict:
        return {
            "enabled": self.enable.isChecked(),
            "text": self.content.text(),
            "is_hex": self.hexCb.isChecked(),
            "periodic": self.periodicCb.isChecked(),
            "interval_ms": self.interval.value(),
        }

    def applyDict(self, d: dict):
        # 载入时屏蔽控件信号：避免触发 syncTimer 校验弹「校验失败」警告
        # （启动阶段 UI 尚未显示，且此时校验无意义），载入完由 addRow 统一同步
        for w in (self.enable, self.hexCb, self.periodicCb, self.interval,
                  self.content):
            w.blockSignals(True)
        self.enable.setChecked(bool(d.get("enabled", True)))
        self.content.setText(str(d.get("text", "")))
        self.hexCb.setChecked(bool(d.get("is_hex", False)))
        self.periodicCb.setChecked(bool(d.get("periodic", False)))
        self.interval.setValue(int(d.get("interval_ms", 1000)))
        for w in (self.enable, self.hexCb, self.periodicCb, self.interval,
                  self.content):
            w.blockSignals(False)

    def buildPayload(self):
        """返回 (bytes|None, err)。"""
        text = self.content.text()
        if self.hexCb.isChecked():
            return su.parse_hex_input(text)
        if not text:
            return None, "命令内容为空"
        return su.encode_text(text, "UTF-8"), ""

    def updateHint(self):
        """HEX 内容即时校验：错误写到输入框 tooltip，避免发送时才发现。"""
        data, err = self.buildPayload()
        self.content.setToolTip(err if data is None else "")
        return data is not None

    def stop(self):
        self.timer.stop()


class PresetPage(QWidget):
    sendRequested = pyqtSignal(bytes)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list = []

        v = QVBoxLayout(self)
        v.setContentsMargins(12, 60, 12, 0)  # 顶部留白避开悬浮标题栏
        v.setSpacing(12)

        # 工具条
        bar = QHBoxLayout()
        bar.addWidget(TitleLabel("预设命令", self))
        bar.addStretch(1)
        upBtn = ToolButton(FluentIcon.UP, self)
        upBtn.setToolTip("上移选中行")
        downBtn = ToolButton(FluentIcon.DOWN, self)
        downBtn.setToolTip("下移选中行")
        addBtn = PushButton(FluentIcon.ADD, "添加", self)
        delBtn = PushButton(FluentIcon.DELETE, "删除选中", self)
        importBtn = PushButton(FluentIcon.LIBRARY, "导入", self)
        exportBtn = PushButton(FluentIcon.SAVE, "导出", self)
        upBtn.clicked.connect(lambda: self.moveSelected(-1))
        downBtn.clicked.connect(lambda: self.moveSelected(1))
        addBtn.clicked.connect(lambda: self.addRow())
        delBtn.clicked.connect(self.removeSelected)
        importBtn.clicked.connect(self.importJson)
        exportBtn.clicked.connect(self.exportJson)
        for b in (upBtn, downBtn, addBtn, delBtn, importBtn, exportBtn):
            bar.addWidget(b)
        v.addLayout(bar)

        # 表格
        self.table = TableWidget(self)
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["启用", "命令内容", "HEX", "周期", "间隔(ms)", "操作"])
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 60), (2, 56), (3, 56), (4, 110), (5, 90)):
            self.table.setColumnWidth(col, width)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(46)
        v.addWidget(self.table, 1)

        # 防抖自动保存：任何行变更后延时落盘，避免崩溃/被杀丢失全部预设
        # （须在下方载入预设前创建：addRow 内会调用 _scheduleSave）
        self._saveTimer = QTimer(self)
        self._saveTimer.setSingleShot(True)
        self._saveTimer.setInterval(1500)
        self._saveTimer.timeout.connect(self.savePresets)

        # 载入持久化的预设
        for d in loadData().get("presets", []):
            if isinstance(d, dict):
                self.addRow(d)
        if not self._rows:
            self.addRow()
        # 载入阶段触发的自动保存无意义（数据未变），取消之
        self._saveTimer.stop()

    # ── 行管理 ──────────────────────────────────────────────────

    def addRow(self, data: dict = None):
        row = _Row(self)
        if data:
            row.applyDict(data)
        n = self.table.rowCount()
        self.table.insertRow(n)
        for col, w in enumerate(row.widgets()):
            self.table.setCellWidget(n, col, w)
        self._rows.append(row)
        self.syncTimer(row, quiet=True)  # 载入/新增阶段静默同步，不弹警告
        row.updateHint()
        self._scheduleSave()

    def removeSelected(self):
        # 支持多选删除（选行模式下 selectedRows 返回全部选中行）
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()},
                      reverse=True)
        if not rows:
            InfoBar.warning(title="提示", content="请先选中要删除的行",
                            duration=2000, parent=self)
            return
        for r in rows:
            row = self._rows.pop(r)
            row.stop()
            self.table.removeRow(r)
        self._scheduleSave()

    def moveSelected(self, delta: int):
        """选中行上移/下移一行。

        removeRow 会销毁行内控件，不能重摆原控件；
        从 toDict 快照重建整表（周期定时器随 addRow 重新同步）。
        """
        idx = self.table.currentRow()
        if idx < 0:
            InfoBar.warning(title="提示", content="请先选中要移动的行",
                            duration=2000, parent=self)
            return
        new = idx + delta
        if not (0 <= new < len(self._rows)):
            return
        dicts = [r.toDict() for r in self._rows]
        dicts[idx], dicts[new] = dicts[new], dicts[idx]
        self._clearRows()
        for d in dicts:
            self.addRow(d)
        self.table.setCurrentCell(new, 0)
        self._scheduleSave()

    def _clearRows(self):
        """停止全部定时器并清空表格（removeRow 会销毁行内控件）。"""
        for row in self._rows:
            row.stop()
        self._rows.clear()
        for r in range(self.table.rowCount() - 1, -1, -1):
            self.table.removeRow(r)

    def _indexOf(self, row: _Row) -> int:
        return self._rows.index(row)

    # ── 发送 / 周期 ─────────────────────────────────────────────

    def sendRow(self, row: _Row):
        data, err = row.buildPayload()
        if data is None:
            InfoBar.warning(title="无法发送", content=err,
                            duration=3000, parent=self)
            return
        self.sendRequested.emit(data)

    def onRowEdited(self, row: _Row):
        """内容 / HEX 变更：即时校验提示，并防抖重启周期定时器。"""
        row.updateHint()
        # 防抖：连续输入（尤其清空重输的中间态）时不反复校验/弹警告
        if not hasattr(row, "_syncTimer") or row._syncTimer is None:
            row._syncTimer = QTimer(row)
            row._syncTimer.setSingleShot(True)
            row._syncTimer.setInterval(400)
            row._syncTimer.timeout.connect(lambda: self.syncTimer(row))
        row._syncTimer.start()
        self._scheduleSave()

    def syncTimer(self, row: _Row, quiet: bool = False):
        active = (row.enable.isChecked() and row.periodicCb.isChecked())
        if not active:
            row.stop()
            return
        data, err = row.buildPayload()
        if data is None:
            row.stop()
            # quiet（输入中间态/载入阶段）：静默停止，不弹警告轰炸；
            # 内容修好后 onRowEdited 防抖会重新启动定时器
            if not quiet and err:
                InfoBar.warning(title="周期发送校验失败", content=err,
                                duration=3000, parent=self)
            return
        row.timer.start(row.interval.value())

    # ── 持久化 / 导入导出 ───────────────────────────────────────

    def toPresetDicts(self):
        return [r.toDict() for r in self._rows]

    def savePresets(self):
        data = loadData()
        data["presets"] = self.toPresetDicts()
        saveData(data)

    def importJson(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "导入预设命令", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                items = json.load(f)
            if not isinstance(items, list):
                raise ValueError("JSON 顶层应为数组")
        except (OSError, ValueError) as e:
            InfoBar.error(title="导入失败", content=str(e),
                          duration=5000, parent=self)
            return
        items = [d for d in items if isinstance(d, dict)]
        if not items:
            InfoBar.warning(title="导入失败", content="文件中没有有效的预设行",
                            duration=3000, parent=self)
            return
        # 询问导入方式：替换整表 / 追加到末尾
        box = MessageBox("导入方式", f"共 {len(items)} 条有效预设，如何导入？", self)
        box.yesButton.setText("替换")
        box.cancelButton.setText("追加")
        if box.exec():
            self._clearRows()
        for d in items:
            self.addRow(d)
        InfoBar.success(title="导入完成",
                        content=f"已导入 {len(items)} 行，共 {len(self._rows)} 行",
                        duration=2000, parent=self)

    def exportJson(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出预设命令", "presets.json", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.toPresetDicts(), f, ensure_ascii=False, indent=2)
        except OSError as e:
            InfoBar.error(title="导出失败", content=str(e),
                          duration=5000, parent=self)
            return
        InfoBar.success(title="导出完成", content=path, duration=2000, parent=self)

    # ── 关窗清理 ────────────────────────────────────────────────

    def _scheduleSave(self):
        """变更后防抖落盘：静止 1.5 s 后保存，避免高频 IO。"""
        self._saveTimer.start()

    def shutdown(self):
        self._saveTimer.stop()
        for row in self._rows:
            row.stop()
            if getattr(row, "_syncTimer", None) is not None:
                row._syncTimer.stop()
        self.savePresets()
