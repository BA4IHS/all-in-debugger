# coding: utf-8
"""串口工作线程：唯一持有 serial.Serial 的地方。

线程模型：QObject worker + moveToThread(QThread)，阻塞读短超时循环
（timeout=0.05，读 in_waiting 或阻塞读 1 字节）。UI 线程通过 queued slot
间接操作串口，严禁在 UI 线程直接触碰 serial.Serial。
"""
import threading
import time

import serial
from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal, pyqtSlot

# MCP 只读查询用的 RX 环形缓冲上限（不影响 UI 接收链路）
RX_CAP = 65536


class SerialWorker(QObject):
    # ── worker → UI（queued）────────────────────────────────────
    portOpened = pyqtSignal(str)        # 成功打开的端口名
    openFailed = pyqtSignal(str)        # 打开失败原因
    portClosed = pyqtSignal(str)        # 端口关闭（正常/异常统一出口）
    dataReceived = pyqtSignal(bytes, float)  # 原始字节 + time.time()
    dataWritten = pyqtSignal(int)       # 成功写入的字节数
    errorOccurred = pyqtSignal(str)     # 运行期异常
    finished = pyqtSignal()             # run() 退出，通知线程收尾
    mcpReply = pyqtSignal(dict)         # MCP 只读查询应答 {op, data|error}

    def __init__(self):
        super().__init__()
        self._ser = None
        self._portName = ""
        self._quit = threading.Event()
        self._logFp = None
        self._logPath = ""
        self._rxBuf = bytearray()

    # ── UI → worker（全部在 worker 线程执行）──────────────────────

    @pyqtSlot(dict)
    def requestOpen(self, cfg: dict):
        if self._ser is not None:
            self.openFailed.emit("端口已处于打开状态")
            return
        cfg = dict(cfg)
        port = str(cfg.pop("port", ""))
        # dtr/rts 不是构造函数参数（pyserial 3.5），只能打开后经属性设置
        dtr = cfg.pop("dtr", None)
        rts = cfg.pop("rts", None)
        try:
            if "://" in port:
                ser = serial.serial_for_url(port, **cfg)
            else:
                ser = serial.Serial(port=port, **cfg)
            if dtr is not None:
                ser.dtr = bool(dtr)
            if rts is not None:
                ser.rts = bool(rts)
        except Exception as e:
            self.openFailed.emit(str(e))
            return
        self._ser = ser
        self._portName = port
        self._rxBuf.clear()
        self.portOpened.emit(port)

    @pyqtSlot()
    def requestClose(self):
        self._closePort(notify=True)

    @pyqtSlot(bytes)
    def requestWrite(self, data: bytes):
        ser = self._ser
        if ser is None or not ser.is_open:
            self.errorOccurred.emit("串口未打开，无法发送")
            return
        try:
            n = ser.write(bytes(data))
            ser.flush()
            self.dataWritten.emit(int(n) if n is not None else len(data))
        except Exception as e:
            self.errorOccurred.emit(f"发送失败：{e}")

    @pyqtSlot(bool)
    def setDTR(self, on: bool):
        try:
            if self._ser is not None:
                self._ser.dtr = bool(on)
        except Exception as e:
            self.errorOccurred.emit(f"设置 DTR 失败：{e}")

    @pyqtSlot(bool)
    def setRTS(self, on: bool):
        try:
            if self._ser is not None:
                self._ser.rts = bool(on)
        except Exception as e:
            self.errorOccurred.emit(f"设置 RTS 失败：{e}")

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

    @pyqtSlot()
    def requestQuit(self):
        """请求 run() 退出（关闭窗口时调用）。"""
        self._quit.set()

    # ── MCP 只读查询（sigMcpQuery → mcpReply）─────────────────

    @pyqtSlot(dict)
    def requestMcpQuery(self, q: dict):
        """q: {op: 'snapshot'} 或 {op:'rx', n}；只读，供 MCP 桥查询。"""
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
        """当前端口状态与参数。"""
        ser = self._ser
        opened = ser is not None and ser.is_open
        info = {"opened": opened, "port": self._portName,
                "rx_buffered": len(self._rxBuf)}
        if opened:
            info.update({"baudrate": ser.baudrate, "bytesize": ser.bytesize,
                         "parity": ser.parity, "stopbits": ser.stopbits})
        return info

    def _recentRx(self, n: int) -> bytes:
        """最近 n 字节接收数据（n<=0 返回全部缓冲）。"""
        if n <= 0:
            return bytes(self._rxBuf)
        return bytes(self._rxBuf[-n:])

    # ── 读循环（worker 线程）────────────────────────────────────

    def run(self):
        # 本循环即 worker 线程的事件分发点：queued slot（open/close/write…）
        # 投递到本线程事件队列后，由 processEvents 在两次 read 之间执行，
        # 保证 serial.Serial 始终只被本线程单线程访问。
        from PyQt6.QtCore import QCoreApplication

        while not self._quit.is_set():
            QCoreApplication.processEvents()
            ser = self._ser
            if ser is None:
                # 等待打开请求；短睡眠避免空转
                time.sleep(0.02)
                continue
            try:
                n = ser.in_waiting
                data = ser.read(n) if n else ser.read(1)
            except Exception as e:
                self.errorOccurred.emit(f"读取失败：{e}")
                self._closePort(notify=True)
                continue
            if data:
                self._rxBuf.extend(data)
                if len(self._rxBuf) > RX_CAP:
                    del self._rxBuf[:len(self._rxBuf) - RX_CAP]
                self._writeLog(data)
                self.dataReceived.emit(bytes(data), time.time())
        # 收尾：确保端口与日志关闭
        self._closePort(notify=True)
        if self._logFp is not None:
            try:
                self._logFp.close()
            except OSError:
                pass
            self._logFp = None
        self.finished.emit()

    # ── 内部 ────────────────────────────────────────────────────

    def _closePort(self, notify: bool):
        ser, self._ser = self._ser, None
        if ser is None:
            return
        name, self._portName = self._portName, ""
        try:
            ser.close()
        except Exception:
            pass
        if notify:
            self.portClosed.emit(name)

    def _writeLog(self, data: bytes):
        fp = self._logFp
        if fp is None:
            return
        try:
            fp.write(data)
            fp.flush()
        except OSError:
            try:
                fp.close()
            except OSError:
                pass
            self._logFp = None
            self._logPath = ""


