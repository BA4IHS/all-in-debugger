# coding: utf-8
"""SSH 工作线程：唯一持有 paramiko 客户端 / shell channel / SFTP 的地方。

流程：连接（密码或私钥）→ invoke_shell 开交互终端（rx 轮询 + 写/resize），
SFTP 与命令执行作为独立操作复用同一 SSH 连接；MCP 查询走 sigMcpQuery → mcpReply。
"""
import base64
import hashlib
import logging
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
log = logging.getLogger(__name__)


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
    """paramiko 版本描述（供 SSH 页依赖标签）。

    用 importlib.metadata 读已安装包元数据取版本，避免仅为显示一个
    版本字符串就 import paramiko（~150ms）——该标签在 SSH 页构造时
    就要显示，若走真导入会把 paramiko 拖进启动链，令“延迟导入”
    形同虚设。真实的可用性判断仍由 has_paramiko()（会真正 import）
    在连接时把关，不影响功能。
    """
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return f"paramiko {version('paramiko')}"
        except PackageNotFoundError:
            return "缺少 paramiko 依赖（pip install paramiko）"
    except Exception:  # noqa: BLE001 - 极老环境无 importlib.metadata：回退真导入
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


def _drain_channel(recv_fn, sink: list, stop: threading.Event,
                   on_chunk=None) -> None:
    """把一路通道输出排进 sink（字节块列表），直到 EOF/异常/stop。

    通道为短超时轮询：socket.timeout 只表示“暂无数据”，继续等；
    其它异常视为流结束。stop 置位后退出，已读部分保留在 sink。
    on_chunk 非空时每收一块即回调（终端实时回显，来自排空线程）。
    """
    while not stop.is_set():
        try:
            data = recv_fn(4096)
        except socket.timeout:
            time.sleep(0.01)
            continue
        except Exception:     # noqa: BLE001 - 通道被关/传输断开
            return
        if not data:
            return
        sink.append(bytes(data))
        if on_chunk is not None:
            on_chunk(bytes(data))


def _join_text(chunks: list) -> str:
    return b"".join(chunks).decode("utf-8", "replace")[:EXEC_TEXT_CAP]


def _ensure_crlf(data: bytes) -> bytes:
    """把裸 LF 补成 CRLF，供终端回显对齐（已有 CRLF 不重复补）。

    换行对齐靠 PTY 的 ONLCR 把 LF 翻译成 CRLF；exec 通道无 PTY，远端
    只发裸 LF，直接注入终端仿真会只下移不回行首，输出一行比一行偏。
    按块处理：块边界偶发 \\r\\r\\n 无害（连续两次回行首等价）。
    """
    out = bytearray()
    for b in data:
        if b == 0x0A and (not out or out[-1] != 0x0D):
            out.append(0x0D)
        out.append(b)
    return bytes(out)


