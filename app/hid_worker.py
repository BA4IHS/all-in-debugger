# coding: utf-8
"""HID 工作线程：唯一持有 hid_device 句柄的地方。

线程模型与 serial_worker 保持一致：QObject worker + moveToThread(QThread)，
打开后进入短超时读循环；UI 线程通过 queued slot 间接操作。
"""
import threading
import time

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal, pyqtSlot

from app import hid_binding
from app.native import NativeError

# MCP 只读查询用的 RX 环形缓冲上限
RX_CAP = 65536


class HidWorker(QObject):
    # ── worker → UI ────────────────────────────────────────────
    deviceOpened = pyqtSignal(dict)         # 设备信息
    openFailed = pyqtSignal(str)
    deviceClosed = pyqtSignal()
    dataReceived = pyqtSignal(bytes, float)  # 原始字节 + time.time()
    dataWritten = pyqtSignal(int)
    featureData = pyqtSignal(bytes)          # get feature report 结果
    errorOccurred = pyqtSignal(str)
    finished = pyqtSignal()
    mcpReply = pyqtSignal(dict)         # MCP 只读查询应答 {op, data|error}

    def __init__(self):
        super().__init__()
        self._dev = hid_binding.HidDevice()
        self._quit = threading.Event()
        self._pollMs = 50
        self._rxBuf = bytearray()
        self._info = {}
        self._readBroken = False   # 读失败后停止轮询但保持打开

    # ── UI → worker ────────────────────────────────────────────

    @pyqtSlot(dict)
    def requestOpen(self, cfg: dict):
        if self._dev.opened:
            self.openFailed.emit("HID 设备已打开")
            return
        cfg = dict(cfg or {})
        try:
            path = cfg.get("path")
            if path:
                self._dev.open_path(path if isinstance(path, bytes)
                                    else str(path).encode())
            else:
                self._dev.open(int(cfg.get("vid", 0)), int(cfg.get("pid", 0)),
                               str(cfg.get("serial", "")))
            info = self._dev.get_strings()
            try:
                rep_len = self._dev.report_lengths()
            except NativeError:
                rep_len = {}
        except (NativeError, OSError, ValueError) as e:
            self.openFailed.emit(str(e))
            return
        self._rxBuf.clear()
        self._readBroken = False
        self._info = {
            "vid": int(cfg.get("vid", 0)),
            "pid": int(cfg.get("pid", 0)),
            "product": cfg.get("product", "") or info.get("product", ""),
            "manufacturer": info.get("manufacturer", ""),
            "serial": info.get("serial", "") or str(cfg.get("serial", "")),
            "report_lengths": rep_len,
        }
        self.deviceOpened.emit(dict(self._info))

    @pyqtSlot()
    def requestClose(self):
        if self._dev.opened:
            self._dev.close()
            self._info = {}
            self.deviceClosed.emit()

    @pyqtSlot(bytes)
    def requestWrite(self, data: bytes):
        if not self._dev.opened:
            self.errorOccurred.emit("HID 设备未打开，无法发送")
            return
        try:
            n = self._dev.write(bytes(data))
            self.dataWritten.emit(int(n))
        except NativeError as e:
            self.errorOccurred.emit(f"HID 写入失败：{e}")

    @pyqtSlot(bytes)
    def requestFeatureSend(self, data: bytes):
        if not self._dev.opened:
            self.errorOccurred.emit("HID 设备未打开")
            return
        try:
            n = self._dev.send_feature_report(bytes(data))
            self.dataWritten.emit(int(n))
        except NativeError as e:
            self.errorOccurred.emit(f"发送特征报告失败：{e}")

    @pyqtSlot(int, int)
    def requestFeatureGet(self, report_id: int, size: int):
        if not self._dev.opened:
            self.errorOccurred.emit("HID 设备未打开")
            return
        try:
            data = self._dev.get_feature_report(int(report_id), int(size))
            self.featureData.emit(data)
        except NativeError as e:
            self.errorOccurred.emit(f"获取特征报告失败：{e}")

    @pyqtSlot()
    def requestQuit(self):
        self._quit.set()

    # ── MCP 只读查询（sigMcpQuery → mcpReply）─────────────────

    @pyqtSlot(dict)
    def requestMcpQuery(self, q: dict):
        """q: {op:'snapshot'} 或 {op:'rx', n}；只读，供 MCP 桥查询。"""
        op = str(q.get("op", "snapshot"))
        rid = q.get("id")
        if op == "snapshot":
            self.mcpReply.emit({"op": op, "id": rid,
                                "data": self._snapshot()})
        elif op == "rx":
            self.mcpReply.emit({"op": op, "id": rid,
                                "data": self._recentRx(int(q.get("n", 0)))})
        else:
            self.mcpReply.emit({"op": op, "id": rid,
                                "error": f"未知查询 {op}"})

    def _snapshot(self):
        """设备状态。"""
        return {"opened": self._dev.opened, "info": dict(self._info),
                "rx_buffered": len(self._rxBuf)}

    def _recentRx(self, n: int) -> bytes:
        """最近 n 字节接收数据（n<=0 返回全部缓冲）。"""
        if n <= 0:
            return bytes(self._rxBuf)
        return bytes(self._rxBuf[-n:])

    # ── 读循环（worker 线程）───────────────────────────────────

    def run(self):
        # 与 SerialWorker 一致：queued slot 由 processEvents 在两次
        # 短超时读之间分发，保证 hid_device 只被本线程访问。
        from PyQt6.QtCore import QCoreApplication

        while not self._quit.is_set():
            QCoreApplication.processEvents()
            if self._quit.is_set():
                break
            if self._dev.opened:
                if self._readBroken:
                    # 设备不支持/拒绝中断读（如被独占的键鼠接收器）：
                    # 保持打开，写与特征报告仍可用
                    time.sleep(0.05)
                    continue
                try:
                    data = self._dev.read(512, self._pollMs)
                except NativeError as e:
                    self._readBroken = True
                    self.errorOccurred.emit(
                        f"HID 读取不可用（{e}），设备保持打开，"
                        "写入/特征报告仍可尝试")
                    continue
                if data:
                    self._rxBuf.extend(data)
                    if len(self._rxBuf) > RX_CAP:
                        del self._rxBuf[:len(self._rxBuf) - RX_CAP]
                    self.dataReceived.emit(data, time.time())
            else:
                time.sleep(0.02)
        if self._dev.opened:
            self._dev.close()
        self.finished.emit()

    @property
    def opened(self) -> bool:
        return self._dev.opened


