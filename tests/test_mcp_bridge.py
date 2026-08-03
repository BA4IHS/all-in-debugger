# coding: utf-8
"""MCP 桥接层测试：跨线程查询/命令转发/超时/错误路径。"""
import threading
import time

import pytest
from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import QApplication

from app.mcp_bridge import BridgeError, WorkerBridge, parse_hex, to_hex
from app.modbus_core import ModbusThread
from app.serial_worker import SerialThread


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def serial_thread(app):
    st = SerialThread()
    st.start()
    yield st
    st.stop()


@pytest.fixture(scope="module")
def modbus_thread(app):
    mt = ModbusThread()
    mt.start()
    yield mt
    mt.stop()


@pytest.fixture(scope="module")
def bridge(serial_thread, modbus_thread):
    """ht/dt 用 None：本文件只测串口与 Modbus 通路。"""
    return WorkerBridge(serial_thread, None, None, modbus_thread)


# ── HEX 工具 ──────────────────────────────────────────────────

def test_parse_hex_variants():
    assert parse_hex("AA BB CC") == b"\xaa\xbb\xcc"
    assert parse_hex("aa,bb,cc") == b"\xaa\xbb\xcc"
    assert parse_hex("0xAA 0xBB") == b"\xaa\xbb"
    assert parse_hex("AABBCC") == b"\xaa\xbb\xcc"
    assert parse_hex("0") == b"\x00"


def test_parse_hex_invalid():
    with pytest.raises(BridgeError):
        parse_hex("GG")
    with pytest.raises(BridgeError):
        parse_hex("")
    with pytest.raises(BridgeError):
        parse_hex("1FF")  # 超过 0xFF


def test_to_hex():
    assert to_hex(b"\x01\xab") == "01 AB"
    assert to_hex(b"") == ""


# ── 只读查询（sigMcpQuery → mcpReply）────────────────────────

def test_serial_snapshot_query(bridge):
    snap = bridge.serial_status()
    assert snap["opened"] is False
    assert snap["port"] == ""


def test_serial_rx_query_empty(bridge):
    assert bridge.serial_read_recent(64) == b""


def test_query_unknown_op(bridge):
    with pytest.raises(BridgeError, match="未知查询"):
        bridge._query(bridge.st, {"op": "no-such-op"})


# ── 命令转发：错误路径 ────────────────────────────────────────

def test_serial_send_not_open(bridge):
    with pytest.raises(BridgeError, match="串口未打开"):
        bridge.serial_send(b"\x01")


def test_modbus_read_not_connected(bridge):
    with pytest.raises(BridgeError, match="未连接"):
        bridge.modbus_read(3, 0, 1)


def test_modbus_connect_no_event_loop_error(bridge):
    """连接不可达地址：应报连接失败而非 'no running event loop'。"""
    with pytest.raises(BridgeError) as ei:
        bridge.modbus_connect_tcp("127.0.0.1", 1)
    assert "no running event loop" not in str(ei.value)


class _FakeHidWorker(QObject):
    deviceOpened = pyqtSignal(dict)
    openFailed = pyqtSignal(str)
    mcpReply = pyqtSignal(dict)


class _FakeHidThread(QObject):
    sigOpen = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.worker = _FakeHidWorker()
        self.captured = []
        self.sigOpen.connect(self._on_open, Qt.ConnectionType.DirectConnection)

    def _on_open(self, cfg):
        self.captured.append(cfg)
        self.worker.deviceOpened.emit({
            "vid": cfg.get("vid", 0), "pid": cfg.get("pid", 0),
            "product": cfg.get("product", ""),
            "manufacturer": "", "serial": cfg.get("serial", "")})


