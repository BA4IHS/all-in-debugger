# coding: utf-8
"""SSH 工作线程：唯一持有 paramiko 客户端 / shell channel / SFTP 的地方。

流程：连接（密码或私钥）→ invoke_shell 开交互终端（rx 轮询 + 写/resize），
SFTP 与命令执行作为独立操作复用同一 SSH 连接；MCP 查询走 sigMcpQuery → mcpReply。
"""
import base64
import hashlib
import posixpath
import shlex
import socket
import stat as statmod
import threading
import time

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal, pyqtSlot

from app.config import loadData, saveData

# paramiko 延迟导入：顶部 import 会把 paramiko+invoke（~150ms）拖进
# 启动链，而 SSH 连接前用不到；首次真正需要时才加载并缓存。
_paramiko = None


def _get_paramiko():
    """首次调用时导入 paramiko 并缓存；缺失时返回 None。"""
    global _paramiko
    if _paramiko is None:
        try:
            import paramiko as _pm
            _paramiko = _pm
        except ImportError:
            _paramiko = False
    return _paramiko or None


def has_paramiko() -> bool:
    """paramiko 是否可用（UI 据此判断能否发起连接）。"""
    return _get_paramiko() is not None


# MCP exec/list 结果中单字段截断上限，避免超大输出撑爆应答
EXEC_TEXT_CAP = 32768

# 已信任主机密钥存放在 data.json 的这个字段下（只存指纹，不存密钥本体）
HOST_KEYS_FIELD = "ssh_host_keys"


def paramiko_info() -> str:
    pm = _get_paramiko()
    if pm is None:
        return "缺少 paramiko 依赖（pip install paramiko）"
    return f"paramiko {pm.__version__}"


# ── 主机密钥校验（TOFU）─────────────────────────────────
# 旧实现用 AutoAddPolicy 无条件接受任意主机密钥，局域网内伪造 SSH
# 服务器即可截获密码/私钥与全部调试命令。改为：首次连接记录指纹并提醒
# 用户核对，之后每次连接都重新校验（不缓存信任）；指纹变更即拒绝连接，
# 需用户在弹框中确认后才更新记录。


class HostKeyMismatchError(Exception):
    """服务器主机密钥与已记录指纹不一致（可能存在中间人攻击）。"""

    def __init__(self, expected: str, actual: str, key_type: str):
        super().__init__(
            f"主机密钥指纹不一致：已记录 {expected}，服务器 {actual}")
        self.expected = expected
        self.actual = actual
        self.key_type = key_type


def host_key_id(host: str, port: int) -> str:
    """known_hosts 风格键名：22 端口用主机名，其余用 [host]:port。"""
    host = str(host or "").strip()
    port = int(port or 22)
    return host if port == 22 else f"[{host}]:{port}"


def fingerprint_of(key) -> str:
    """SHA256 指纹，与 `ssh-keygen -lf` 输出一致，便于线下核对。

    paramiko>=3.2 提供 PKey.fingerprint；requirements 允许 >=3，
    因此对更旧版本回退到自行计算。
    """
    fp = getattr(key, "fingerprint", None)
    if isinstance(fp, str) and fp.startswith("SHA256:"):
        return fp
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def load_known_hosts() -> dict:
    """读取已记录的主机密钥指纹表（损坏/缺失时返回空表）。"""
    table = loadData().get(HOST_KEYS_FIELD)
    return table if isinstance(table, dict) else {}


def save_known_host(host_id: str, key_type: str, fingerprint: str) -> None:
    """记录/更新某主机的指纹（只含公开信息，不含密钥与密码）。"""
    data = loadData()
    table = data.get(HOST_KEYS_FIELD)
    if not isinstance(table, dict):
        table = {}
    table[str(host_id)] = {"key_type": str(key_type),
                           "fingerprint": str(fingerprint),
                           "recorded": time.strftime("%Y-%m-%d %H:%M:%S")}
    data[HOST_KEYS_FIELD] = table
    saveData(data)