class _HidWorkerThread(QThread):
    """直接执行 worker 读循环（不进 Qt 事件循环）。"""

    def __init__(self, worker, parent=None):
        super().__init__(parent)
        self._worker = worker

    def run(self):
        self._worker.run()


class HidThread(QObject):
    """QThread 启停辅助：持有 worker，负责 moveToThread 与优雅停机。

    UI 侧通过 sig* 信号（queued）间接操作 worker，与 SerialThread 用法一致。
    """

    # UI → worker（queued connection）
    sigOpen = pyqtSignal(dict)          # {path} 或 {vid,pid,serial}
    sigClose = pyqtSignal()
    sigWrite = pyqtSignal(bytes)
    sigFeatureSend = pyqtSignal(bytes)
    sigFeatureGet = pyqtSignal(int, int)  # report_id, size
    sigMcpQuery = pyqtSignal(dict)      # MCP 只读查询请求

    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = HidWorker()
        self.thread = _HidWorkerThread(self.worker, self)
        self.worker.moveToThread(self.thread)

        queued = Qt.ConnectionType.QueuedConnection
        self.sigOpen.connect(self.worker.requestOpen, queued)
        self.sigClose.connect(self.worker.requestClose, queued)
        self.sigWrite.connect(self.worker.requestWrite, queued)
        self.sigFeatureSend.connect(self.worker.requestFeatureSend, queued)
        self.sigFeatureGet.connect(self.worker.requestFeatureGet, queued)
        self.sigMcpQuery.connect(self.worker.requestMcpQuery, queued)

    def start(self):
        self.thread.start()

    def stop(self, timeout_ms: int = 1500):
        self.worker.requestQuit()  # threading.Event.set，线程安全
        if not self.thread.wait(timeout_ms):
            self.thread.wait(500)

    @property
    def isRunning(self) -> bool:
        return self.thread.isRunning()
