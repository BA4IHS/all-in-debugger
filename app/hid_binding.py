# coding: utf-8
"""hidapi.dll 的 ctypes 绑定（HID 调试核心）。

对外提供：
- available() / load_info()：DLL 是否可用
- enumerate_devices(vid=0, pid=0)：枚举设备
- HidDevice：打开/关闭/读/写/特征报告/描述字符串

线程安全约定：同一个 HidDevice 只应在一个线程内使用（worker 线程）。
"""
import ctypes
from ctypes import (
    POINTER, Structure, byref, c_char_p, c_int, c_size_t, c_ubyte,
    c_ushort, c_void_p, c_wchar_p,
)
from typing import List, Optional

from app import native

_DLL_NAMES = ("hidapi.dll", "hidapi-hidapi.dll", "libhidapi.dll")


class _HidDeviceInfo(Structure):
    pass


_HidDeviceInfo._fields_ = [
    ("path", c_char_p),
    ("vendor_id", c_ushort),
    ("product_id", c_ushort),
    ("serial_number", c_wchar_p),
    ("release_number", c_ushort),
    ("manufacturer_string", c_wchar_p),
    ("product_string", c_wchar_p),
    ("usage_page", c_ushort),
    ("usage", c_ushort),
    ("interface_number", c_int),
    ("next", POINTER(_HidDeviceInfo)),
]

_dll = None
_init_done = False


def _load():
    global _dll, _init_done
    if _dll is not None:
        return _dll
    for name in _DLL_NAMES:
        dll = native.load_dll(name, env_var="HIDAPI_DLL")
        if dll is not None:
            _dll = dll
            break
    if _dll is None:
        return None
    dll.hid_init.restype = c_int
    dll.hid_init.argtypes = []
    dll.hid_enumerate.restype = POINTER(_HidDeviceInfo)
    dll.hid_enumerate.argtypes = [c_ushort, c_ushort]
    dll.hid_free_enumeration.argtypes = [POINTER(_HidDeviceInfo)]
    dll.hid_open.restype = c_void_p
    dll.hid_open.argtypes = [c_ushort, c_ushort, c_wchar_p]
    dll.hid_open_path.restype = c_void_p
    dll.hid_open_path.argtypes = [c_char_p]
    dll.hid_close.argtypes = [c_void_p]
    dll.hid_write.restype = c_int
    dll.hid_write.argtypes = [c_void_p, POINTER(c_ubyte), c_size_t]
    dll.hid_read_timeout.restype = c_int
    dll.hid_read_timeout.argtypes = [c_void_p, POINTER(c_ubyte), c_size_t, c_int]
    dll.hid_set_nonblocking.restype = c_int
    dll.hid_set_nonblocking.argtypes = [c_void_p, c_int]
    if hasattr(dll, "hid_report_length"):
        # hidapi >= 0.14：查询设备声明的报告长度（含报告 ID 字节）
        dll.hid_report_length.restype = c_int
        dll.hid_report_length.argtypes = [c_void_p, c_int, c_ubyte]
    dll.hid_send_feature_report.restype = c_int
    dll.hid_send_feature_report.argtypes = [c_void_p, POINTER(c_ubyte), c_size_t]
    dll.hid_get_feature_report.restype = c_int
    dll.hid_get_feature_report.argtypes = [c_void_p, POINTER(c_ubyte), c_size_t]
    dll.hid_get_manufacturer_string.restype = c_int
    dll.hid_get_manufacturer_string.argtypes = [c_void_p, c_wchar_p, c_size_t]
    dll.hid_get_product_string.restype = c_int
    dll.hid_get_product_string.argtypes = [c_void_p, c_wchar_p, c_size_t]
    dll.hid_get_serial_number_string.restype = c_int
    dll.hid_get_serial_number_string.argtypes = [c_void_p, c_wchar_p, c_size_t]
    dll.hid_error.restype = c_wchar_p
    dll.hid_error.argtypes = [c_void_p]
    if not _init_done:
        _dll.hid_init()
        _init_done = True
    return _dll


def available() -> bool:
    return _load() is not None


def load_info() -> str:
    """DLL 状态说明（用于 UI 显示）。"""
    if available():
        return "hidapi.dll 已加载"
    return native.load_error("hidapi.dll") or "未找到 hidapi.dll"


def enumerate_devices(vid: int = 0, pid: int = 0) -> List[dict]:
    """枚举 HID 设备；vid/pid 为 0 表示全部。"""
    dll = _load()
    if dll is None:
        raise native.NativeError(load_info())
    head = dll.hid_enumerate(vid & 0xFFFF, pid & 0xFFFF)
    out = []
    node = head
    while node:
        info = node.contents
        out.append({
            "path": info.path or b"",
            "vid": int(info.vendor_id),
            "pid": int(info.product_id),
            "serial": info.serial_number or "",
            "release": int(info.release_number),
            "manufacturer": info.manufacturer_string or "",
            "product": info.product_string or "",
            "usage_page": int(info.usage_page),
            "usage": int(info.usage),
            "interface": int(info.interface_number),
        })
        node = info.next
    if head:
        dll.hid_free_enumeration(head)
    return out


