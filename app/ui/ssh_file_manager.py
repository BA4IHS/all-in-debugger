# coding: utf-8
"""独立 SFTP 文件管理器：与主窗口解耦的顶层窗口。

所有操作经 SshThread.sigSftp 发到 worker 线程执行（worker 唯一持有
paramiko SFTP 句柄），结果经 worker.sftpResult 信号回传，UI 不触碰句柄。
"""
from __future__ import annotations

import posixpath

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import (
    QAbstractItemView, QFileDialog, QFileIconProvider, QHBoxLayout,
    QHeaderView, QInputDialog, QTableWidgetItem, QVBoxLayout, QWidget,
)

from qfluentwidgets import (
    Action, BodyLabel, CaptionLabel, CardWidget, FluentIcon, IndeterminateProgressBar,
    InfoBar, LineEdit, MessageBox, PrimaryDropDownPushButton, PushButton,
    RoundMenu, TableWidget, ToolButton,
)

from app.ui.window_utils import center_window


def _fmt_size(n: int) -> str:
    value = max(0, int(n))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" \
                else f"{value:.1f} {unit}"
        value /= 1024
    return str(value)


class _SftpTable(TableWidget):
    """支持 Enter 进入目录 / Delete 删除的文件表格。"""
    enterPressed = pyqtSignal()
    deletePressed = pyqtSignal()

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.enterPressed.emit()
            return
        if event.key() == Qt.Key.Key_Delete:
            self.deletePressed.emit()
            return
        super().keyPressEvent(event)


