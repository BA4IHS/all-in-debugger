# coding: utf-8
"""SSH 工作线程：唯一持有 paramiko 客户端 / shell channel / SFTP 的地方。

流程：连接（密码或私钥）→ invoke_shell 开交互终端（rx 轮询 + 写/resize），
SFTP 与命令执行作为独立操作复用同一 SSH 连接；MCP 查询走 sigMcpQuery → mcpReply。
"""
import socket
import stat as statmod
import threading
import time

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal, pyqtSlot

try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:
    paramiko = None
    HAS_PARAMIKO = False

# MCP exec/list 结果中单字段截断上限，避免超大输出撑爆应答
EXEC_TEXT_CAP = 32768


def paramiko_info() -> str:
    if not HAS_PARAMIKO:
        return "缺少 paramiko 依赖（pip install paramiko）"
    return f"paramiko {paramiko.__version__}"


class SshWorker(QObject):
    # ── worker → UI ────────────────────────────────────────────
    connected = pyqtSignal(dict)        # {host, port, username}
    connectFailed = pyqtSignal(str)
    closed = pyqtSignal()
    rxData = pyqtSignal(bytes)
    errorOccurred = pyqtSignal(str)
    sftpResult = pyqtSignal(dict)       # {op, ok, data|error}
    mcpReply = pyqtSignal(dict)         # MCP 查询应答 {op, id, data|error}
    finished = pyqtSignal()

    def __init__(self, client_factory=None):
        super().__init__()
        # client_factory 仅供测试注入 fake paramiko 客户端
        self._client_factory = client_factory
        self._client = None
        self._chan = None
        self._sftp = None
        self._connected = False
        self._info = {}
        self._quit = threading.Event()

    # ── UI → worker：连接管理 ─────────────────────────────────

    @pyqtSlot(dict)
    def requestConnect(self, cfg: dict):
        """cfg: {host, port, username, password, key_path, timeout, cols, rows}"""
        if not HAS_PARAMIKO:
            self.connectFailed.emit(paramiko_info())
            return
        if self._connected:
            self.connectFailed.emit("SSH 已连接，请先断开")
            return
        cfg = dict(cfg or {})
        host = str(cfg.get("host") or "").strip()
        port = int(cfg.get("port") or 22)
        username = str(cfg.get("username") or "").strip()
        if not host or not username:
            self.connectFailed.emit("请填写主机与用户名")
            return
        timeout = float(cfg.get("timeout") or 10)
        key_path = str(cfg.get("key_path") or "").strip()
        password = cfg.get("password") or None
        try:
            client = (self._client_factory() if self._client_factory
                      else paramiko.SSHClient())
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            kwargs = dict(
                hostname=host, port=port, username=username,
                timeout=timeout, allow_agent=False, look_for_keys=False)
            if key_path:          # 私钥优先
                kwargs["key_filename"] = key_path
            elif password:
                kwargs["password"] = password
            client.connect(**kwargs)
            try:
                client.get_transport().set_keepalive(15)
            except Exception:     # noqa: BLE001 - fake/异常传输不致命
                pass
            chan = client.invoke_shell(
                term="xterm",
                width=int(cfg.get("cols") or 80),
                height=int(cfg.get("rows") or 24))
            chan.settimeout(0.0)
            self._client = client
            self._chan = chan
            self._connected = True
            self._info = {"host": host, "port": port, "username": username}
        except Exception as e:    # noqa: BLE001 - 连接层统一报错出口
            self._cleanup()
            self.connectFailed.emit(f"SSH 连接失败：{e}")
            return
        self.connected.emit(dict(self._info))

    @pyqtSlot()
    def requestClose(self):
        was = self._connected
        self._cleanup()
        if was:
            self.closed.emit()

    def _cleanup(self):
        for closer in (
                lambda: self._sftp and self._sftp.close(),
                lambda: self._chan and self._chan.close(),
                lambda: self._client and self._client.close()):
            try:
                closer()
            except Exception:     # noqa: BLE001
                pass
        self._sftp = None
        self._chan = None
        self._client = None
        self._connected = False

    # ── UI → worker：交互终端 ─────────────────────────────────

    @pyqtSlot(bytes)
    def requestWrite(self, data: bytes):
        if self._chan is None or not self._connected:
            return
        try:
            self._chan.send(bytes(data))
        except Exception as e:    # noqa: BLE001
            self.errorOccurred.emit(f"SSH 写入失败：{e}")

    @pyqtSlot(int, int)
    def requestResize(self, cols: int, rows: int):
        if self._chan is None or not self._connected:
            return
        try:
            self._chan.resize_pty(width=max(2, int(cols)),
                                  height=max(2, int(rows)))
        except Exception:         # noqa: BLE001 - resize 失败不致命
            pass

    # ── UI → worker：SFTP ─────────────────────────────────────

    @pyqtSlot(dict)
    def requestSftp(self, req: dict):
        """req: {op:'list'|'upload'|'download'|'mkdir'|'delete', ...}"""
        op = str((req or {}).get("op", "list"))
        if not self._connected or self._client is None:
            self.sftpResult.emit({"op": op, "ok": False,
                                  "error": "SSH 未连接"})
            return
        try:
            if self._sftp is None:
                self._sftp = self._client.open_sftp()
            if op == "list":
                data = self._sftp_list(str(req.get("path") or "."))
            elif op == "download":
                self._sftp.get(str(req["remote"]), str(req["local"]))
                data = {"remote": req["remote"], "local": req["local"]}
            elif op == "upload":
                self._sftp.put(str(req["local"]), str(req["remote"]))
                data = {"local": req["local"], "remote": req["remote"]}
            elif op == "mkdir":
                self._sftp.mkdir(str(req["path"]))
                data = {"path": req["path"]}
            elif op == "delete":
                self._sftp.remove(str(req["path"]))
                data = {"path": req["path"]}
            else:
                self.sftpResult.emit({"op": op, "ok": False,
                                      "error": f"未知操作 {op}"})
                return
            self.sftpResult.emit({"op": op, "ok": True, "data": data})
        except Exception as e:    # noqa: BLE001
            self.sftpResult.emit({"op": op, "ok": False, "error": str(e)})

    def _sftp_list(self, path: str) -> list:
        out = []
        # listdir_attr 返回 SFTPAttributes 列表（文件名在 .filename）
        for attr in self._sftp.listdir_attr(path):
            name = getattr(attr, "filename", "") or ""
            mode = getattr(attr, "st_mode", 0) or 0
            is_dir = bool(mode) and statmod.S_ISDIR(mode)
            out.append({"name": name,
                        "size": int(getattr(attr, "st_size", 0) or 0),
                        "type": "dir" if is_dir else "file"})
        out.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
        return out

    # ── 命令执行（MCP）────────────────────────────────────────

    def _do_exec(self, cmd: str, timeout: float) -> dict:
        if not self._connected or self._client is None:
            return {"error": "SSH 未连接"}
        try:
            _in, out, err = self._client.exec_command(
                str(cmd), timeout=max(1.0, float(timeout)))
            exit_code = out.channel.recv_exit_status()
            stdout = out.read().decode("utf-8", "replace")
            stderr = err.read().decode("utf-8", "replace")
            return {"exit": int(exit_code),
                    "stdout": stdout[:EXEC_TEXT_CAP],
                    "stderr": stderr[:EXEC_TEXT_CAP]}
        except Exception as e:    # noqa: BLE001
            return {"error": str(e)}

    @pyqtSlot(dict)
    def requestExec(self, req: dict):
        """req: {id, cmd, timeout}；结果经 mcpReply（op='exec'）返回。"""
        rid = (req or {}).get("id")
        res = self._do_exec(str((req or {}).get("cmd") or ""),
                            float((req or {}).get("timeout") or 15))
        if "error" in res:
            self.mcpReply.emit({"op": "exec", "id": rid,
                                "error": res["error"]})
        else:
            self.mcpReply.emit({"op": "exec", "id": rid, "data": res})

    # ── MCP 只读查询（sigMcpQuery → mcpReply）─────────────────

    @pyqtSlot(dict)
    def requestMcpQuery(self, q: dict):
        """q: {op:'snapshot'|'exec'|'list', ...}"""
        op = str(q.get("op", "snapshot"))
        rid = q.get("id")
        if op == "snapshot":
            data = {"connected": self._connected, "library": paramiko_info()}
            if self._connected:
                data.update(self._info)
            self.mcpReply.emit({"op": op, "id": rid, "data": data})
        elif op == "exec":
            res = self._do_exec(str(q.get("cmd") or ""),
                                float(q.get("timeout") or 15))
            if "error" in res:
                self.mcpReply.emit({"op": op, "id": rid,
                                    "error": res["error"]})
            else:
                self.mcpReply.emit({"op": op, "id": rid, "data": res})
        elif op == "list":
            if not self._connected or self._client is None:
                self.mcpReply.emit({"op": op, "id": rid,
                                    "error": "SSH 未连接"})
                return
            try:
                if self._sftp is None:
                    self._sftp = self._client.open_sftp()
                self.mcpReply.emit({"op": op, "id": rid, "data": {
                    "path": str(q.get("path") or "."),
                    "entries": self._sftp_list(str(q.get("path") or "."))}})
            except Exception as e:    # noqa: BLE001
                self.mcpReply.emit({"op": op, "id": rid, "error": str(e)})
        else:
            self.mcpReply.emit({"op": op, "id": rid,
                                "error": f"未知查询 {op}"})

    @pyqtSlot()
    def requestQuit(self):
        self._quit.set()

    # ── 事件循环（worker 线程）────────────────────────────────

    def run(self):
        from PyQt6.QtCore import QCoreApplication

        while not self._quit.is_set():
            QCoreApplication.processEvents()
            if self._quit.is_set():
                break
            self._poll_rx()
            time.sleep(0.02)
        self._cleanup()
        self.finished.emit()

    def _poll_rx(self):
        chan = self._chan
        if chan is None or not self._connected:
            return
        try:
            while True:
                data = chan.recv(4096)
                if not data:
                    break
                self.rxData.emit(bytes(data))
        except socket.timeout:
            pass                  # 非阻塞轮询的正常空读
        except Exception as e:    # noqa: BLE001 - 通道异常按断开处理
            self._cleanup()
            self.errorOccurred.emit(f"SSH 连接断开：{e}")
            self.closed.emit()
            return
        try:
            if chan.closed or chan.exit_status_ready():
                self._cleanup()
                self.closed.emit()
        except Exception:         # noqa: BLE001
            pass


