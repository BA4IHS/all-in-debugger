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
    CheckBox, FluentIcon, InfoBar, LineEdit, PrimaryPushButton, PushButton,
    SpinBox, TableWidget, TitleLabel,
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
        self.enable.setChecked(bool(d.get("enabled", True)))
        self.content.setText(str(d.get("text", "")))
        self.hexCb.setChecked(bool(d.get("is_hex", False)))
        self.periodicCb.setChecked(bool(d.get("periodic", False)))
        self.interval.setValue(int(d.get("interval_ms", 1000)))

    def buildPayload(self):
        """返回 (bytes|None, err)。"""
        text = self.content.text()
        if self.hexCb.isChecked():
            return su.parse_hex_input(text)
        if not text:
            return None, "命令内容为空"
        return su.encode_text(text, "UTF-8"), ""

    def stop(self):
        self.timer.stop()


class PresetPage(QWidget):
    sendRequested = pyqtSignal(bytes)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list = []

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 40, 0, 0)  # 顶部留白避开悬浮标题栏
        v.setSpacing(12)

        # 工具条
        bar = QHBoxLayout()
        bar.addWidget(TitleLabel("预设命令", self))
        bar.addStretch(1)
        addBtn = PushButton(FluentIcon.ADD, "添加", self)
        delBtn = PushButton(FluentIcon.DELETE, "删除选中", self)
        importBtn = PushButton(FluentIcon.LIBRARY, "导入", self)
        exportBtn = PushButton(FluentIcon.SAVE, "导出", self)
        addBtn.clicked.connect(lambda: self.addRow())
        delBtn.clicked.connect(self.removeSelected)
        importBtn.clicked.connect(self.importJson)
        exportBtn.clicked.connect(self.exportJson)
        for b in (addBtn, delBtn, importBtn, exportBtn):
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

        # 载入持久化的预设
        for d in loadData().get("presets", []):
            if isinstance(d, dict):
                self.addRow(d)
        if not self._rows:
            self.addRow()

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
        self.syncTimer(row)

    def removeSelected(self):
        idx = self.table.currentRow()
        if idx < 0 or idx >= len(self._rows):
            InfoBar.warning(title="提示", content="请先选中要删除的行",
                            duration=2000, parent=self)
            return
        row = self._rows.pop(idx)
        row.stop()
        self.table.removeRow(idx)

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

    def syncTimer(self, row: _Row):
        active = (row.enable.isChecked() and row.periodicCb.isChecked())
        if not active:
            row.stop()
            return
        data, err = row.buildPayload()
        if data is None:
            row.stop()
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
        for d in items:
            if isinstance(d, dict):
                self.addRow(d)
        InfoBar.success(title="导入完成", content=f"新增 {len(items)} 行",
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

    def shutdown(self):
        for row in self._rows:
            row.stop()
        self.savePresets()