class SftpFileManagerWindow(QWidget):
    """独立 SFTP 文件管理器窗口（不影响主窗口）。"""

    def __init__(self, sht, current_path: str = ".", connected: bool = False,
                 conn_label: str = "", parent=None):
        super().__init__(None, Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle("SFTP 文件管理器")
        self.resize(1080, 700)
        self.setMinimumSize(760, 480)
        self.sht = sht
        self._connected = connected
        self._current_path = posixpath.normpath(current_path or ".")
        self._pending = 0            # 未完成的文件操作数（不含目录列表）
        self._busy = False           # 是否有文件操作在途
        self._icon_provider = QFileIconProvider()

        self._build_ui()
        center_window(self)

        # worker → 窗口：连接状态与操作结果
        w = self.sht.worker
        w.connected.connect(self._on_connected)
        w.closed.connect(self._on_closed)
        w.connectFailed.connect(self._on_connect_failed)
        w.sftpResult.connect(self._on_sftp_result)

        if connected:
            if conn_label:
                self.statusLabel.setText(conn_label)
                self._apply_conn_title(conn_label)
            self.navigate(self._current_path)
        else:
            self._set_conn_state(False, "未连接，等待 SSH 连接…")

    # ── UI ─────────────────────────────────────────────────────

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
            triggered=self._upload_files))
        self.uploadBtn.setMenu(upload_menu)

        self.downloadBtn = PushButton(
            FluentIcon.DOWNLOAD, "下载", toolbar)
        self.deleteBtn = PushButton(
            FluentIcon.DELETE, "删除", toolbar)
        self.mkdirBtn = PushButton(
            FluentIcon.ADD, "新建目录", toolbar)
        self.downloadBtn.clicked.connect(self._download_selected)
        self.deleteBtn.clicked.connect(self._delete_selected)
        self.mkdirBtn.clicked.connect(self._mkdir)
        tools.addWidget(self.uploadBtn)
        tools.addWidget(self.downloadBtn)
        tools.addWidget(self.deleteBtn)
        tools.addWidget(self.mkdirBtn)
        tools.addStretch(1)
        self.connLabel = CaptionLabel("未连接", toolbar)
        tools.addWidget(self.connLabel)
        root.addWidget(toolbar)

        path_row = QHBoxLayout()
        path_row.setSpacing(7)
        self.rootBtn = ToolButton(FluentIcon.HOME, self)
        self.rootBtn.setToolTip("返回根目录")
        self.upBtn = ToolButton(FluentIcon.UP, self)
        self.upBtn.setToolTip("返回上级目录")
        self.addressEdit = LineEdit(self)
        self.addressEdit.setPlaceholderText("远程完整路径（可输入相对路径）")
        self.addressEdit.setText(self._current_path)
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

        self.table = _SftpTable(self)
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["名称", "大小", "类型"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.itemSelectionChanged.connect(self._update_action_state)
        self.table.cellDoubleClicked.connect(self._open_item)
        self.table.enterPressed.connect(self._open_current_item)
        self.table.deletePressed.connect(self._delete_selected)
        root.addWidget(self.table, 1)

        status_row = QHBoxLayout()
        self.statusLabel = BodyLabel("准备就绪", self)
        status_row.addWidget(self.statusLabel, 1)
        root.addLayout(status_row)

        self._update_action_state()

    # ── 连接状态 ───────────────────────────────────────────────

    def _apply_conn_title(self, label: str):
        text = str(label).replace("已连接 ", "").strip()
        if text:
            self.setWindowTitle(f"SFTP 文件管理器 · {text}")

    def _set_conn_state(self, connected: bool, text: str):
        self._connected = connected
        self.statusLabel.setText(text)
        if not connected:
            self.table.setRowCount(0)
            self._pending = 0
            self._busy = False
            self.loadingBar.stop()
            self.loadingBar.hide()
            self.connLabel.setText("未连接")
        self._update_action_state()

    def _on_connected(self, info: dict):
        text = f"已连接 {info.get('username')}@{info.get('host')}:" \
               f"{info.get('port')}"
        self._set_conn_state(True, text)
        self.connLabel.setText("已连接")
        self._apply_conn_title(text)
        self.navigate(self._current_path)

    def _on_closed(self):
        self._set_conn_state(False, "未连接")
        InfoBar.info(title="SSH 已断开", content="远端连接已关闭",
                     duration=3000, parent=self)

    def _on_connect_failed(self, msg: str):
        self._set_conn_state(False, "连接失败")
        InfoBar.error(title="SSH 连接失败", content=msg,
                      duration=6000, parent=self)

    # ── 目录读取 ───────────────────────────────────────────────

    def navigate(self, path: str):
        if not self._connected:
            return
        self._current_path = self._normalize(path)
        self.addressEdit.setText(self._current_path)
        self.loadingBar.start()
        self.loadingBar.show()
        self.sht.sigSftp.emit({"op": "list", "path": self._current_path})

    def _normalize(self, path: str) -> str:
        value = (path or "").strip().replace("\\", "/")
        if not value:
            return self._current_path or "/"
        if not value.startswith("/"):
            value = posixpath.join(self._current_path or "/", value)
        return posixpath.normpath(value)

    def _fill_table(self, entries: list):
        self.loadingBar.stop()
        self.loadingBar.hide()
        t = self.table
        t.setRowCount(len(entries))
        for i, e in enumerate(entries):
            is_dir = e.get("type") == "dir"
            name = QTableWidgetItem(str(e.get("name", "")))
            name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            name.setIcon(self._icon_provider.icon(
                QFileIconProvider.IconType.Folder
                if is_dir else QFileIconProvider.IconType.File))
            size = QTableWidgetItem(
                "" if is_dir else _fmt_size(int(e.get("size") or 0)))
            size.setFlags(size.flags() & ~Qt.ItemFlag.ItemIsEditable)
            typ = QTableWidgetItem("目录" if is_dir else "文件")
            typ.setFlags(typ.flags() & ~Qt.ItemFlag.ItemIsEditable)
            t.setItem(i, 0, name)
            t.setItem(i, 1, size)
            t.setItem(i, 2, typ)
        self.statusLabel.setText(f"{self._current_path}  ·  {len(entries)} 项")
        self._update_action_state()

    # ── 选择与导航 ─────────────────────────────────────────────

    def _selected_entries(self) -> list:
        rows = self.table.selectionModel().selectedRows()
        out = []
        for idx in rows:
            row = idx.row()
            name_item = self.table.item(row, 0)
            typ_item = self.table.item(row, 2)
            if not name_item or not typ_item:
                continue
            out.append({
                "name": name_item.text(),
                "remote": posixpath.normpath(
                    posixpath.join(self._current_path, name_item.text())),
                "is_dir": typ_item.text() == "目录",
            })
        return out

    def _open_item(self, row: int, _col: int):
        name_item = self.table.item(row, 0)
        typ_item = self.table.item(row, 2)
        if not name_item or not typ_item or typ_item.text() != "目录":
            return
        self.navigate(posixpath.join(self._current_path, name_item.text()))

    def _open_current_item(self):
        row = self.table.currentRow()
        if row >= 0:
            self._open_item(row, 0)

    def _update_action_state(self):
        has_selection = bool(self._selected_entries())
        enabled = self._connected and not self._busy
        self.uploadBtn.setEnabled(enabled)
        self.mkdirBtn.setEnabled(enabled)
        self.downloadBtn.setEnabled(enabled and has_selection)
        self.deleteBtn.setEnabled(enabled and has_selection)
        self.rootBtn.setEnabled(self._connected)
        self.upBtn.setEnabled(self._connected)
        self.refreshBtn.setEnabled(self._connected)
        self.addressEdit.setEnabled(self._connected)

    # ── 上传 / 下载 / 删除 / 新建目录 ─────────────────────────

    def _send(self, req: dict, label: str):
        self._pending += 1
        self._busy = True
        self.statusLabel.setText(label)
        self._update_action_state()
        self.sht.sigSftp.emit(req)

    def _upload_files(self):
        if not self._connected or self._busy:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择要上传的文件", "", "所有文件 (*.*)")
        if not paths:
            return
        for local in paths:
            name = local.replace("\\", "/").rsplit("/", 1)[-1]
            remote = posixpath.normpath(
                posixpath.join(self._current_path, name))
            self._send({"op": "upload", "local": local, "remote": remote},
                       f"正在上传：{name}")

    def _download_selected(self):
        entries = [e for e in self._selected_entries() if not e["is_dir"]]
        if not entries:
            InfoBar.warning(title="未选择文件",
                            content="暂不支持下载目录，请选择文件",
                            duration=3500, parent=self)
            return
        local_dir = QFileDialog.getExistingDirectory(self, "选择保存目录")
        if not local_dir:
            return
        local_dir = local_dir.replace("\\", "/")
        for e in entries:
            local = posixpath.join(local_dir, e["name"])
            self._send({"op": "download", "remote": e["remote"],
                        "local": local}, f"正在下载：{e['name']}")

    def _delete_selected(self):
        entries = self._selected_entries()
        if not entries or self._busy:
            return
        names = [e["name"] for e in entries]
        preview = "\n".join(f"• {name}" for name in names[:8])
        if len(names) > 8:
            preview += f"\n…以及另外 {len(names) - 8} 项"
        box = MessageBox(
            "确认永久删除",
            f"以下内容将从远端永久删除，且无法恢复：\n\n{preview}",
            self)
        box.yesButton.setText("永久删除")
        box.cancelButton.setText("取消")
        if not box.exec():
            return
        for e in entries:
            if e["is_dir"]:
                self._send({"op": "delete_dir", "path": e["remote"]},
                           f"正在删除：{e['name']}")
            else:
                self._send({"op": "delete", "path": e["remote"]},
                           f"正在删除：{e['name']}")

    def _mkdir(self):
        if not self._connected or self._busy:
            return
        name, ok = QInputDialog.getText(
            self, "新建目录", "目录名称：")
        name = (name or "").strip().strip("/")
        if not ok or not name:
            return
        target = posixpath.normpath(
            posixpath.join(self._current_path, name))
        self._send({"op": "mkdir", "path": target}, f"正在创建：{name}")

    # ── 操作结果 ───────────────────────────────────────────────

    def _on_sftp_result(self, r: dict):
        op = r.get("op")
        ok = bool(r.get("ok"))
        if op == "list":
            if ok:
                self._fill_table(r.get("data") or [])
            else:
                self.loadingBar.stop()
                self.loadingBar.hide()
                InfoBar.error(title="目录读取失败",
                              content=str(r.get("error")),
                              duration=5000, parent=self)
            return

        # 文件操作：计数归零后统一提示并刷新
        self._pending = max(0, self._pending - 1)
        if not ok:
            InfoBar.error(title=f"SFTP {op} 失败",
                          content=str(r.get("error")),
                          duration=5000, parent=self)
        if self._pending == 0:
            self._busy = False
            self._update_action_state()
            if ok:
                InfoBar.success(title="操作完成",
                                content="SFTP 文件操作已完成",
                                duration=2500, parent=self)
                self.navigate(self._current_path)
