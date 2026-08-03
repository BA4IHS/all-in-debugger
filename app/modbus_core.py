# coding: utf-8
"""Modbus 客户端封装（pymodbus 3.x async）。

ModbusWorker 在独立 QThread 中运行 asyncio 事件循环，
UI 通过 queued slot 提交读写任务；避免 pymodbus 同步 API 的版本差异。
"""
import asyncio
import inspect
import logging
import threading
import time

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal, pyqtSlot

# 压低 pymodbus 内部重连刷屏日志（"Failed to connect / Repeating...."）
logging.getLogger("pymodbus").setLevel(logging.ERROR)

try:
    from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient
    _HAS_PYMODBUS = True
except ImportError:
    _HAS_PYMODBUS = False

# 功能码 → 读/写方法映射（pymodbus 客户端方法名）
READ_METHODS = {
    1: "read_coils",
    2: "read_discrete_inputs",
    3: "read_holding_registers",
    4: "read_input_registers",
}
WRITE_METHODS = {
    5: "write_coil",
    6: "write_register",
    15: "write_coils",
    16: "write_registers",
}


def pymodbus_info() -> str:
    if _HAS_PYMODBUS:
        try:
            import pymodbus
            return f"pymodbus {getattr(pymodbus, '__version__', '')}".strip()
        except Exception:
            return "pymodbus 已安装"
    return "未安装 pymodbus（pip install pymodbus）"