class SshWorker(QObject):
    # ── worker → UI ────────────────────────────────────────────
    connected = pyqtSignal(dict)        # {host, port, username[, host_key]}
    connectFailed = pyqtSignal(str)
    hostKeyMismatch = pyqtSignal(dict)  # 服务器指纹与已记录值不一致
    closed = pyqtSignal()
    rxData = pyqtSignal(bytes)
    execEcho = pyqtSignal(bytes)        # 命令执行的终端回显（横幅/输出/退出）
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
        # 在途远端命令计数（_start_exec 独立线程增减），经 snapshot 的
        # exec_running 暴露给 MCP，让调用方能区分“worker 卡死”与
        # “长命令仍在执行”
        self._exec_lock = threading.Lock()
        self._exec_running = 0

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
            log.warning("SSH 主机密钥校验失败 %s:%s：记录 %s 实际 %s",
                        host, port, e.expected, e.actual)
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
            log.warning("SSH 连接失败 %s:%s：%s", host, port, e)
            self.connectFailed.emit(f"SSH 连接失败：{e}")
            return
        if key_box and key_box["status"] in ("new", "updated"):
            # 首次连接 / 用户确认信任新密钥后，落盘指纹供下次比对
            save_known_host(key_box["host_id"], key_box["key_type"],
                            key_box["fingerprint"])
        log.info("SSH 已连接：%s@%s:%s", self._info.get("username"),
                 host, port)
        self.connected.emit(dict(self._info))

    @pyqtSlot()
    def requestClose(self):
        was = self._connected
        self._cleanup()
        if was:
            log.info("SSH 已断开")
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
            log.warning("SSH 写入失败：%s", e)
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
                # rm -rf 也走独立线程：大目录删除可持续数十秒，同步执行
                # 会冻结 worker 事件循环（终端输出/其它请求全部卡住）
                def _rm_done(res, _op=op, _path=abs_target):
                    if "error" in res:
                        self.sftpResult.emit({"op": _op, "ok": False,
                                              "error": res["error"]})
                    else:
                        self.sftpResult.emit({"op": _op, "ok": True,
                                              "data": {"path": _path}})
                self._start_exec(f"rm -rf -- {shlex.quote(abs_target)}",
                                 120, _rm_done)
                return
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

    def _do_exec(self, cmd: str, timeout: float, on_chunk=None) -> dict:
        """执行单条远端命令，严格按 timeout 截止（秒）；on_chunk 供实时回显。

        历史实现先 recv_exit_status() 再 read()：paramiko 的
        recv_exit_status() 无超时且要求先排空输出，远端输出超过通道
        窗口即互相等待死锁，timeout 形同虚设。改为：
        - 双线程持续排空 stdout/stderr（短超时轮询，保留已读部分）；
        - 主线程轮询 exit_status_ready()，到点 close() 通道强制返回，
          错误信息附输出尾部便于排查。
        """
        if not self._connected or self._client is None:
            return {"error": "SSH 未连接"}
        tmo = max(0.05, float(timeout))
        try:
            _in, out, _err = self._client.exec_command(
                str(cmd), timeout=max(1.0, tmo))
            chan = out.channel
            chan.settimeout(0.05)   # 排空线程短轮询，静默期不误判超时
        except Exception as e:    # noqa: BLE001
            return {"error": str(e)}
        stop = threading.Event()
        chunks_out: list = []
        chunks_err: list = []
        drains = [
            threading.Thread(
                target=_drain_channel,
                args=(chan.recv, chunks_out, stop, on_chunk), daemon=True),
            threading.Thread(
                target=_drain_channel,
                args=(chan.recv_stderr, chunks_err, stop, on_chunk),
                daemon=True),
        ]
        for t in drains:
            t.start()
        deadline = time.monotonic() + tmo
        timed_out = False
        while not chan.exit_status_ready():
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.05)
        if timed_out:
            try:
                chan.close()
            except Exception:     # noqa: BLE001
                pass
        stop.set()
        for t in drains:
            t.join(1.0)
        stdout = _join_text(chunks_out)
        stderr = _join_text(chunks_err)
        if timed_out:
            tail = (stdout or stderr)[-200:]
            msg = f"命令执行超时（{tmo:g}s），通道已关闭"
            if tail:
                msg += f"；输出尾部：{tail}"
            return {"error": msg}
        try:
            exit_code = int(chan.recv_exit_status())
        except Exception:         # noqa: BLE001 - 通道异常时退化为 -1
            exit_code = -1
        return {"exit": exit_code, "stdout": stdout, "stderr": stderr}

    def _echo_line(self, text: str, color: str = "33"):
        """向终端注入一行彩色回显（\r\n 开头，避开当前 shell 提示行）。

        text 内的裸 LF 一并补 CR（失败详情含远端输出尾部时是多行文本）。
        """
        self.execEcho.emit(_ensure_crlf(
            f"\r\n\x1b[{color}m{text}\x1b[0m\r\n".encode("utf-8")))

    def _start_exec(self, cmd: str, timeout: float, on_done,
                    echo: bool = False) -> None:
        """把 _do_exec 挪到独立线程执行，结果经 on_done(res) 回传。

        命令执行原本跑在 worker 事件循环（processEvents）里，一条长命令
        （如 apt install）会冻结全部 queued 请求与终端输出轮询，其它
        MCP 调用只能拿到「查询超时（worker 未运行或正忙）」。独立线程让
        事件循环保持响应；paramiko transport 本身线程安全（每次 exec 是
        独立 channel），在途数量经 exec_running 暴露给 ssh_status。

        echo=True 时把执行过程渲染进终端（execEcho → UI）：先黄色横幅
        显示命令，输出实时透传，结束显示退出码/失败原因——agent 在后台
        执行什么，用户在窗口里看得见。SFTP 删除目录等 UI 自身发起的
        内部命令不回显（已有状态栏提示，避免刷屏）。
        """
        on_chunk = None
        if echo:
            self._echo_line(f"[AI] $ {cmd}")
            on_chunk = lambda data: self.execEcho.emit(_ensure_crlf(data))
        with self._exec_lock:
            self._exec_running += 1

        def run():
            try:
                res = self._do_exec(cmd, timeout, on_chunk=on_chunk)
            except Exception as e:  # noqa: BLE001 - 兜底，防线程静默消失
                res = {"error": f"命令执行失败：{e}"}
            with self._exec_lock:
                self._exec_running -= 1
            if echo:
                if "error" in res:
                    self._echo_line(f"[AI] 失败：{res['error']}", "31")
                else:
                    code = res.get("exit")
                    self._echo_line(f"[AI] exit={code}",
                                    "32" if code == 0 else "31")
            on_done(res)

        threading.Thread(target=run, daemon=True, name="ssh-exec").start()

    def _emit_exec_reply(self, op: str, rid, res: dict):
        """命令结果统一经 mcpReply 回传（error / data 二选一）。"""
        if "error" in res:
            self.mcpReply.emit({"op": op, "id": rid, "error": res["error"]})
        else:
            self.mcpReply.emit({"op": op, "id": rid, "data": res})

    @pyqtSlot(dict)
    def requestExec(self, req: dict):
        """req: {id, cmd, timeout}；结果经 mcpReply（op='exec'）异步返回。"""
        rid = (req or {}).get("id")
        self._start_exec(str((req or {}).get("cmd") or ""),
                         float((req or {}).get("timeout") or 15),
                         lambda res: self._emit_exec_reply("exec", rid, res),
                         echo=True)

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
            with self._exec_lock:
                data["exec_running"] = self._exec_running
            self.mcpReply.emit({"op": op, "id": rid, "data": data})
        elif op == "exec":
            # 异步执行：长命令不得冻结 worker 事件循环（详见 _start_exec）
            self._start_exec(
                str(q.get("cmd") or ""), float(q.get("timeout") or 15),
                lambda res: self._emit_exec_reply(op, rid, res), echo=True)
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
