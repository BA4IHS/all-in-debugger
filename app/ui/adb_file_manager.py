# coding: utf-8
"""独立 ADB 文件管理器：异步目录浏览、上传、下载与删除。"""
from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from PyQt6.QtCore import (
    QObject, QPoint, QProcess, QRect, Qt, QTimer, pyqtSignal,
)
from PyQt6.QtGui import QColor, QCloseEvent, QKeyEvent, QMouseEvent, QPalette
from PyQt6.QtWidgets import (
    QAbstractItemView, QFileDialog, QFileIconProvider, QHBoxLayout,
    QHeaderView, QRubberBand, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from qfluentwidgets import (
    Action, BodyLabel, CaptionLabel, CardWidget, FluentIcon, InfoBar,
    IndeterminateProgressBar, LineEdit, MessageBox, PrimaryDropDownPushButton,
    ProgressBar, PushButton, RoundMenu, ToolButton, TreeWidget, isDarkTheme,
)

from app.ui.window_utils import center_window


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PROGRESS_RE = re.compile(r"(?<!\d)(\d{1,3})%")
_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
_ROLE_PATH = int(Qt.ItemDataRole.UserRole)
_ROLE_KIND = _ROLE_PATH + 1
_DELETE_OK = "__ADB_FILE_DELETE_OK__"


def normalize_remote_path(path: str, current: str = "/") -> str:
    """规范化设备绝对路径，允许地址栏输入相对路径。"""
    value = (path or "").strip().replace("\\", "/")
    if not value:
        return current or "/"
    if not value.startswith("/"):
        value = posixpath.join(current or "/", value)
    value = posixpath.normpath(value)
    return value if value.startswith("/") else "/" + value


def remote_child_path(parent: str, name: str) -> str:
    return normalize_remote_path(posixpath.join(parent, name), "/")


def build_delete_command(paths) -> str:
    """构造安全的批量删除命令；禁止删除设备根目录。"""
    normalized = []
    for path in paths:
        value = normalize_remote_path(str(path))
        if value in ("", "/"):
            raise ValueError("禁止删除设备根目录")
        normalized.append(value)
    if not normalized:
        raise ValueError("没有可删除的目标")
    quoted = " ".join(shlex.quote(p) for p in normalized)
    return f"rm -rf -- {quoted} && echo {_DELETE_OK}"


def _parse_modified(month: str, day: str, time_or_year: str) -> str:
    month_num = _MONTHS.get(month)
    if month_num is None:
        return f"{month} {day} {time_or_year}"
    try:
        day_num = int(day)
        if ":" in time_or_year:
            hour, minute = (int(v) for v in time_or_year.split(":", 1))
            now = datetime.now()
            value = datetime(now.year, month_num, day_num, hour, minute)
            if value > now + timedelta(days=1):
                value = value.replace(year=now.year - 1)
        else:
            value = datetime(int(time_or_year), month_num, day_num)
        return value.strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return f"{month} {day} {time_or_year}"


def parse_directory_listing(data: bytes):
    """解析 ``LC_ALL=C ls -lAn`` 输出，文件名中的空格与中文保持原样。"""
    text = data.decode("utf-8", "replace").replace("\r", "")
    text = _ANSI_RE.sub("", text)
    entries = []
    errors = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("total "):
            continue
        if line.lower().startswith(("ls:", "sh:", "error:")):
            errors.append(line)
            continue
        parts = line.split(None, 8)
        if len(parts) < 9 or not parts[0]:
            continue
        mode, size_text = parts[0], parts[4]
        if mode[0] not in "-dlcbps":
            continue
        name = parts[8]
        if mode[0] == "l" and " -> " in name:
            name = name.split(" -> ", 1)[0]
        if name in (".", ".."):
            continue
        try:
            size = int(size_text)
        except ValueError:
            size = 0
        kind = {
            "d": "directory", "l": "link", "c": "device", "b": "device",
            "p": "pipe", "s": "socket",
        }.get(mode[0], "file")
        entries.append({
            "name": name,
            "size": size,
            "modified": _parse_modified(parts[5], parts[6], parts[7]),
            "kind": kind,
            "mode": mode,
        })
    entries.sort(key=lambda item: (
        item["kind"] != "directory", item["name"].casefold()))
    return entries, "\n".join(errors)


def format_size(size: int) -> str:
    value = float(max(0, int(size)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return "0 B"


def file_type_text(entry: dict) -> str:
    kind = entry["kind"]
    if kind == "directory":
        return "文件夹"
    if kind == "link":
        return "符号链接"
    if kind == "device":
        return "设备文件"
    if kind == "pipe":
        return "管道"
    if kind == "socket":
        return "套接字"
    suffix = Path(entry["name"]).suffix
    return f"{suffix[1:].upper()} 文件" if suffix else "文件"


class _FileTreeWidget(TreeWidget):
    """带资源管理器式框选以及 Delete/Enter 快捷键的文件列表。"""

    deletePressed = pyqtSignal()
    enterPressed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rubber = QRubberBand(QRubberBand.Shape.Rectangle, self.viewport())
        self._rubber_origin = None
        self._initial_selection = set()

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Delete:
            self.deletePressed.emit()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.enterPressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event: QMouseEvent):
        if (event.button() == Qt.MouseButton.LeftButton
                and self.itemAt(event.position().toPoint()) is None):
            self._rubber_origin = event.position().toPoint()
            self._initial_selection = {id(item) for item in self.selectedItems()} \
                if event.modifiers() & Qt.KeyboardModifier.ControlModifier \
                else set()
            if not self._initial_selection:
                self.clearSelection()
            self._rubber.setGeometry(QRect(self._rubber_origin, QPoint()))
            self._rubber.show()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._rubber_origin is None:
            super().mouseMoveEvent(event)
            return
        rect = QRect(
            self._rubber_origin, event.position().toPoint()).normalized()
        self._rubber.setGeometry(rect)
        for index in range(self.topLevelItemCount()):
            item = self.topLevelItem(index)
            item.setSelected(
                id(item) in self._initial_selection
                or rect.intersects(self.visualItemRect(item)))
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._rubber_origin is not None:
            self._rubber_origin = None
            self._rubber.hide()
            event.accept()
            return
        super().mouseReleaseEvent(event)


@dataclass
class _AdbOperation:
    label: str
    arguments: list
    required_marker: str = ""


class _AdbOperationQueue(QObject):
    """顺序执行 push/pull/delete，输出进度且不阻塞 UI。"""

    progressChanged = pyqtSignal(int, str)
    finished = pyqtSignal(bool, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._adb = ""
        self._serial = ""
        self._queue = []
        self._total = 0
        self._done = 0
        self._proc = None
        self._buffer = bytearray()
        self._current = None

    def is_running(self):
        return self._proc is not None or bool(self._queue)

    def start(self, adb: str, serial: str, operations):
        if self.is_running():
            return False
        self._adb, self._serial = adb, serial
        self._queue = list(operations)
        self._total = len(self._queue)
        self._done = 0
        self._start_next()
        return True

    def cancel(self):
        self._queue.clear()
        p = self._proc
        self._proc = None
        if p is not None and p.state() != QProcess.ProcessState.NotRunning:
            p.kill()

    def shutdown(self):
        self._queue.clear()
        p = self._proc
        self._proc = None
        if p is not None and p.state() != QProcess.ProcessState.NotRunning:
            p.kill()
            p.waitForFinished(500)

    def _start_next(self):
        if not self._queue:
            self.progressChanged.emit(100, "操作完成")
            self.finished.emit(True, "")
            return
        self._current = self._queue.pop(0)
        self._buffer.clear()
        self.progressChanged.emit(
            int(self._done * 100 / max(1, self._total)),
            self._current.label)
        p = QProcess(self)
        p.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        p.readyReadStandardOutput.connect(lambda p=p: self._read(p))
        p.finished.connect(
            lambda code, status, p=p: self._on_finished(p, code, status))
        p.errorOccurred.connect(lambda error, p=p: self._on_error(p, error))
        self._proc = p
        p.start(self._adb, ["-s", self._serial, *self._current.arguments])

    def _read(self, p):
        if p is not self._proc:
            return
        chunk = bytes(p.readAllStandardOutput())
        if not chunk:
            return
        self._buffer.extend(chunk)
        text = chunk.decode("utf-8", "replace")
        matches = _PROGRESS_RE.findall(text)
        if matches:
            current = min(100, int(matches[-1]))
            overall = int((self._done + current / 100) * 100
                          / max(1, self._total))
            self.progressChanged.emit(overall, self._current.label)

    def _on_finished(self, p, code, _status):
        if p is not self._proc:
            return
        self._read(p)
        self._proc = None
        p.deleteLater()
        output = self._buffer.decode("utf-8", "replace").strip()
        marker = self._current.required_marker
        if int(code) != 0 or (marker and marker not in output):
            detail = output or f"adb 退出代码 {int(code)}"
            self._queue.clear()
            self.finished.emit(False, f"{self._current.label}\n{detail}")
            return
        self._done += 1
        self._start_next()

    def _on_error(self, p, error):
        if p is not self._proc:
            return
        self._proc = None
        self._queue.clear()
        if p.state() != QProcess.ProcessState.NotRunning:
            p.kill()
        p.deleteLater()
        self.finished.emit(
            False, f"{self._current.label}\nadb 进程错误："
                   f"{getattr(error, 'value', error)}")


class AdbFileManagerWindow(QWidget):
    """与主窗口解耦的桌面端 ADB 文件管理器。"""

    def __init__(self, adb_path: str, serial: str, parent=None,
                 auto_load=True):
        super().__init__(None, Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle(f"ADB 文件管理器 · {serial}")
        self.resize(1080, 700)
        self.setMinimumSize(760, 480)
        self._adb = adb_path
        self._serial = serial
        self._current_path = "/"
        self._list_proc = None
        self._list_buffer = bytearray()
        self._list_timer = QTimer(self)
        self._list_timer.setSingleShot(True)
        self._list_timer.timeout.connect(self._on_list_timeout)
        self._operations = _AdbOperationQueue(self)
        self._operations.progressChanged.connect(self._on_progress)
        self._operations.finished.connect(self._on_operations_finished)
        self._refresh_after_operation = False
        self._icon_provider = QFileIconProvider()

        self._apply_window_palette()
        self._build_ui()
        center_window(self)
        if auto_load:
            self.navigate("/")

    def _apply_window_palette(self):
        palette = self.palette()
        palette.setColor(
            QPalette.ColorRole.Window,
            QColor(32, 39, 46) if isDarkTheme() else QColor(245, 247, 250))
        self.setPalette(palette)
        self.setAutoFillBackground(True)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 14)
        root.setSpacing(10)

        toolbar = CardWidget(self)
        tools = QHBoxLayout(toolbar)
        tools.setContentsMargins(12, 10, 12, 10)
        tools.setSpacing(8)

        self.uploadBtn = PrimaryDropDownPushButton(
            FluentIcon.UP, "上传", toolbar)
        upload_menu = RoundMenu(parent=self.uploadBtn)
        upload_menu.addAction(Action(
            FluentIcon.DOCUMENT, "上传文件（可多选）",
            triggered=self._choose_upload_files))
        upload_menu.addAction(Action(
            FluentIcon.FOLDER, "上传文件夹",
            triggered=self._choose_upload_folder))
        self.uploadBtn.setMenu(upload_menu)

        self.downloadBtn = PushButton(
            FluentIcon.DOWNLOAD, "下载", toolbar)
        self.deleteBtn = PushButton(
            FluentIcon.DELETE, "删除", toolbar)
        self.downloadBtn.setEnabled(False)
        self.deleteBtn.setEnabled(False)
        self.downloadBtn.clicked.connect(self._download_selected)
        self.deleteBtn.clicked.connect(self._delete_selected)
        tools.addWidget(self.uploadBtn)
        tools.addWidget(self.downloadBtn)
        tools.addWidget(self.deleteBtn)
        tools.addStretch(1)
        tools.addWidget(CaptionLabel(f"设备：{self._serial}", toolbar))
        root.addWidget(toolbar)

        path_row = QHBoxLayout()
        path_row.setSpacing(7)
        self.rootBtn = ToolButton(FluentIcon.HOME, self)
        self.rootBtn.setToolTip("返回根目录")
        self.upBtn = ToolButton(FluentIcon.UP, self)
        self.upBtn.setToolTip("返回上级目录")
        self.addressEdit = LineEdit(self)
        self.addressEdit.setPlaceholderText("设备完整路径")
        self.addressEdit.setText(self._current_path)
        self.addressEdit.setClearButtonEnabled(False)
        self.addressEdit.returnPressed.connect(
            lambda: self.navigate(self.addressEdit.text()))
        self.refreshBtn = ToolButton(FluentIcon.SYNC, self)
        self.refreshBtn.setToolTip("刷新当前目录")
        self.rootBtn.clicked.connect(lambda _=False: self.navigate("/"))
        self.upBtn.clicked.connect(
            lambda _=False: self.navigate(
                posixpath.dirname(self._current_path.rstrip("/")) or "/"))
        self.refreshBtn.clicked.connect(
            lambda _=False: self.navigate(self._current_path))
        path_row.addWidget(self.rootBtn)
        path_row.addWidget(self.upBtn)
        path_row.addWidget(self.addressEdit, 1)
        path_row.addWidget(self.refreshBtn)
        root.addLayout(path_row)

        self.loadingBar = IndeterminateProgressBar(self, start=False)
        self.loadingBar.setFixedHeight(4)
        self.loadingBar.hide()
        root.addWidget(self.loadingBar)

        self.tree = _FileTreeWidget(self)
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["名称", "大小", "修改日期", "类型"])
        self.tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setUniformRowHeights(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.itemSelectionChanged.connect(self._update_action_state)
        self.tree.itemChanged.connect(
            lambda _item, _column: self._update_action_state())
        self.tree.itemDoubleClicked.connect(self._open_item)
        self.tree.deletePressed.connect(self._delete_selected)
        self.tree.enterPressed.connect(self._open_current_item)
        root.addWidget(self.tree, 1)

        status_row = QHBoxLayout()
        self.statusLabel = BodyLabel("准备就绪", self)
        self.operationProgress = ProgressBar(self)
        self.operationProgress.setRange(0, 100)
        self.operationProgress.setValue(0)
        self.operationProgress.setFixedWidth(260)
        self.operationProgress.hide()
        status_row.addWidget(self.statusLabel, 1)
        status_row.addWidget(self.operationProgress)
        root.addLayout(status_row)

    # ── 目录读取 ────────────────────────────────────────────────

    def navigate(self, path: str):
        path = normalize_remote_path(path, self._current_path)
        self._cancel_listing()
        self._current_path = path
        self.addressEdit.setText(path)
        self.upBtn.setEnabled(path != "/")
        self.tree.clear()
        self.tree.setEnabled(False)
        self.loadingBar.show()
        self.loadingBar.start()
        self.statusLabel.setText(f"正在加载 {path} …")
        command = (
            "LC_ALL=C TERM=dumb LS_COLORS= "
            f"ls -lAn {shlex.quote(path)}")
        p = QProcess(self)
        p.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        p.readyReadStandardOutput.connect(lambda p=p: self._read_listing(p))
        p.finished.connect(
            lambda code, status, p=p: self._listing_finished(
                p, code, status))
        p.errorOccurred.connect(
            lambda error, p=p: self._listing_error(p, error))
        self._list_proc = p
        self._list_buffer.clear()
        self._list_timer.start(15_000)
        p.start(self._adb, ["-s", self._serial, "shell", command])

    def _read_listing(self, p):
        if p is self._list_proc:
            self._list_buffer.extend(bytes(p.readAllStandardOutput()))

    def _listing_finished(self, p, code, _status):
        if p is not self._list_proc:
            return
        self._read_listing(p)
        self._list_proc = None
        self._list_timer.stop()
        p.deleteLater()
        entries, error = parse_directory_listing(bytes(self._list_buffer))
        self._finish_loading()
        if int(code) != 0 or error:
            detail = error or f"adb 退出代码 {int(code)}"
            self.statusLabel.setText("目录加载失败")
            InfoBar.error(
                title="无法读取目录", content=detail,
                duration=6000, parent=self)
            return
        self._populate(entries)

    def _listing_error(self, p, error):
        if p is not self._list_proc:
            return
        self._list_proc = None
        self._list_timer.stop()
        if p.state() != QProcess.ProcessState.NotRunning:
            p.kill()
        p.deleteLater()
        self._finish_loading()
        self.statusLabel.setText("目录加载失败")
        InfoBar.error(
            title="ADB 进程错误",
            content=str(getattr(error, "value", error)),
            duration=6000, parent=self)

    def _on_list_timeout(self):
        p = self._list_proc
        if p is None:
            return
        self._list_proc = None
        p.kill()
        p.deleteLater()
        self._finish_loading()
        self.statusLabel.setText("目录加载超时")
        InfoBar.error(
            title="目录加载超时", content=self._current_path,
            duration=5000, parent=self)

    def _cancel_listing(self, wait=False):
        p = self._list_proc
        self._list_proc = None
        self._list_timer.stop()
        if p is not None and p.state() != QProcess.ProcessState.NotRunning:
            p.kill()
            if wait:
                p.waitForFinished(500)
            p.deleteLater()

    def _finish_loading(self):
        self.loadingBar.stop()
        self.loadingBar.hide()
        self.tree.setEnabled(True)

    def _populate(self, entries):
        self.tree.blockSignals(True)
        self.tree.clear()
        for entry in entries:
            item = QTreeWidgetItem(self.tree)
            item.setText(0, entry["name"])
            item.setText(
                1, "—" if entry["kind"] == "directory"
                else format_size(entry["size"]))
            item.setText(2, entry["modified"])
            item.setText(3, file_type_text(entry))
            icon_type = QFileIconProvider.IconType.Folder \
                if entry["kind"] == "directory" \
                else QFileIconProvider.IconType.File
            item.setIcon(0, self._icon_provider.icon(icon_type))
            item.setCheckState(0, Qt.CheckState.Unchecked)
            item.setData(
                0, _ROLE_PATH,
                remote_child_path(self._current_path, entry["name"]))
            item.setData(0, _ROLE_KIND, entry["kind"])
            item.setToolTip(0, item.data(0, _ROLE_PATH))
            item.setFlags(
                item.flags() | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsEnabled)
        self.tree.blockSignals(False)
        self.statusLabel.setText(f"{self._current_path}  ·  {len(entries)} 项")
        self._update_action_state()

    # ── 选择与导航 ──────────────────────────────────────────────

    def _target_items(self):
        checked = [
            self.tree.topLevelItem(i)
            for i in range(self.tree.topLevelItemCount())
            if self.tree.topLevelItem(i).checkState(0)
            == Qt.CheckState.Checked
        ]
        # 复选与资源管理器式行选择可以混用；Ctrl+A 后即使已有复选项，
        # 也必须把所有选中行纳入批量操作。
        result = []
        seen = set()
        for item in [*checked, *self.tree.selectedItems()]:
            identity = id(item)
            if identity not in seen:
                seen.add(identity)
                result.append(item)
        return result

    def _update_action_state(self):
        enabled = bool(self._target_items()) and not self._operations.is_running()
        self.downloadBtn.setEnabled(enabled)
        self.deleteBtn.setEnabled(enabled)

    def _open_item(self, item, _column=0):
        if item and item.data(0, _ROLE_KIND) in ("directory", "link"):
            self.navigate(item.data(0, _ROLE_PATH))

    def _open_current_item(self):
        self._open_item(self.tree.currentItem())

    # ── 上传 / 下载 / 删除 ─────────────────────────────────────

    def _choose_upload_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择要上传的文件", "", "所有文件 (*.*)")
        if paths:
            self._upload_paths(paths)

    def _choose_upload_folder(self):
        path = QFileDialog.getExistingDirectory(
            self, "选择要上传的文件夹")
        if path:
            self._upload_paths([path])

    def _upload_paths(self, paths):
        operations = [
            _AdbOperation(
                f"正在上传：{Path(path).name}",
                ["push", str(path), self._current_path.rstrip("/") + "/"])
            for path in paths
        ]
        self._start_operations(operations, refresh_after=True)

    def _download_selected(self):
        items = self._target_items()
        if not items:
            return
        destination = QFileDialog.getExistingDirectory(
            self, "选择下载保存位置")
        if not destination:
            return
        operations = [
            _AdbOperation(
                f"正在下载：{item.text(0)}",
                ["pull", item.data(0, _ROLE_PATH), destination])
            for item in items
        ]
        self._start_operations(operations)

    def _delete_selected(self):
        items = self._target_items()
        if not items or self._operations.is_running():
            return
        names = [item.text(0) for item in items]
        preview = "\n".join(f"• {name}" for name in names[:8])
        if len(names) > 8:
            preview += f"\n…以及另外 {len(names) - 8} 项"
        box = MessageBox(
            "确认永久删除",
            f"以下内容将从设备永久删除，且无法恢复：\n\n{preview}",
            self)
        box.yesButton.setText("永久删除")
        box.cancelButton.setText("取消")
        if not box.exec():
            return
        try:
            command = build_delete_command(
                [item.data(0, _ROLE_PATH) for item in items])
        except ValueError as error:
            InfoBar.error(
                title="无法删除", content=str(error),
                duration=5000, parent=self)
            return
        operation = _AdbOperation(
            f"正在删除 {len(items)} 项",
            ["shell", command], required_marker=_DELETE_OK)
        self._start_operations([operation], refresh_after=True)

    def _start_operations(self, operations, refresh_after=False):
        if not operations or self._operations.is_running():
            return
        self._refresh_after_operation = refresh_after
        self.uploadBtn.setEnabled(False)
        self.downloadBtn.setEnabled(False)
        self.deleteBtn.setEnabled(False)
        self.operationProgress.setValue(0)
        self.operationProgress.show()
        self._operations.start(self._adb, self._serial, operations)

    def _on_progress(self, value: int, text: str):
        self.operationProgress.setValue(max(0, min(100, int(value))))
        self.statusLabel.setText(text)

    def _on_operations_finished(self, success: bool, error: str):
        self.uploadBtn.setEnabled(True)
        self.operationProgress.hide()
        if success:
            self.statusLabel.setText("操作完成")
            InfoBar.success(
                title="操作完成", content="ADB 文件操作已完成",
                duration=2500, parent=self)
            if self._refresh_after_operation:
                self.navigate(self._current_path)
        else:
            self.statusLabel.setText("操作失败")
            InfoBar.error(
                title="文件操作失败", content=error,
                duration=7000, parent=self)
        self._refresh_after_operation = False
        self._update_action_state()

    # ── 生命周期 ────────────────────────────────────────────────

    def closeEvent(self, event: QCloseEvent):
        self._cancel_listing(wait=True)
        self._operations.shutdown()
        super().closeEvent(event)