def test_hid_open_by_index_passes_vidpid():
    """按索引打开应把枚举到的 vid/pid 传入并返回（回归 bug）。"""
    ht = _FakeHidThread()
    b = WorkerBridge(None, ht, None, None)
    b._hid_cache = [{"path": b"\\\\?\\fake", "vid": 0x3554,
                     "pid": 0xFA09, "product": "RX", "serial": ""}]
    info = b.hid_open(index=0)
    assert info["vid"] == 0x3554 and info["pid"] == 0xFA09
    assert ht.captured[0]["vid"] == 0x3554
    assert ht.captured[0]["path"] == b"\\\\?\\fake"


def test_modbus_status_disconnected(bridge):
    snap = bridge.modbus_status()
    assert snap["connected"] is False


# ── Modbus 读写闭环（本地 pymodbus TCP 从站）──────────────

def test_modbus_tcp_readwrite_roundtrip(bridge):
    """连接真实从站后 FC6 写/FC3 读回、FC5 写/FC1 读回。"""
    pymodbus = pytest.importorskip("pymodbus")
    import asyncio

    from pymodbus.datastore import (
        ModbusDeviceContext, ModbusSequentialDataBlock, ModbusServerContext)
    from pymodbus.server import StartAsyncTcpServer

    store = ModbusDeviceContext(
        hr=ModbusSequentialDataBlock(1, [0] * 100),
        co=ModbusSequentialDataBlock(1, [0] * 100))
    ctx = ModbusServerContext(devices=store, single=True)

    def _run():
        try:
            asyncio.run(StartAsyncTcpServer(
                context=ctx, address=("127.0.0.1", 15502)))
        except OSError:
            pass

    srv = threading.Thread(target=_run, daemon=True)
    srv.start()
    time.sleep(1.0)

    try:
        bridge.modbus_connect_tcp("127.0.0.1", 15502)
        bridge.modbus_write(6, 10, [1234])
        r = bridge.modbus_read(3, 10, 2)
        assert r["values"][0] == 1234
        bridge.modbus_write(5, 3, [1])
        r2 = bridge.modbus_read(1, 3, 2)
        assert r2["values"][0] in (True, 1)
    finally:
        bridge.modbus_disconnect()


# ── 超时路径 ──────────────────────────────────────────────────

class _Silent(QObject):
    never = pyqtSignal(str)


class _SilentWorker(QObject):
    """带 mcpReply 但无人处理 sigMcpQuery 的桩 worker。"""
    mcpReply = pyqtSignal(dict)


def test_emit_wait_timeout():
    w = _Silent()
    box = WorkerBridge(None, None, None, None)
    with pytest.raises(BridgeError, match="超时"):
        box._emit_wait([w.never], [], lambda: None, 0.2, "测试操作")


def test_query_timeout_no_worker():
    """无 worker 处理 sigMcpQuery 时 _query 应报超时。"""

    class _FakeThreadObj(QObject):
        sigMcpQuery = pyqtSignal(dict)

        def __init__(self):
            super().__init__()
            self.worker = _SilentWorker()

    fake = _FakeThreadObj()  # 信号无人处理 → 等待直至超时
    box = WorkerBridge(None, None, None, None)
    with pytest.raises(BridgeError, match="超时"):
        box._query(fake, {"op": "snapshot"}, timeout=0.3)


# ── debugger_status 聚合 ─────────────────────────────────────

def test_debugger_status_aggregates(bridge):
    status = bridge.debugger_status()
    assert set(status) == {"serial", "hid", "dap", "modbus", "ssh"}
    assert status["serial"]["opened"] is False
    assert status["modbus"]["connected"] is False
    # hid/dap/ssh 线程为 None → 应降级为 error 而非抛异常
    assert "error" in status["hid"]
    assert "error" in status["dap"]
    assert "error" in status["ssh"]


# ── 并发安全：多线程同时查询不串扰 ───────────────────────────

def test_concurrent_queries(bridge):
    results = []
    errors = []

    def run():
        try:
            results.append(bridge.serial_status())
        except BridgeError as e:
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not errors
    assert len(results) == 8
    assert all(r["opened"] is False for r in results)