class HidDevice:
    """单个 HID 设备句柄（非线程安全，建议在 worker 线程独占使用）。"""

    def __init__(self):
        self._handle: Optional[int] = None

    @property
    def opened(self) -> bool:
        return bool(self._handle)

    def open(self, vid: int, pid: int, serial: str = "") -> None:
        dll = _load()
        if dll is None:
            raise native.NativeError(load_info())
        h = dll.hid_open(vid & 0xFFFF, pid & 0xFFFF, serial or None)
        if not h:
            raise native.NativeError(self._err(h, f"打开 {vid:04X}:{pid:04X} 失败"))
        self._handle = h

    def open_path(self, path: bytes) -> None:
        dll = _load()
        if dll is None:
            raise native.NativeError(load_info())
        h = dll.hid_open_path(path)
        if not h:
            raise native.NativeError("按路径打开 HID 设备失败")
        self._handle = h

    def close(self) -> None:
        if self._handle:
            dll = _load()
            if dll is not None:
                dll.hid_close(self._handle)
            self._handle = None

    # ── 数据传输 ───────────────────────────────────────────────

    def write(self, data: bytes) -> int:
        """写输出报告；返回实际写入字节数。data 首字节应为报告 ID(无则 0x00)。

        Windows 的 WriteFile 把首字节当报告 ID 校验：ID 与设备报告描述符
        不符（或长度超声明值）即报 0x57 参数错误。此处失败时自动用另一
        种报告 ID 形式重试一次（首字节为 0 则去掉、非 0 则补 0），让
        带编号/不带编号报告的设备都能写成功。
        """
        self._require_open()
        dll = _load()
        data = bytes(data)
        try:
            return self._raw_write(dll, data)
        except native.NativeError:
            alt = data[1:] if data[:1] == b"\x00" else b"\x00" + data
            if alt and alt != data:
                return self._raw_write(dll, alt)
            raise

    def _raw_write(self, dll, data: bytes) -> int:
        buf = (c_ubyte * len(data)).from_buffer_copy(data)
        n = dll.hid_write(self._handle, buf, len(data))
        if n < 0:
            raise native.NativeError(self._err(self._handle, "HID 写入失败"))
        return n

    def report_lengths(self) -> dict:
        """设备声明的报告长度（含报告 ID 字节）；DLL 不支持时返回 {}。"""
        self._require_open()
        dll = _load()
        if dll is None or not hasattr(dll, "hid_report_length"):
            return {}
        out = {}
        for key, rtype in (("input", 0), ("output", 1), ("feature", 2)):
            try:
                out[key] = int(dll.hid_report_length(self._handle, rtype, 0))
            except Exception:
                out[key] = 0
        return out

    def read(self, size: int = 512, timeout_ms: int = 100) -> bytes:
        """读输入报告；超时返回 b''。"""
        self._require_open()
        dll = _load()
        buf = (c_ubyte * size)()
        n = dll.hid_read_timeout(self._handle, buf, size, int(timeout_ms))
        if n < 0:
            raise native.NativeError(self._err(self._handle, "HID 读取失败"))
        return bytes(buf[:n])

    def send_feature_report(self, data: bytes) -> int:
        self._require_open()
        dll = _load()
        buf = (c_ubyte * len(data)).from_buffer_copy(bytes(data))
        n = dll.hid_send_feature_report(self._handle, buf, len(data))
        if n < 0:
            raise native.NativeError(self._err(self._handle, "发送特征报告失败"))
        return n

    def get_feature_report(self, report_id: int, size: int = 512) -> bytes:
        self._require_open()
        dll = _load()
        buf = (c_ubyte * size)()
        buf[0] = report_id & 0xFF
        n = dll.hid_get_feature_report(self._handle, buf, size)
        if n < 0:
            raise native.NativeError(self._err(self._handle, "获取特征报告失败"))
        return bytes(buf[:n])

    # ── 描述字符串 ─────────────────────────────────────────────

    def get_strings(self) -> dict:
        self._require_open()
        dll = _load()

        def _get(fn):
            buf = ctypes.create_unicode_buffer(512)
            if fn(self._handle, buf, 512) >= 0:
                return buf.value
            return ""

        return {
            "manufacturer": _get(dll.hid_get_manufacturer_string),
            "product": _get(dll.hid_get_product_string),
            "serial": _get(dll.hid_get_serial_number_string),
        }

    # ── 内部 ───────────────────────────────────────────────────

    def _require_open(self):
        if not self._handle:
            raise native.NativeError("HID 设备未打开")

    @staticmethod
    def _err(handle, fallback: str) -> str:
        try:
            dll = _load()
            if dll is not None and handle:
                msg = dll.hid_error(handle)
                if msg:
                    return msg.strip()
        except Exception:
            pass
        return fallback