class ModbusWorker(QObject):
    # ── worker → UI ────────────────────────────────────────────
    connected = pyqtSignal(str)
    connectFailed = pyqtSignal(str)
    closed = pyqtSignal()
    readResult = pyqtSignal(dict)      # {fc, addr, values, ts, ms}
    writeResult = pyqtSignal(dict)     # {fc, addr, count, ts}
    errorOccurred = pyqtSignal(str)
    finished = pyqtSignal()
    mcpReply = pyqtSignal(dict)         # MCP 只读查询应答 {op, data|error}

    # pymodbus 读写方法的从站地址参数名随版本变动：
    # 旧 3.x 用 unit=，中期用 slave=，3.14+ 用 device_id=
    _SLAVE_KWARG = next(
        (k for k in ("device_id", "slave", "unit")
         if k in inspect.signature(
             AsyncModbusTcpClient.read_holding_registers).parameters),
        "slave") if _HAS_PYMODBUS else "slave"

    def __init__(self):
        super().__init__()
        self._loop = None
        self._client = None
        self._connected = False
        self._quit = threading.Event()
        self._transport = ""
        self._endpoint = ""
        self._lastRead = None

    # ── UI → worker ────────────────────────────────────────────

    @pyqtSlot(dict)
    def requestConnect(self, cfg: dict):
        if not _HAS_PYMODBUS:
            self.connectFailed.emit(pymodbus_info())
            return
        if self._connected:
            self.connectFailed.emit("已连接")
            return
        cfg = dict(cfg or {})
        transport = cfg.get("transport", "tcp")
        t0 = time.time()

        async def _do():
            # pymodbus 3.x 要求客户端在运行中的事件循环内构造
            if transport == "rtu":
                client = AsyncModbusSerialClient(
                    port=cfg.get("port", ""),
                    baudrate=int(cfg.get("baudrate", 9600)),
                    bytesize=int(cfg.get("bytesize", 8)),
                    parity=cfg.get("parity", "N"),
                    stopbits=float(cfg.get("stopbits", 1)),
                    timeout=float(cfg.get("timeout", 1.0)),
                )
            else:
                client = AsyncModbusTcpClient(
                    host=cfg.get("host", "127.0.0.1"),
                    port=int(cfg.get("tcp_port", 502)),
                    timeout=float(cfg.get("timeout", 1.0)),
                )
            ok = await client.connect()
            return client, ok

        try:
            client, ok = self._loop.run_until_complete(_do())
        except Exception as e:
            self.connectFailed.emit(f"连接异常：{e}")
            return
        if not ok:
            try:
                client.close()
            except Exception:
                pass
            self.connectFailed.emit("连接失败（检查地址/串口参数）")
            return
        self._client = client
        self._connected = True
        self._transport = transport
        self._endpoint = (cfg.get("port", "") if transport == "rtu"
                          else f"{cfg.get('host', '127.0.0.1')}:"
                               f"{int(cfg.get('tcp_port', 502))}")
        ms = int((time.time() - t0) * 1000)
        self.connected.emit(f"{transport.upper()} 连接成功（{ms} ms）")

    @pyqtSlot()
    def requestClose(self):
        try:
            if self._client is not None:
                self._client.close()
        except Exception:
            pass
        self._client = None
        if self._connected:
            self._connected = False
            self.closed.emit()

    @pyqtSlot(dict)
    def requestRead(self, req: dict):
        """req: {fc, addr, count, slave}"""
        if not self._connected or self._client is None:
            self.errorOccurred.emit("Modbus 未连接")
            return
        fc = int(req.get("fc", 3))
        addr = int(req.get("addr", 0))
        count = max(1, int(req.get("count", 1)))
        slave = int(req.get("slave", 1))
        method = READ_METHODS.get(fc)
        if method is None:
            self.errorOccurred.emit(f"不支持的读功能码 {fc}")
            return
        t0 = time.time()
        try:
            rsp = self._loop.run_until_complete(
                getattr(self._client, method)(
                    address=addr, count=count,
                    **{self._SLAVE_KWARG: slave}))
        except Exception as e:
            self.errorOccurred.emit(f"读取异常：{e}")
            return
        if rsp.isError():
            self.errorOccurred.emit(f"FC{fc} 读错误：{rsp}")
            return
        values = list(rsp.bits[:count]) if fc in (1, 2) else list(rsp.registers)
        result = {
            "fc": fc, "addr": addr, "values": values,
            "ts": time.time(), "ms": int((time.time() - t0) * 1000),
        }
        self._lastRead = result
        self.readResult.emit(result)

    @pyqtSlot(dict)
    def requestWrite(self, req: dict):
        """req: {fc, addr, values: list, slave}"""
        if not self._connected or self._client is None:
            self.errorOccurred.emit("Modbus 未连接")
            return
        fc = int(req.get("fc", 6))
        addr = int(req.get("addr", 0))
        values = list(req.get("values", []))
        slave = int(req.get("slave", 1))
        method = WRITE_METHODS.get(fc)
        if method is None or not values:
            self.errorOccurred.emit(f"写请求无效（FC{fc}）")
            return
        try:
            skw = {self._SLAVE_KWARG: slave}
            if fc == 5:
                coro = self._client.write_coil(
                    address=addr, value=bool(values[0]), **skw)
            elif fc == 6:
                coro = self._client.write_register(
                    address=addr, value=int(values[0]) & 0xFFFF, **skw)
            elif fc == 15:
                coro = self._client.write_coils(
                    address=addr, values=[bool(v) for v in values], **skw)
            else:
                coro = self._client.write_registers(
                    address=addr, values=[int(v) & 0xFFFF for v in values],
                    **skw)
            rsp = self._loop.run_until_complete(coro)
        except Exception as e:
            self.errorOccurred.emit(f"写入异常：{e}")
            return
        if rsp.isError():
            self.errorOccurred.emit(f"FC{fc} 写错误：{rsp}")
            return
        self.writeResult.emit({
            "fc": fc, "addr": addr, "count": len(values), "ts": time.time(),
        })

    @pyqtSlot()
    def requestQuit(self):
        self._quit.set()

    # ── MCP 只读查询（sigMcpQuery → mcpReply）─────────────────

    @pyqtSlot(dict)
    def requestMcpQuery(self, q: dict):
        """q: {op:'snapshot'}；只读，供 MCP 桥查询。"""
        op = str(q.get("op", "snapshot"))
        rid = q.get("id")
        if op == "snapshot":
            self.mcpReply.emit({"op": op, "id": rid, "data": {
                "connected": self._connected,
                "transport": self._transport,
                "endpoint": self._endpoint,
                "last_read": self._lastRead,
            }})
        else:
            self.mcpReply.emit({"op": op, "id": rid,
                                "error": f"未知查询 {op}"})

    # ── 事件循环（worker 线程）────────────────────────────────

    def run(self):
        from PyQt6.QtCore import QCoreApplication

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        while not self._quit.is_set():
            QCoreApplication.processEvents()
            if self._quit.is_set():
                break
            time.sleep(0.02)
        # 收尾
        try:
            if self._client is not None:
                self._client.close()
        except Exception:
            pass
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        self._loop.run_until_complete(asyncio.gather(*pending,
                                                     return_exceptions=True))
        self._loop.close()
        self.finished.emit()


class _ModbusWorkerThread(QThread):

    def __init__(self, worker, parent=None):
        super().__init__(parent)
        self._worker = worker

    def run(self):
        self._worker.run()


class ModbusThread(QObject):
    """QThread 启停辅助，用法与 SerialThread 等一致。"""

    sigConnect = pyqtSignal(dict)
    sigClose = pyqtSignal()
    sigRead = pyqtSignal(dict)
    sigWrite = pyqtSignal(dict)
    sigMcpQuery = pyqtSignal(dict)      # MCP 只读查询请求

    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = ModbusWorker()
        self.thread = _ModbusWorkerThread(self.worker, self)
        self.worker.moveToThread(self.thread)

        queued = Qt.ConnectionType.QueuedConnection
        self.sigConnect.connect(self.worker.requestConnect, queued)
        self.sigClose.connect(self.worker.requestClose, queued)
        self.sigRead.connect(self.worker.requestRead, queued)
        self.sigWrite.connect(self.worker.requestWrite, queued)
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
