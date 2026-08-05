# coding: utf-8
"""TCP/IP 网络工作线程：唯一持有全部 socket 的地方。

线程模型：QObject worker + moveToThread(QThread)，阻塞循环里用
selectors 多路复用监听/客户端/UDP socket（select 超时 50ms，与
QCoreApplication.processEvents 交替，保证 queued 信号不饿死）。
UI 线程通过 queued slot 间接操作，严禁在 UI 线程直接触碰 socket。

模式：
- tcp_server：监听本地端口，接受多个客户端（连接列表 + 指定目标发送）
- tcp_client：连接远程主机（单连接）
- udp：绑定本地端口，向远程目标发送（支持广播 255.255.255.255）
"""
import selectors
import socket
import threading
import time

from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal, pyqtSlot

from app.tcpip_utils import format_source, validate_host, validate_port

_RECV_CHUNK = 65536
_SEND_TIMEOUT = 5.0          # 非阻塞 socket 发送的保护性超时（秒）


class TcpipWorker(QObject):
    # ── worker → UI（queued）────────────────────────────────────
    started = pyqtSignal(dict)          # {mode, local, remote}
    startFailed = pyqtSignal(str)
    stopped = pyqtSignal()
    dataReceived = pyqtSignal(bytes, float, str)  # 原始字节 + time.time() + 来源
    dataWritten = pyqtSignal(int)       # 成功发送的字节数
    clientsChanged = pyqtSignal(list)   # TCP Server 客户端地址列表
    errorOccurred = pyqtSignal(str)
    mcpReply = pyqtSignal(dict)         # MCP 查询应答 {op, id, data|error}
    finished = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._selector = selectors.DefaultSelector()
        self._quit = threading.Event()
        self._running = False
        self._mode = ""
        self._local_str = ""
        self._remote_str = ""
        self._remote = None             # UDP 发送目标 (host, port)
        self._listen_sock = None        # TCP Server 监听 socket
        self._tcp_sock = None           # TCP Client 连接 socket
        self._udp_sock = None           # UDP socket
        self._clients = {}              # {addr_str: socket}
        self._sock_source = {}          # {id(sock): addr_str}
        self._logFp = None
        self._logPath = ""

    # ── UI → worker（全部在 worker 线程执行）──────────────────────

    @pyqtSlot(dict)
    def requestStart(self, cfg: dict):
        if self._running:
            self.startFailed.emit("网络已处于连接状态")
            return
        cfg = cfg or {}
        mode = str(cfg.get("mode") or "")
        try:
            local_host = validate_host(str(cfg.get("local_host") or ""))
            local_port = validate_port(cfg.get("local_port") or 0,
                                       allow_zero=True)
            if mode == "tcp_server":
                # 服务端监听不依赖远程地址，忽略 remote 字段
                remote_host = ""
                remote_port = 0
            else:
                remote_host = validate_host(str(cfg.get("remote_host") or ""))
                remote_port = validate_port(cfg.get("remote_port") or 0)
        except ValueError as e:
            self.startFailed.emit(str(e))
            return

        try:
            if mode == "tcp_server":
                ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                ls.bind((local_host, local_port))
                ls.listen(8)
                ls.setblocking(False)
                self._listen_sock = ls
                self._selector.register(ls, selectors.EVENT_READ)
                self._local_str = format_source(*ls.getsockname()[:2])
                self._remote_str = ""
            elif mode == "tcp_client":
                if not remote_host:
                    raise ValueError("TCP Client 需要远程地址")
                cs = socket.create_connection((remote_host, remote_port),
                                              timeout=8)
                cs.setblocking(False)
                self._tcp_sock = cs
                self._selector.register(cs, selectors.EVENT_READ)
                self._local_str = format_source(*cs.getsockname()[:2])
                self._remote_str = format_source(remote_host, remote_port)
            elif mode == "udp":
                us = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                us.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                us.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                us.bind((local_host, local_port))
                us.setblocking(False)
                self._udp_sock = us
                self._selector.register(us, selectors.EVENT_READ)
                self._local_str = format_source(*us.getsockname()[:2])
                self._remote_str = format_source(remote_host, remote_port)
                self._remote = (remote_host, remote_port)
            else:
                raise ValueError(f"未知模式：{mode}")
        except Exception as e:          # noqa: BLE001
            self._stop_all()
            self.startFailed.emit(str(e))
            return
        self._mode = mode
        self._running = True
        self.started.emit({"mode": mode, "local": self._local_str,
                           "remote": self._remote_str})

    @pyqtSlot()
    def requestStop(self):
        if not self._running:
            return
        self._stop_all()
        self._running = False
        self._mode = ""
        self.stopped.emit()

    @pyqtSlot(dict)
    def requestSend(self, req: dict):
        """req: {data: bytes, target: str}；TCP Server 时 target 为
        客户端地址或 'ALL'，其余模式忽略。"""
        data = bytes((req or {}).get("data") or b"")
        target = str((req or {}).get("target") or "")
        if not self._running:
            self.errorOccurred.emit("网络未连接，无法发送")
            return
        try:
            if self._mode == "tcp_server":
                if target == "ALL":
                    for s in list(self._clients.values()):
                        self._sendall(s, data)
                elif target:
                    s = self._clients.get(target)
                    if s is None:
                        self.errorOccurred.emit(f"客户端不存在：{target}")
                        return
                    self._sendall(s, data)
                else:
                    self.errorOccurred.emit("TCP Server 需指定目标客户端")
                    return
            elif self._mode == "tcp_client":
                self._sendall(self._tcp_sock, data)
            else:                        # udp
                self._udp_sock.sendto(data, self._remote)
            self.dataWritten.emit(len(data))
        except Exception as e:          # noqa: BLE001
            self.errorOccurred.emit(f"发送失败：{e}")

    @pyqtSlot(str)
    def requestCloseClient(self, addr: str):
        """断开指定 TCP Server 客户端；空串 = 断开全部。"""
        if self._mode != "tcp_server":
            return
        addr = str(addr or "")
        if addr:
            sock = self._clients.get(addr)
            if sock is not None:
                self._drop_sock(sock)
        else:
            for sock in list(self._clients.values()):
                self._drop_sock(sock)

    @pyqtSlot(str)
    def setLogFile(self, path: str):
        """空串 = 关闭日志；文件 IO 只在 worker 线程进行。"""
        path = path or ""
        if path == self._logPath:
            return
        if self._logFp is not None:
            try:
                self._logFp.close()
            except OSError:
                pass
            self._logFp = None
        self._logPath = path
        if path:
            try:
                self._logFp = open(path, "ab")
            except OSError as e:
                self._logPath = ""
                self.errorOccurred.emit(f"无法打开日志文件：{e}")

    # ── MCP 只读查询（sigMcpQuery → mcpReply）─────────────────

    @pyqtSlot(dict)
    def requestMcpQuery(self, q: dict):
        op = str(q.get("op", "status"))
        rid = q.get("id")
        if op == "status":
            self.mcpReply.emit({"op": op, "id": rid, "data": {
                "running": self._running,
                "mode": self._mode,
                "local": self._local_str,
                "remote": self._remote_str,
                "clients": self.client_list()}})
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
            self._poll()
        self._stop_all()
        self.finished.emit()

    def _poll(self):
        if not self._running:
            return
        try:
            events = self._selector.select(0.05)
        except (OSError, ValueError):
            return
        for key, _mask in events:
            sock = key.fileobj
            if sock is self._listen_sock:
                self._accept_clients()
            elif sock is self._udp_sock:
                self._read_udp()
            else:
                self._read_sock(sock)

    # ── 内部 ────────────────────────────────────────────────────

    def client_list(self) -> list:
        return sorted(self._clients.keys())

    def _accept_clients(self):
        try:
            conn, addr = self._listen_sock.accept()
        except OSError:
            return
        conn.setblocking(False)
        src = format_source(addr[0], addr[1])
        self._clients[src] = conn
        self._sock_source[id(conn)] = src
        self._selector.register(conn, selectors.EVENT_READ)
        self.clientsChanged.emit(self.client_list())

    def _read_sock(self, sock):
        src = self._sock_source.get(id(sock), self._remote_str or "")
        try:
            data = sock.recv(_RECV_CHUNK)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if not data:
            self._drop_sock(sock)
            return
        self._rx_data(data, src)

    def _read_udp(self):
        try:
            data, addr = self._udp_sock.recvfrom(_RECV_CHUNK)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self.requestStop()
            self.errorOccurred.emit("UDP 通道异常关闭")
            return
        self._rx_data(data, format_source(addr[0], addr[1]))

    def _rx_data(self, data: bytes, src: str):
        if self._logFp is not None:
            try:
                self._logFp.write(data)
                self._logFp.flush()
            except OSError:
                pass
        self.dataReceived.emit(bytes(data), time.time(), src)

    def _sendall(self, sock, data: bytes):
        """带保护性超时的非阻塞 socket 全量发送，避免对端不读时卡死循环。"""
        sock.settimeout(_SEND_TIMEOUT)
        try:
            sock.sendall(data)
        finally:
            sock.setblocking(False)

    def _drop_sock(self, sock):
        try:
            self._selector.unregister(sock)
        except (KeyError, ValueError):
            pass
        src = self._sock_source.pop(id(sock), None)
        try:
            sock.close()
        except OSError:
            pass
        if src and src in self._clients:
            del self._clients[src]
            self.clientsChanged.emit(self.client_list())

    def _stop_all(self):
        for sock in list(self._clients.values()):
            try:
                self._selector.unregister(sock)
            except (KeyError, ValueError):
                pass
            try:
                sock.close()
            except OSError:
                pass
        self._clients.clear()
        self._sock_source.clear()
        for sock in (self._listen_sock, self._tcp_sock, self._udp_sock):
            if sock is None:
                continue
            try:
                self._selector.unregister(sock)
            except (KeyError, ValueError):
                pass
            try:
                sock.close()
            except OSError:
                pass
        self._listen_sock = None
        self._tcp_sock = None
        self._udp_sock = None
        if self._logFp is not None:
            try:
                self._logFp.close()
            except OSError:
                pass
            self._logFp = None
        self._logPath = ""


class _TcpipWorkerThread(QThread):

    def __init__(self, worker, parent=None):
        super().__init__(parent)
        self._worker = worker

    def run(self):
        self._worker.run()


class TcpipThread(QObject):
    """QThread 启停辅助，用法与 SerialThread / SshThread 一致。"""

    sigStart = pyqtSignal(dict)
    sigStop = pyqtSignal()
    sigSend = pyqtSignal(dict)
    sigCloseClient = pyqtSignal(str)
    sigSetLogFile = pyqtSignal(str)
    sigMcpQuery = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = TcpipWorker()
        self.thread = _TcpipWorkerThread(self.worker, self)
        self.worker.moveToThread(self.thread)

        queued = Qt.ConnectionType.QueuedConnection
        self.sigStart.connect(self.worker.requestStart, queued)
        self.sigStop.connect(self.worker.requestStop, queued)
        self.sigSend.connect(self.worker.requestSend, queued)
        self.sigCloseClient.connect(self.worker.requestCloseClient, queued)
        self.sigSetLogFile.connect(self.worker.setLogFile, queued)
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
