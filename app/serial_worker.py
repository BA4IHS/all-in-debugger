# coding: utf-8
"""串口工作线程：唯一持有 serial.Serial 的地方。

线程模型：QObject worker + moveToThread(QThread)，阻塞读短超时循环
（timeout=0.05，读 in_waiting 或阻塞读 1 字节）。UI 线程通过 queued slot
间接操作串口，严禁在 UI 线程直接触碰 serial.Serial。
"""
import logging
import threading
import time

import serial
from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal, pyqtSlot

from app.native import hexPreview

# MCP 只读查询用的 RX 环形缓冲上限（不影响 UI 接收链路）
RX_CAP = 65536
log = logging.getLogger(__name__)


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
            log.warning("串口打开失败：端口已处于打开状态")
            self.openFailed.emit("端口已处于打开状态")
            return
        cfg = dict(cfg)
        port = str(cfg.pop("port", ""))
        # dtr/rts 不是 pyserial 构造参数（传进 **kwargs 会抛
        # ValueError: unexpected keyword arguments），但它们必须在
        # “打开的那一刻”就生效，不能打开后再补设——补设会先以默认电平
        # 打开端口，对 DTR 接复位电路的板子（如 Arduino）等于多了一次
        # 电平跳变，可能把目标板复位掉。
        #
        # pyserial 的 dtr/rts 属性 setter 在**未打开**时只记录状态、
        # 不碰硬件，而 open() 内部的 _reconfigure_port() 会读这两个状态
        # 直接配置 DCB（DTR_CONTROL_ENABLE/DISABLE）。因此正确顺序是：
        # 先构造（不自动打开）→ 设属性 → open()，一次打开即完成配置。
        dtr = cfg.pop("dtr", None)
        rts = cfg.pop("rts", None)
        # 防御性兜底：write_timeout 缺省（pyserial 默认 None）意味着
        # ser.write() 可能无限阻塞，一次写入就能永久卡死整个 worker 线程
        # （串口随即“卡死”：不再收数据、不再响应任何操作）。任何调用路径
        # （UI/MCP/未来新增）漏设时都补上有限值。
        if cfg.get("write_timeout") is None:
            cfg["write_timeout"] = 1
        if cfg.get("timeout") is None:
            cfg["timeout"] = 0.05
        # 显式关闭 DSR/DTR 硬件流控：它是发送闸门，对端 DSR 不就绪会把
        # 写入卡到超时甚至永久阻塞。本工具的流控只经 xonxoff/rtscts。
        cfg["dsrdtr"] = False
        ser = None
        try:
            if "://" in port:
                # 带协议的 URL（如 loop://）：serial_for_url 支持 do_not_open，
                # 用它拿到未打开的实例，以便先设电平再 open
                ser = serial.serial_for_url(port, do_not_open=True, **cfg)
            else:
                # 实体串口：Serial.__init__ 不接受 do_not_open（会抛
                # ValueError），故先以 port=None 构造（不打开），
                # 设好电平后再补上 port 并 open()
                ser = serial.Serial(**cfg)
            # 打开前设电平：未打开时 setter 只记状态，open() 内部会依此
            # 配置 DCB（DTR_CONTROL_ENABLE/DISABLE），一次打开即完成
            if dtr is not None:
                ser.dtr = bool(dtr)
            if rts is not None:
                ser.rts = bool(rts)
            if "://" not in port:
                ser.port = port
            ser.open()
        except Exception as e:
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass
            message = str(e) or e.__class__.__name__
            log.warning("串口打开失败 %s: %s", port, message)
            self.openFailed.emit(message)
            return
        self._ser = ser
        self._portName = port
        self._rxBuf.clear()
        # 详细参数：波特率/数据位/校验/停止位/流控（排查现场接线与配置）
        try:
            log.info(
                "串口已打开：%s baudrate=%s bytesize=%s parity=%s stopbits=%s "
                "rtscts=%s xonxoff=%s dsrdtr=%s dtr=%s rts=%s",
                port, ser.baudrate, ser.bytesize, ser.parity, ser.stopbits,
                ser.rtscts, ser.xonxoff,
                getattr(ser, "dsrdtr", None),
                ser.dtr if dtr is None else bool(dtr),
                ser.rts if rts is None else bool(rts))
        except Exception as e:
            message = str(e) or e.__class__.__name__
            log.warning("串口打开后读取状态失败 %s: %s", port, message)
            self._closePort(notify=False)
            self.openFailed.emit(message)
            return
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
            # 不调用 ser.flush()：pyserial 的 flush() 会一直等到发送缓冲
            # 真正排空，且不受 write_timeout 约束——对端不接收/流控未放行
            # 时它同样能无限阻塞 worker 线程（与 write_timeout 缺失是同一
            # 类卡死）。write() 返回的字节数已足够确认发送结果，数据由
            # 驱动异步送出，无需在此同步等待。
            if log.isEnabledFor(logging.DEBUG):
                log.debug("串口 TX %s %d 字节：%s", self._portName,
                          len(data), hexPreview(data))
            self.dataWritten.emit(int(n) if n is not None else len(data))
        except Exception as e:
            log.warning("串口发送失败：%s", e)
            self.errorOccurred.emit(f"发送失败：{e}")

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
                log.warning("串口读取失败：%s", e)
                self.errorOccurred.emit(f"读取失败：{e}")
                self._closePort(notify=True)
                continue
            if data:
                self._rxBuf.extend(data)
                if len(self._rxBuf) > RX_CAP:
                    del self._rxBuf[:len(self._rxBuf) - RX_CAP]
                self._writeLog(data)
                if log.isEnabledFor(logging.DEBUG):
                    log.debug("串口 RX %s %d 字节：%s", self._portName,
                              len(data), hexPreview(data))
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
        if ser is not None:
            log.info("串口已关闭：%s", getattr(ser, "name", "?"))
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
