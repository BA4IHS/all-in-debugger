# coding: utf-8
"""纯函数单测 + loop:// 回环下的 SerialWorker 端到端测试（无需硬件）。"""
import sys
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import serial_utils as su


# ---------------------------------------------------------------------------
# parse_hex_input
# ---------------------------------------------------------------------------

def test_parse_hex_grouped():
    data, err = su.parse_hex_input("AA BB 0F")
    assert err == "" and data == b"\xaa\xbb\x0f"


def test_parse_hex_continuous():
    data, err = su.parse_hex_input("AABB0F")
    assert err == "" and data == b"\xaa\xbb\x0f"


def test_parse_hex_0x_and_comma():
    data, err = su.parse_hex_input("0xAA, 0xBB，0x0f")
    assert err == "" and data == b"\xaa\xbb\x0f"


def test_parse_hex_odd_length():
    data, err = su.parse_hex_input("AA B")
    assert data is None and "奇数" in err


def test_parse_hex_invalid_char():
    data, err = su.parse_hex_input("AA ZZ")
    assert data is None and "非十六进制" in err


def test_parse_hex_empty():
    data, err = su.parse_hex_input("   ")
    assert data is None and "为空" in err


def test_format_hex_roundtrip():
    data, _ = su.parse_hex_input("01 7F FF")
    assert su.format_hex(data) == "01 7F FF"


# ---------------------------------------------------------------------------
# 增量解码（跨块半个多字节字符）
# ---------------------------------------------------------------------------

def test_decode_chunk_utf8_split():
    dec = su.make_decoder("UTF-8")
    full = "中文".encode("utf-8")          # 6 字节
    part1, part2 = full[:4], full[4:]      # 在第二个汉字中间切断
    out = su.decode_chunk(dec, part1) + su.decode_chunk(dec, part2)
    assert out == "中文"


def test_decode_chunk_gbk_split():
    dec = su.make_decoder("GBK")
    full = "汉字".encode("gbk")            # 4 字节
    out = su.decode_chunk(dec, full[:1]) + su.decode_chunk(dec, full[1:])
    assert out == "汉字"


def test_decode_ascii_replaces_high_bytes():
    dec = su.make_decoder("ASCII")
    out = su.decode_chunk(dec, b"\xffA")
    assert "A" in out and len(out) == 2     # \xff 被替换而非丢弃/抛错


def test_decoder_reset():
    dec = su.make_decoder("UTF-8")
    full = "中".encode("utf-8")
    su.decode_chunk(dec, full[:1])          # 喂半个字符
    dec.reset()
    assert su.decode_chunk(dec, full) == "中"  # reset 后重新完整解码


# ---------------------------------------------------------------------------
# 换行符 / 格式化
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode,expect", [
    ("None", b"X"),
    ("CR", b"X\r"),
    ("LF", b"X\n"),
    ("CR+LF", b"X\r\n"),
])
def test_append_newline(mode, expect):
    assert su.append_newline(b"X", mode) == expect


def test_fmt_bytes():
    assert su.fmt_bytes(0) == "0 B"
    assert su.fmt_bytes(1023) == "1023 B"
    assert su.fmt_bytes(1536) == "1.50 KB"


def test_build_open_config():
    cfg = su.build_open_config("COM1", "9600", "8", "1", "None", "RTS/CTS", True, False)
    assert cfg["baudrate"] == 9600
    assert cfg["rtscts"] is True and cfg["xonxoff"] is False
    assert cfg["dtr"] is True and cfg["rts"] is False


def test_build_open_config_bad_baud():
    with pytest.raises(ValueError):
        su.build_open_config("COM1", "abc", "8", "1", "None", "None", True, True)


def test_format_port_label_strips_dup_com():
    assert su.format_port_label("COM13", "USB-SERIAL CH340 (COM13)") == "COM13 - USB-SERIAL CH340"


def test_format_port_label_plain():
    assert su.format_port_label("COM1", "Communications Port") == "COM1 - Communications Port"


def test_format_port_label_empty_desc():
    assert su.format_port_label("COM3", "") == "COM3"
    assert su.format_port_label("COM3", "COM3") == "COM3"


def test_list_serial_ports_filters_null_com_prefix(monkeypatch):
    ports = [
        SimpleNamespace(device="NULL_COM1", description="ELTIMA control"),
        SimpleNamespace(device="null_com10", description="ELTIMA control"),
        SimpleNamespace(device="NULL COM2", description="ELTIMA control"),
        SimpleNamespace(device="COM3", description="USB Serial"),
        SimpleNamespace(device="XNULL_COM4", description="Real device"),
    ]
    monkeypatch.setattr(su._list_ports, "comports", lambda: ports)

    assert su.list_serial_ports() == [
        ("COM3", "USB Serial"),
        ("XNULL_COM4", "Real device"),
    ]


# ---------------------------------------------------------------------------
# SerialWorker loop:// 回环端到端
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def qapp():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _wait_until(pred, timeout_ms=3000, app=None):
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.005)
    return False


def test_worker_loopback(qapp):
    from app.serial_worker import SerialThread

    st = SerialThread()
    opened, closed, errors, received = [], [], [], []
    written = []
    st.worker.portOpened.connect(opened.append)
    st.worker.portClosed.connect(closed.append)
    st.worker.errorOccurred.connect(errors.append)
    st.worker.dataWritten.connect(written.append)
    st.worker.dataReceived.connect(lambda d, ts: received.append(d))
    st.start()

    cfg = su.build_open_config("loop://", "9600", "8", "1", "None", "None", True, False)
    st.sigOpen.emit(cfg)
    assert _wait_until(lambda: opened, app=qapp), f"端口未打开: {errors}"

    st.sigWrite.emit(b"\xAA\xBB\x01")
    assert _wait_until(lambda: sum(map(len, received)) >= 3, app=qapp), \
        f"未收到回环数据: {errors}"
    assert b"".join(received) == b"\xAA\xBB\x01"
    assert written == [3]

    st.sigClose.emit()
    assert _wait_until(lambda: closed, app=qapp), "端口未关闭"

    st.stop()
    assert not st.isRunning


def test_worker_open_failed(qapp):
    """打开不存在的端口应发出 openFailed 而非崩溃。"""
    from app.serial_worker import SerialThread

    st = SerialThread()
    failed = []
    st.worker.openFailed.connect(failed.append)
    st.start()
    cfg = su.build_open_config("COM9999", "9600", "8", "1", "None", "None", True, True)
    st.sigOpen.emit(cfg)
    assert _wait_until(lambda: failed, app=qapp), "未收到 openFailed"
    st.stop()


def test_worker_log_file(qapp, tmp_path):
    """日志开关：worker 线程内写原始字节。"""
    from app.serial_worker import SerialThread

    log = tmp_path / "rx.bin"
    st = SerialThread()
    opened = []
    st.worker.portOpened.connect(opened.append)
    st.start()
    st.sigSetLogFile.emit(str(log))
    cfg = su.build_open_config("loop://", "9600", "8", "1", "None", "None", True, False)
    st.sigOpen.emit(cfg)
    assert _wait_until(lambda: opened, app=qapp)
    st.sigWrite.emit(b"LOG123")
    assert _wait_until(lambda: log.exists() and log.stat().st_size >= 6, app=qapp)
    st.sigClose.emit()
    st.stop()
    assert log.read_bytes()[:6] == b"LOG123"
