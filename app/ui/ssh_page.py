# coding: utf-8
"""SSH 调试页：交互式终端（paramiko + pyte 复用）+ 会话保存 + SFTP 面板。

- 左栏：连接卡（主机/端口/用户名/密码或私钥）、会话卡（保存/加载/删除，不存密码）
- 右上：复用 QTerminalWidget 交互终端，窗口尺寸变化自动 resize_pty
- 右下：SFTP 文件面板（列目录/进入/上传/下载/删除）
"""
import posixpath

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHeaderView, QHBoxLayout, QSplitter,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, ComboBox, FluentIcon, InfoBar,
    LineEdit, PrimaryPushButton, PushButton, SingleDirectionScrollArea,
    SpinBox, SubtitleLabel, TableWidget, ToolButton,
)

from app import ssh_worker as sw
from app.config import loadData, saveData
from app.ui.terminal_widget import QTerminalWidget

SESSIONS_KEY = "ssh_sessions"
SESSION_FIELDS = ("name", "host", "port", "username", "auth", "key_path")


def session_record(name: str, host: str, port: int, username: str,
                   auth: str, key_path: str) -> dict:
    """构造会话记录；仅白名单字段，密码绝不落盘。"""
    return {"name": str(name), "host": str(host), "port": int(port),
            "username": str(username), "auth": str(auth),
            "key_path": str(key_path)}


def merge_sessions(existing, rec: dict) -> list:
    """按名称去重合并（同名覆盖），返回新列表。"""
    out = [dict(s) for s in (existing or [])
           if s.get("name") != rec.get("name")]
    out.append(dict(rec))
    return out


