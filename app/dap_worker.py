# coding: utf-8
"""DAP-link RTT 工作线程：唯一持有 DapProbe 的地方。

流程：打开调试器 → SWD 连接 → （可选复位）→ 定位/解析 RTT 控制块 →
周期轮询 UP 通道上行数据；下行通过 queued slot 写入 DOWN 通道。
"""
import threading
import time

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal, pyqtSlot

from app import dap_core, dap_rtt
from app.dap_core import DapError

# MCP 只读查询用的每通道 RX 环形缓冲上限
RX_CAP = 65536


class DapWorker(QObject):
    # ── worker → UI ────────────────────────────────────────────
    probeOpened = pyqtSignal(str)
    openFailed = pyqtSignal(str)
    probeClosed = pyqtSignal()
    connected = pyqtSignal(dict)        # {idcode, channels...}
    rttFound = pyqtSignal(dict)         # 控制块解析结果
    dataReceived = pyqtSignal(str, bytes, float)  # 通道名, 数据, ts
    dataWritten = pyqtSignal(str, int)
    errorOccurred = pyqtSignal(str)
    finished = pyqtSignal()
    mcpReply = pyqtSignal(dict)         # MCP 只读查询应答 {op, data|error}

    def __init__(self):
        super().__init__()
        self._probe = dap_core.DapProbe()
        self._target = None
        self._rtt = None
        self._rttActive = False
        self._pollMs = 50
        self._quit = threading.Event()
        self._rxBufs = {}   # 通道名 → bytearray

    # ── UI → worker ────────────────────────────────────────────

    @pyqtSlot(dict)
    def requestOpen(self, cfg: dict):
        """cfg: {path, clock, ram_start, ram_size, cb_addr, reset}"""
        if self._probe.opened:
            self.openFailed.emit("调试器已打开")
            return
        cfg = dict(cfg or {})
        try:
            self._probe.open(cfg.get("path") or b"")
            port = self._probe.connect(dap_core.DAP_PORT_SWD)
            clock = int(cfg.get("clock") or 1_000_000)
            self._probe.set_clock(clock)
            self._probe.transfer_configure()
            target = dap_core.SwdTarget(self._probe)
            idcode = target.read_idcode()
            if cfg.get("reset"):
                try:
                    self._probe.reset_target()
                    time.sleep(0.1)
                    idcode = target.read_idcode()
                except DapError:
                    pass  # 无 RESET 线时忽略
            self._target = target
        except DapError as e:
            self._cleanup_probe()
            self.openFailed.emit(str(e))
            return
        self.probeOpened.emit(f"IDCODE={idcode:#010x}")
        # RTT 控制块：指定地址优先，否则自动扫描
        try:
            cb_addr = int(cfg.get("cb_addr") or 0)
            if not cb_addr:
                regions = None
                ram_start = int(cfg.get("ram_start") or 0)
                ram_size = int(cfg.get("ram_size") or 0)
                if ram_start and ram_size:
                    regions = [(ram_start, ram_start + ram_size)]
                cb_addr = dap_rtt.find_control_block(target, regions)
            if not cb_addr:
                self.errorOccurred.emit(
                    "未找到 RTT 控制块：确认固件已初始化 SEGGER RTT，"
                    "或手动指定 RAM 区间/控制块地址")
                return
            rtt = dap_rtt.parse_control_block(target, cb_addr)
        except DapError as e:
            self.errorOccurred.emit(f"RTT 控制块解析失败：{e}")
            return
        self._rtt = rtt
        self._rxBufs.clear()
        self.rttFound.emit(rtt)

    @pyqtSlot()
    def requestStartRtt(self):
        if self._rtt is not None:
            self._rttActive = True

    @pyqtSlot()
    def requestStopRtt(self):
        self._rttActive = False

    @pyqtSlot(str, bytes)
    def requestWrite(self, channel_name: str, data: bytes):
        if self._rtt is None:
            self.errorOccurred.emit("RTT 未就绪")
            return
        ch = next((c for c in self._rtt["channels"]
                   if c["direction"] == "DOWN" and c["name"] == channel_name),
                  None)
        if ch is None:
            self.errorOccurred.emit(f"DOWN 通道 {channel_name} 不存在")
            return
        try:
            n = dap_rtt.write_channel(self._target, ch, bytes(data))
        except DapError as e:
            self.errorOccurred.emit(f"RTT 写入失败：{e}")
            return
        self.dataWritten.emit(channel_name, n)

    @pyqtSlot()
    def requestClose(self):
        self._rttActive = False
        self._rtt = None
        self._target = None
        self._cleanup_probe()
        self.probeClosed.emit()

    @pyqtSlot()
    def requestQuit(self):
        self._quit.set()

    # ── MCP 只读查询（sigMcpQuery → mcpReply）─────────────────

    @pyqtSlot(dict)
    def requestMcpQuery(self, q: dict):
        """q: {op:'snapshot'} 或 {op:'rx', channel, n}；只读。"""
        op = str(q.get("op", "snapshot"))
        rid = q.get("id")
        if op == "snapshot":
            self.mcpReply.emit({"op": op, "id": rid,
                                "data": self._snapshot()})
        elif op == "rx":
            self.mcpReply.emit({
                "op": op, "id": rid,
                "data": self._recentRx(str(q.get("channel", "")),
                                       int(q.get("n", 0)))})
        else:
            self.mcpReply.emit({"op": op, "id": rid,
                                "error": f"未知查询 {op}"})

    def _snapshot(self):
        """调试器/RTT 状态。"""
        info = {"opened": self._probe.opened,
                "rtt_active": self._rttActive,
                "channels": []}
        if self._rtt:
            info["cb_addr"] = self._rtt.get("addr", 0)
            info["channels"] = [
                {"name": c["name"], "direction": c["direction"]}
                for c in self._rtt["channels"]]
        return info

    def _recentRx(self, channel_name: str, n: int) -> bytes:
        """指定 UP 通道最近 n 字节（n<=0 返回全部缓冲）。"""
        buf = self._rxBufs.get(channel_name, bytearray())
        if n <= 0:
            return bytes(buf)
        return bytes(buf[-n:])

    def _cleanup_probe(self):
        try:
            if self._probe.opened:
                self._probe.close()
        except Exception:
            pass

    # ── 轮询循环（worker 线程）────────────────────────────────

    def run(self):
        from PyQt6.QtCore import QCoreApplication

        while not self._quit.is_set():
            QCoreApplication.processEvents()
            if self._quit.is_set():
                break
            if self._rttActive and self._rtt is not None:
                self._poll_once()
            else:
                time.sleep(0.02)
        self._cleanup_probe()
        self.finished.emit()

    def _poll_once(self):
        try:
            for ch in self._rtt["channels"]:
                if ch["direction"] != "UP":
                    continue
                data = dap_rtt.read_channel(self._target, ch)
                if data:
                    buf = self._rxBufs.setdefault(ch["name"], bytearray())
                    buf.extend(data)
                    if len(buf) > RX_CAP:
                        del buf[:len(buf) - RX_CAP]
                    self.dataReceived.emit(ch["name"], data, time.time())
        except DapError as e:
            self._rttActive = False
            self.errorOccurred.emit(f"RTT 轮询失败（目标断开？）：{e}")