class _SshWorkerThread(QThread):

    def __init__(self, worker, parent=None):
        super().__init__(parent)
        self._worker = worker

    def run(self):
        self._worker.run()


class SshThread(QObject):
    """QThread 启停辅助，用法与 SerialThread / ModbusThread 一致。"""

    sigConnect = pyqtSignal(dict)
    sigClose = pyqtSignal()
    sigWrite = pyqtSignal(bytes)
    sigResize = pyqtSignal(int, int)
    sigSftp = pyqtSignal(dict)
    sigExec = pyqtSignal(dict)
    sigMcpQuery = pyqtSignal(dict)      # MCP 查询请求

    def __init__(self, parent=None, client_factory=None):
        super().__init__(parent)
        self.worker = SshWorker(client_factory=client_factory)
        self.thread = _SshWorkerThread(self.worker, self)
        self.worker.moveToThread(self.thread)

        queued = Qt.ConnectionType.QueuedConnection
        self.sigConnect.connect(self.worker.requestConnect, queued)
        self.sigClose.connect(self.worker.requestClose, queued)
        self.sigWrite.connect(self.worker.requestWrite, queued)
        self.sigResize.connect(self.worker.requestResize, queued)
        self.sigSftp.connect(self.worker.requestSftp, queued)
        self.sigExec.connect(self.worker.requestExec, queued)
        self.sigMcpQuery.connect(self.worker.requestMcpQuery, queued)

    def start(self):
        self.thread.start()

    def stop(self, timeout_ms: int = 2000):
        self.worker.requestQuit()
        if not self.thread.wait(timeout_ms):
            self.thread.wait(500)

    @property
    def isRunning(self) -> bool:
        return self.thread.isRunning()