class _WorkerThread(QThread):
    """直接执行 worker 读循环（不进 Qt 事件循环）。

    queued slot 由 worker.run() 内的 processEvents() 分发，
    因此不需要事件循环；worker.run() 返回即线程自然结束，wait() 必然成功。
    """

    def __init__(self, worker, parent=None):
        super().__init__(parent)
        self._worker = worker

    def run(self):
        self._worker.run()


class SerialThread(QObject):
    """QThread 启停辅助：持有 worker，负责 moveToThread 与优雅停机。

    UI 侧必须通过这些 sig* 信号（queued）间接操作 worker，
    不能直接调用 worker.requestXxx —— 那会在调用者线程同步执行。
    唯一例外是 requestQuit()：只 set 一个线程安全的 Event，可直接调用。
    """

    # UI → worker（queued connection）
    sigOpen = pyqtSignal(dict)
    sigClose = pyqtSignal()
    sigWrite = pyqtSignal(bytes)
    sigSetDTR = pyqtSignal(bool)
    sigSetRTS = pyqtSignal(bool)
    sigSetLogFile = pyqtSignal(str)
    sigMcpQuery = pyqtSignal(dict)      # MCP 只读查询请求

    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = SerialWorker()
        self.thread = _WorkerThread(self.worker, self)
        self.worker.moveToThread(self.thread)

        queued = Qt.ConnectionType.QueuedConnection
        self.sigOpen.connect(self.worker.requestOpen, queued)
        self.sigClose.connect(self.worker.requestClose, queued)
        self.sigWrite.connect(self.worker.requestWrite, queued)
        self.sigSetDTR.connect(self.worker.setDTR, queued)
        self.sigSetRTS.connect(self.worker.setRTS, queued)
        self.sigSetLogFile.connect(self.worker.setLogFile, queued)
        self.sigMcpQuery.connect(self.worker.requestMcpQuery, queued)

    def start(self):
        self.thread.start()

    def stop(self, timeout_ms: int = 800):
        self.worker.requestQuit()  # threading.Event.set，线程安全，可直接调用
        if not self.thread.wait(timeout_ms):
            # 兜底：正常不会走到这里（read 超时 50ms，退出延迟可控）
            self.thread.wait(500)

    @property
    def isRunning(self) -> bool:
        return self.thread.isRunning()