class _DapWorkerThread(QThread):

    def __init__(self, worker, parent=None):
        super().__init__(parent)
        self._worker = worker

    def run(self):
        self._worker.run()


class DapThread(QObject):
    """QThread 启停辅助，用法与 SerialThread / HidThread 一致。"""

    sigOpen = pyqtSignal(dict)
    sigClose = pyqtSignal()
    sigStartRtt = pyqtSignal()
    sigStopRtt = pyqtSignal()
    sigWrite = pyqtSignal(str, bytes)
    sigMcpQuery = pyqtSignal(dict)      # MCP 只读查询请求

    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = DapWorker()
        self.thread = _DapWorkerThread(self.worker, self)
        self.worker.moveToThread(self.thread)

        queued = Qt.ConnectionType.QueuedConnection
        self.sigOpen.connect(self.worker.requestOpen, queued)
        self.sigClose.connect(self.worker.requestClose, queued)
        self.sigStartRtt.connect(self.worker.requestStartRtt, queued)
        self.sigStopRtt.connect(self.worker.requestStopRtt, queued)
        self.sigWrite.connect(self.worker.requestWrite, queued)
        self.sigMcpQuery.connect(self.worker.requestMcpQuery, queued)

    def start(self):
        self.thread.start()

    def stop(self, timeout_ms: int = 1500):
        self.worker.requestQuit()
        if not self.thread.wait(timeout_ms):
            self.thread.wait(500)

    @property
    def isRunning(self) -> bool:
        return self.thread.isRunning()