class SshPage(QWidget):

    def __init__(self, sht: "sw.SshThread", parent=None):
        super().__init__(parent)
        self.sht = sht
        self._connected = False
        self._sftpPath = "."

        scroll = SingleDirectionScrollArea(self)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(12)
        ll.addWidget(self._build_connect_card())
        ll.addWidget(self._build_session_card())
        ll.addStretch(1)
        scroll.setWidget(left)
        scroll.setFixedWidth(330)
        scroll.setWidgetResizable(True)
        scroll.enableTransparentBackground()
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        splitter = QSplitter(Qt.Orientation.Vertical, self)
        splitter.addWidget(self._build_terminal())
        splitter.addWidget(self._build_sftp_card())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setChildrenCollapsible(False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 40, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(scroll)
        layout.addWidget(splitter, 1)

        self._connect_signals()

    # ── 左：连接卡 ─────────────────────────────────────────────

    def _build_connect_card(self) -> CardWidget:
        card = CardWidget()
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(8)
        v.addWidget(SubtitleLabel("连接", card))

        self.libLabel = CaptionLabel(sw.paramiko_info(), card)
        self.libLabel.setWordWrap(True)
        v.addWidget(self.libLabel)

        self.hostEdit = LineEdit(card)
        self.hostEdit.setPlaceholderText("主机地址，如 192.168.1.10")
        v.addWidget(BodyLabel("主机", card))
        v.addWidget(self.hostEdit)

        pr = QHBoxLayout()
        pr.addWidget(BodyLabel("端口", card))
        self.portBox = SpinBox(card)
        self.portBox.setRange(1, 65535)
        self.portBox.setValue(22)
        self.portBox.setMinimumWidth(80)
        pr.addStretch(1)
        pr.addWidget(self.portBox)
        v.addLayout(pr)

        self.userEdit = LineEdit(card)
        self.userEdit.setPlaceholderText("登录用户名，如 root")
        v.addWidget(BodyLabel("用户名", card))
        v.addWidget(self.userEdit)

        ar = QHBoxLayout()
        ar.addWidget(BodyLabel("认证方式", card))
        self.authCombo = ComboBox(card)
        self.authCombo.addItems(["密码", "私钥文件"])
        self.authCombo.setMinimumWidth(110)
        self.authCombo.currentIndexChanged.connect(self._on_auth_changed)
        ar.addWidget(self.authCombo, 1)
        v.addLayout(ar)

        self.passEdit = LineEdit(card)
        self.passEdit.setEchoMode(LineEdit.EchoMode.Password)
        self.passEdit.setPlaceholderText("登录密码")
        v.addWidget(self.passEdit)

        kr = QHBoxLayout()
        self.keyEdit = LineEdit(card)
        self.keyEdit.setPlaceholderText("私钥文件路径（OpenSSH/RSA）")
        browse = ToolButton(FluentIcon.FOLDER, card)
        browse.setToolTip("选择私钥文件")
        browse.clicked.connect(self._browse_key)
        kr.addWidget(self.keyEdit, 1)
        kr.addWidget(browse)
        self.keyRow = QWidget(card)
        self.keyRow.setLayout(kr)
        self.keyRow.setVisible(False)
        v.addWidget(self.keyRow)

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

    # ── 左：会话卡 ─────────────────────────────────────────────

    def _build_session_card(self) -> CardWidget:
        card = CardWidget()
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(8)
        v.addWidget(SubtitleLabel("会话", card))

        nr = QHBoxLayout()
        self.nameEdit = LineEdit(card)
        self.nameEdit.setPlaceholderText("会话名称")
        saveBtn = PushButton("保存", card)
        saveBtn.clicked.connect(self._save_session)
        nr.addWidget(self.nameEdit, 1)
        nr.addWidget(saveBtn)
        v.addLayout(nr)

        lr = QHBoxLayout()
        self.sessionCombo = ComboBox(card)
        loadBtn = PushButton("加载", card)
        delBtn = PushButton("删除", card)
        loadBtn.clicked.connect(self._load_session)
        delBtn.clicked.connect(self._delete_session)
        lr.addWidget(self.sessionCombo, 1)
        lr.addWidget(loadBtn)
        lr.addWidget(delBtn)
        v.addLayout(lr)

        hint = CaptionLabel("会话只保存连接参数（不含密码），存于 data.json。",
                             card)
        hint.setWordWrap(True)
        v.addWidget(hint)
        self._reload_session_combo()
        return card

    # ── 右：终端 ───────────────────────────────────────────────

    def _build_terminal(self) -> QWidget:
        wrap = CardWidget(self)
        v = QVBoxLayout(wrap)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(6)
        bar = QHBoxLayout()
        bar.addWidget(SubtitleLabel("终端", wrap))
        clearBtn = ToolButton(FluentIcon.DELETE, wrap)
        clearBtn.setToolTip("清屏")
        clearBtn.clicked.connect(lambda _=False: self.terminal.clear())
        bar.addStretch(1)
        bar.addWidget(clearBtn)
        v.addLayout(bar)
        self.terminal = QTerminalWidget(wrap)
        self.terminal.set_enter_mode("\r")
        # 网格尺寸变化 → 同步远端 PTY
        self.terminal.set_resize_cb(
            lambda c, r: self.sht.sigResize.emit(c, r))
        v.addWidget(self.terminal, 1)
        return wrap

    # ── 右：SFTP 面板 ──────────────────────────────────────────

    def _build_sftp_card(self) -> CardWidget:
        card = CardWidget()
        v = QVBoxLayout(card)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(6)
        bar = QHBoxLayout()
        bar.addWidget(SubtitleLabel("SFTP", card))
        upBtn = ToolButton(FluentIcon.UP, card)
        upBtn.setToolTip("上级目录")
        upBtn.clicked.connect(self._sftp_up)
        refreshBtn = ToolButton(FluentIcon.UPDATE, card)
        refreshBtn.setToolTip("刷新")
        refreshBtn.clicked.connect(self._sftp_refresh)
        bar.addStretch(1)
        bar.addWidget(upBtn)
        bar.addWidget(refreshBtn)
        v.addLayout(bar)

        self.sftpPathEdit = LineEdit(card)
        self.sftpPathEdit.setText(".")
        self.sftpPathEdit.returnPressed.connect(self._sftp_refresh)
        v.addWidget(self.sftpPathEdit)

        self.sftpTable = TableWidget(card)
        self.sftpTable.setColumnCount(3)
        self.sftpTable.setHorizontalHeaderLabels(["名称", "大小", "类型"])
        hh = self.sftpTable.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.sftpTable.verticalHeader().setVisible(False)
        self.sftpTable.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.sftpTable.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.sftpTable.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sftpTable.cellDoubleClicked.connect(self._sftp_enter)
        v.addWidget(self.sftpTable, 1)

        brow = QHBoxLayout()
        dlBtn = PushButton("下载", card)
        ulBtn = PushButton("上传", card)
        rmBtn = PushButton("删除", card)
        dlBtn.clicked.connect(self._sftp_download)
        ulBtn.clicked.connect(self._sftp_upload)
        rmBtn.clicked.connect(self._sftp_delete)
        brow.addWidget(dlBtn, 1)
        brow.addWidget(ulBtn, 1)
        brow.addWidget(rmBtn, 1)
        v.addLayout(brow)
        return card

    # ── 信号接线 ───────────────────────────────────────────────

    def _connect_signals(self):
        w = self.sht.worker
        w.connected.connect(self._on_connected)
        w.connectFailed.connect(self._on_connect_failed)
        w.closed.connect(self._on_closed)
        w.rxData.connect(self.terminal.queue_bytes)
        w.errorOccurred.connect(self._on_error)
        w.sftpResult.connect(self._on_sftp_result)
        self.terminal.sendRequested.connect(
            lambda data: self.sht.sigWrite.emit(data))
        self.connectBtn.clicked.connect(self._on_connect)
        self.closeBtn.clicked.connect(lambda _=False: self.sht.sigClose.emit())

    def _on_auth_changed(self, idx: int):
        is_key = idx == 1
        self.passEdit.setVisible(not is_key)
        self.keyRow.setVisible(is_key)

    def _browse_key(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择私钥文件", "", "所有文件 (*)")
        if path:
            self.keyEdit.setText(path)

    # ── 连接 ───────────────────────────────────────────────────

    def _on_connect(self):
        host = self.hostEdit.text().strip()
        username = self.userEdit.text().strip()
        if not sw.HAS_PARAMIKO:
            InfoBar.error(title="缺少依赖", content=sw.paramiko_info(),
                          duration=6000, parent=self)
            return
        if not host or not username:
            InfoBar.warning(title="参数不全", content="请填写主机与用户名",
                            duration=4000, parent=self)
            return
        is_key = self.authCombo.currentIndex() == 1
        cfg = {
            "host": host,
            "port": self.portBox.value(),
            "username": username,
            "password": "" if is_key else self.passEdit.text(),
            "key_path": self.keyEdit.text().strip() if is_key else "",
            "timeout": 10,
            "cols": self.terminal._cols,
            "rows": self.terminal._rows,
        }
        self.connectBtn.setEnabled(False)
        self.statusLabel.setText("连接中…")
        self.sht.sigConnect.emit(cfg)

    def _on_connected(self, info: dict):
        self._connected = True
        self.connectBtn.setEnabled(False)
        self.closeBtn.setEnabled(True)
        self.statusLabel.setText(
            f"已连接 {info.get('username')}@{info.get('host')}:"
            f"{info.get('port')}")
        self.terminal.clear()
        self.terminal.setFocus()
        self._sftpPath = "."
        self.sftpPathEdit.setText(".")
        self._sftp_refresh()

    def _on_connect_failed(self, msg: str):
        self.connectBtn.setEnabled(True)
        self.statusLabel.setText("连接失败")
        InfoBar.error(title="SSH 连接失败", content=msg,
                      duration=6000, parent=self)

    def _on_closed(self):
        self._connected = False
        self.connectBtn.setEnabled(True)
        self.closeBtn.setEnabled(False)
        self.statusLabel.setText("未连接")
        self.sftpTable.setRowCount(0)
        InfoBar.info(title="SSH 已断开", content="远端连接已关闭",
                     duration=3000, parent=self)

    def _on_error(self, msg: str):
        InfoBar.error(title="SSH 错误", content=msg,
                      duration=5000, parent=self)

    # ── 会话保存/加载（不含密码）──────────────────────────────

    def _sessions(self) -> list:
        data = loadData()
        ss = data.get(SESSIONS_KEY)
        return ss if isinstance(ss, list) else []

    def _reload_session_combo(self):
        cur = self.sessionCombo.currentText()
        self.sessionCombo.clear()
        for s in self._sessions():
            self.sessionCombo.addItem(str(s.get("name", "")))
        if cur:
            idx = self.sessionCombo.findText(cur)
            if idx >= 0:
                self.sessionCombo.setCurrentIndex(idx)

    def _save_session(self):
        name = self.nameEdit.text().strip()
        if not name:
            InfoBar.warning(title="缺少名称", content="请填写会话名称",
                            duration=3000, parent=self)
            return
        rec = session_record(
            name, self.hostEdit.text().strip(), self.portBox.value(),
            self.userEdit.text().strip(),
            self.authCombo.currentText(), self.keyEdit.text().strip())
        data = loadData()
        data[SESSIONS_KEY] = merge_sessions(self._sessions(), rec)
        saveData(data)
        self._reload_session_combo()
        InfoBar.success(title="已保存", content=f"会话「{name}」已保存",
                        duration=2500, parent=self)

    def _find_session(self, name: str):
        for s in self._sessions():
            if s.get("name") == name:
                return s
        return None

    def _load_session(self):
        name = self.sessionCombo.currentText()
        s = self._find_session(name)
        if not s:
            InfoBar.warning(title="无此会话", content="请先选择已保存的会话",
                            duration=3000, parent=self)
            return
        self.nameEdit.setText(str(s.get("name", "")))
        self.hostEdit.setText(str(s.get("host", "")))
        self.portBox.setValue(int(s.get("port") or 22))
        self.userEdit.setText(str(s.get("username", "")))
        auth = str(s.get("auth") or "密码")
        idx = self.authCombo.findText(auth)
        if idx >= 0:
            self.authCombo.setCurrentIndex(idx)
        self.keyEdit.setText(str(s.get("key_path", "")))

    def _delete_session(self):
        name = self.sessionCombo.currentText()
        ss = [s for s in self._sessions() if s.get("name") != name]
        if len(ss) == len(self._sessions()):
            return
        data = loadData()
        data[SESSIONS_KEY] = ss
        saveData(data)
        self._reload_session_combo()

    # ── SFTP ───────────────────────────────────────────────────

    def _sftp_refresh(self):
        if not self._connected:
            return
        self._sftpPath = self.sftpPathEdit.text().strip() or "."
        self.sht.sigSftp.emit({"op": "list", "path": self._sftpPath})

    def _sftp_up(self):
        if not self._connected:
            return
        parent = posixpath.dirname(posixpath.normpath(self._sftpPath))
        self.sftpPathEdit.setText(parent or "/")
        self._sftp_refresh()

    def _sftp_enter(self, row: int, _col: int):
        name_item = self.sftpTable.item(row, 0)
        type_item = self.sftpTable.item(row, 2)
        if not name_item or not type_item or type_item.text() != "目录":
            return
        joined = posixpath.normpath(
            posixpath.join(self._sftpPath, name_item.text()))
        self.sftpPathEdit.setText(joined)
        self._sftp_refresh()

    def _selected_entry(self):
        rows = self.sftpTable.selectionModel().selectedRows()
        if not rows:
            InfoBar.warning(title="未选择", content="请先在表格中选择文件",
                            duration=3000, parent=self)
            return None
        row = rows[0].row()
        name = self.sftpTable.item(row, 0).text()
        typ = self.sftpTable.item(row, 2).text()
        return {"name": name,
                "remote": posixpath.normpath(
                    posixpath.join(self._sftpPath, name)),
                "is_dir": typ == "目录"}

    def _sftp_download(self):
        e = self._selected_entry()
        if not e or e["is_dir"]:
            if e and e["is_dir"]:
                InfoBar.warning(title="不支持", content="暂不支持下载目录",
                                duration=3000, parent=self)
            return
        local_dir = QFileDialog.getExistingDirectory(self, "选择保存目录")
        if not local_dir:
            return
        local = posixpath.join(local_dir.replace("\\", "/"), e["name"])
        InfoBar.info(title="下载中", content=e["remote"],
                     duration=2000, parent=self)
        self.sht.sigSftp.emit({"op": "download", "remote": e["remote"],
                               "local": local})

    def _sftp_upload(self):
        if not self._connected:
            return
        local, _ = QFileDialog.getOpenFileName(self, "选择要上传的文件")
        if not local:
            return
        name = local.replace("\\", "/").rsplit("/", 1)[-1]
        remote = posixpath.normpath(posixpath.join(self._sftpPath, name))
        InfoBar.info(title="上传中", content=name,
                     duration=2000, parent=self)
        self.sht.sigSftp.emit({"op": "upload", "local": local,
                               "remote": remote})

    def _sftp_delete(self):
        e = self._selected_entry()
        if not e or e["is_dir"]:
            if e and e["is_dir"]:
                InfoBar.warning(title="不支持", content="暂不支持删除目录",
                                duration=3000, parent=self)
            return
        self.sht.sigSftp.emit({"op": "delete", "path": e["remote"]})

    def _on_sftp_result(self, r: dict):
        if not r.get("ok"):
            InfoBar.error(title=f"SFTP {r.get('op')} 失败",
                          content=str(r.get("error")),
                          duration=5000, parent=self)
            return
        op = r.get("op")
        if op == "list":
            self._fill_sftp_table(r.get("data") or [])
        elif op in ("download", "upload"):
            InfoBar.success(title=f"{op} 完成",
                            content=str(r.get("data")),
                            duration=3000, parent=self)
        elif op == "delete":
            self._sftp_refresh()

    def _fill_sftp_table(self, entries: list):
        t = self.sftpTable
        t.setRowCount(len(entries))
        for i, e in enumerate(entries):
            name = QTableWidgetItem(str(e.get("name", "")))
            name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            size = QTableWidgetItem(
                "" if e.get("type") == "dir"
                else self._fmt_size(int(e.get("size") or 0)))
            size.setFlags(size.flags() & ~Qt.ItemFlag.ItemIsEditable)
            typ = QTableWidgetItem("目录" if e.get("type") == "dir"
                                   else "文件")
            typ.setFlags(typ.flags() & ~Qt.ItemFlag.ItemIsEditable)
            t.setItem(i, 0, name)
            t.setItem(i, 1, size)
            t.setItem(i, 2, typ)

    @staticmethod
    def _fmt_size(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return f"{n:.0f} {unit}" if unit == "B" \
                    else f"{n:.1f} {unit}"
            n /= 1024
        return str(n)

    # ── 生命周期 ───────────────────────────────────────────────

    def shutdown(self):
        if self._connected:
            self.sht.sigClose.emit()
