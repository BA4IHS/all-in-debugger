# coding: utf-8
"""新增调试模块的纯函数单测（无需 DLL/硬件）。"""
from app import dap_core
from app import dap_rtt


def test_parity():
    assert dap_core._parity(0) == 0
    assert dap_core._parity(1) == 1
    assert dap_core._parity(0b1011) == 1
    assert dap_core._parity(0xFFFFFFFF) == 0


def test_swd_request_bits():
    """对照 ARM 文档已知请求码：IDCODE读=0xA5、CTRL/STAT读=0x8D、SELECT写=0xB1。"""
    from app.dap_core import DapProbe, SwdTarget
    t = SwdTarget(DapProbe())
    assert t._swd_req(ap=False, read=True, addr=0x00) == 0xA5   # DP IDCODE 读
    assert t._swd_req(ap=False, read=True, addr=0x04) == 0x8D   # DP CTRL/STAT 读
    assert t._swd_req(ap=False, read=False, addr=0x08) == 0xB1  # DP SELECT 写


def test_rtt_signature():
    assert len(dap_rtt.RTT_SIGNATURE) == 16
    assert dap_rtt.RTT_SIGNATURE.startswith(b"SEGGER RTT")


def test_dll_absent_graceful():
    """DLL 不存在时不应抛异常，只返回 False/提示文本。"""
    from app import hid_binding, dap_core
    assert isinstance(hid_binding.load_info(), str)
    assert isinstance(dap_core.load_info(), str)
    if not hid_binding.available():
        import pytest
        from app.native import NativeError
        with pytest.raises(NativeError):
            hid_binding.enumerate_devices()


def test_bundled_adb_discovered():
    """程序自带 adb 三件套可被 find_adb 发现（未配置/PATH 无 adb 时）。"""
    from app.adb_runner import _bundled_adb, find_adb
    bundled = _bundled_adb()
    if bundled:  # 交付环境：应能解析到可执行文件
        path, err = find_adb("")
        assert path and not err


def test_modbus_fc_maps():
    from app.modbus_core import READ_METHODS, WRITE_METHODS
    assert set(READ_METHODS) == {1, 2, 3, 4}
    assert set(WRITE_METHODS) == {5, 6, 15, 16}