def make_host_key_policy(paramiko, expected: str, trust_new: bool, box: dict):
    """构造 TOFU 主机密钥策略；校验结果写入 box 供调用方读取。

    paramiko 仅在客户端 host_keys 里没有该主机时回调本策略，而这里
    刻意不写入 client._host_keys，因此每次连接都会重新校验一次。
    box: {host_id, key_type, fingerprint, status}，status 取
    new（首次记录）/ matched（与记录一致）/ updated（用户确认后更新）。
    """

    class _TofuPolicy(paramiko.MissingHostKeyPolicy):

        def missing_host_key(self, client, hostname, key):
            fp = fingerprint_of(key)
            key_type = key.get_name()
            if expected and fp != expected:
                if not trust_new:
                    raise HostKeyMismatchError(expected, fp, key_type)
                status = "updated"
            else:
                status = "matched" if expected else "new"
            box.update({"host_id": hostname, "key_type": key_type,
                        "fingerprint": fp, "status": status})

    return _TofuPolicy()


def _close_quietly(client) -> None:
    """关闭半成品客户端：连接失败时 self._client 尚未赋值，必须就地关闭，
    否则每次失败都会泄漏一个已完成握手的 socket。"""
    if client is None:
        return
    try:
        client.close()
    except Exception:     # noqa: BLE001 - 关闭失败无补救手段
        pass


class SshWorker(QObject):
    # ── worker → UI ────────────────────────────────────────────
    connected = pyqtSignal(dict)        # {host, port, username[, host_key]}
    connectFailed = pyqtSignal(str)
    hostKeyMismatch = pyqtSignal(dict)  # 服务器指纹与已记录值不一致
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
        paramiko = _get_paramiko()
        if paramiko is None:
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
        # 主机密钥 TOFU：已记录指纹用于比对，trust_new_host_key 表示用户
        # 已在弹框中确认信任变更后的新密钥
        known = load_known_hosts().get(host_key_id(host, port)) or {}
        expected = str(known.get("fingerprint") or "")
        key_box = {}
        client = None
        try:
            client = (self._client_factory() if self._client_factory
                      else paramiko.SSHClient())
            client.set_missing_host_key_policy(make_host_key_policy(
                paramiko, expected, bool(cfg.get("trust_new_host_key")),
                key_box))
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
            if key_box:
                # 校验结果随 connected 给 UI 与 MCP（指纹属公开信息）
                self._info["host_key"] = dict(key_box)
        except HostKeyMismatchError as e:
            _close_quietly(client)
            self._cleanup()
            self.hostKeyMismatch.emit(
                {"host": host, "port": port, "key_type": e.key_type,
                 "expected": e.expected, "actual": e.actual})
            # 同时发 connectFailed：MCP 桥与 UI 都以此作为失败出口
            self.connectFailed.emit(
                f"主机密钥校验失败：服务器指纹 {e.actual} 与已记录 "
                f"{e.expected} 不一致，可能存在中间人攻击，已拒绝连接")
            return
        except Exception as e:    # noqa: BLE001 - 连接层统一报错出口
            _close_quietly(client)
            self._cleanup()
            self.connectFailed.emit(f"SSH 连接失败：{e}")
            return
        if key_box and key_box["status"] in ("new", "updated"):
            # 首次连接 / 用户确认信任新密钥后，落盘指纹供下次比对
            save_known_host(key_box["host_id"], key_box["key_type"],
                            key_box["fingerprint"])
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
        """req: {op:'list'|'upload'|'download'|'mkdir'|'delete'|'delete_dir', ...}"""
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
            elif op == "delete_dir":
                target = posixpath.normpath(
                    str(req.get("path") or "").strip())
                # 空串经 normpath 会变成 '.', 同样属于根目录类危险输入
                if target in ("", ".", "/"):
                    self.sftpResult.emit({"op": op, "ok": False,
                                          "error": "禁止删除根目录"})
                    return
                # normalize 取远端绝对路径，避免 exec 与 SFTP 的 cwd 差异；
                # 归一化后再查一次根，拦截 '..'、'/..' 等变形输入
                abs_target = self._sftp.normalize(target)
                if abs_target in ("", "/"):
                    self.sftpResult.emit({"op": op, "ok": False,
                                          "error": "禁止删除根目录"})
                    return
                res = self._do_exec(f"rm -rf -- {shlex.quote(abs_target)}",
                                    120)
                if "error" in res:
                    raise RuntimeError(res["error"])
                data = {"path": abs_target}
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
