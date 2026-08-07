# coding: utf-8
"""winusb.dll 的 ctypes 绑定（CMSIS-DAP v2 批量传输层）。

CMSIS-DAP v2 调试器以 WinUSB 厂商接口（bulk IN/OUT 端点）暴露，不经 HID，
hidapi 无法访问；本模块直接调用 Windows 系统 winusb.dll。

对外提供：
- available() / load_info()：DLL 是否可用
- enumerate_interfaces()：枚举当前在位的 WinUSB 接口设备路径
- parse_vid_pid(path)：从设备路径解析 VID/PID
- WinUsbDevice：打开/关闭/批量读写（管道超时策略内置）

线程安全约定：同一个 WinUsbDevice 只应在一个线程内使用（worker 线程）。

本模块全部 API 调用约定均在本机 DAPLink v2（0D28:0204，固件 v0257
local mods，CMSIS-DAP 2.1.0）上实测验证过：
- CreateFileW 必须带 FILE_FLAG_OVERLAPPED，否则 WinUsb_Initialize 报 err=6
- WinUSB 接口 GUID 固定为 {CDB3B5AD-293B-4663-AA36-1AAE46463776}
- CM_Get_Device_Interface_List flag=0（PRESENT）才排除幽灵设备节点
- PIPE_TRANSFER_TIMEOUT(0x03) 管道策略控制读超时，超时 GetLastError=121
"""
import ctypes
import re
from ctypes import (POINTER, Structure, byref, c_ubyte, c_ulong, c_ushort,
                    c_void_p, c_wchar_p, create_unicode_buffer, windll)
from typing import List, Optional, Tuple

from app import native

# WinUSB 标准设备接口 GUID（本机注册表 DEVPKEY_DeviceInterfaceGUIDs 核实）
WINUSB_INTERFACE_GUID = "{CDB3B5AD-293B-4663-AA36-1AAE46463776}"

# CreateFileW 常量
_GENERIC_RW = 0xC0000000          # GENERIC_READ | GENERIC_WRITE
_SHARE_RW = 0x00000003            # FILE_SHARE_READ | FILE_SHARE_WRITE
_OPEN_EXISTING = 3
_FILE_FLAG_OVERLAPPED = 0x40000000  # WinUsb_Initialize 硬性要求（实测）
_INVALID_HANDLE = (1 << (ctypes.sizeof(c_void_p) * 8)) - 1

# WinUSB 管道策略
_PIPE_TRANSFER_TIMEOUT = 0x03
_ERR_TIMEOUT = 121                # ERROR_SEM_TIMEOUT：管道超时策略触发

_VID_PID_RE = re.compile(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})")


class _GUID(Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", c_ushort),
                ("Data3", c_ushort), ("Data4", c_ubyte * 8)]


def _make_guid(s: str) -> _GUID:
    g = _GUID()
    windll.ole32.CLSIDFromString(s, byref(g))
    return g


class _USB_INTERFACE_DESCRIPTOR(Structure):
    _fields_ = [("bLength", c_ubyte), ("bDescriptorType", c_ubyte),
                ("bInterfaceNumber", c_ubyte), ("bAlternateSetting", c_ubyte),
                ("bNumEndpoints", c_ubyte), ("bInterfaceClass", c_ubyte),
                ("bInterfaceSubClass", c_ubyte),
                ("bInterfaceProtocol", c_ubyte), ("iInterface", c_ubyte)]


class _WINUSB_PIPE_INFORMATION(Structure):
    _fields_ = [("PipeType", c_ulong), ("PipeId", c_ubyte),
                ("MaximumPacketSize", c_ushort), ("Interval", c_ubyte)]


_dll = None
_load_err = ""


def _load():
    """加载 winusb.dll 并声明 API 原型（带缓存）。"""
    global _dll, _load_err
    if _dll is not None:
        return _dll
    # winusb.dll 是 Windows 系统组件：优先程序 libs 目录，回退系统搜索路径
    dll = native.load_dll("winusb.dll")
    if dll is None:
        try:
            dll = ctypes.WinDLL("winusb.dll")
        except OSError as e:
            _load_err = f"加载系统 winusb.dll 失败：{e}"
            return None
    # 64 位下句柄是指针宽度，必须显式声明，否则被 c_int 截断（实测 err=6）
    dll.WinUsb_Initialize.argtypes = [c_void_p, POINTER(c_void_p)]
    dll.WinUsb_QueryInterfaceSettings.argtypes = [
        c_void_p, c_ubyte, POINTER(_USB_INTERFACE_DESCRIPTOR)]
    dll.WinUsb_QueryPipe.argtypes = [
        c_void_p, c_ubyte, c_ubyte, POINTER(_WINUSB_PIPE_INFORMATION)]
    dll.WinUsb_SetPipePolicy.argtypes = [c_void_p, c_ubyte, c_ulong,
                                         c_ulong, c_void_p]
    dll.WinUsb_WritePipe.argtypes = [c_void_p, c_ubyte, ctypes.c_char_p,
                                     c_ulong, POINTER(c_ulong), c_void_p]
    dll.WinUsb_ReadPipe.argtypes = [c_void_p, c_ubyte, POINTER(c_ubyte),
                                    c_ulong, POINTER(c_ulong), c_void_p]
    dll.WinUsb_Free.argtypes = [c_void_p]
    k32 = windll.kernel32
    k32.CreateFileW.restype = c_void_p
    k32.CreateFileW.argtypes = [c_wchar_p, c_ulong, c_ulong, c_void_p,
                                c_ulong, c_ulong, c_void_p]
    _dll = dll
    return dll


def available() -> bool:
    return _load() is not None


def load_info() -> str:
    """DLL 状态说明（用于 UI 显示）。"""
    if available():
        return "winusb.dll 已加载（CMSIS-DAP v2 批量传输可用）"
    return _load_err or "winusb.dll 不可用"


def parse_vid_pid(path: str) -> Tuple[int, int]:
    """从 \\\\?\\USB#VID_xxxx&PID_yyyy... 设备路径解析 (vid, pid)。

    解析失败返回 (0, 0)。
    """
    m = _VID_PID_RE.search(path or "")
    if not m:
        return (0, 0)
    return (int(m.group(1), 16), int(m.group(2), 16))


def enumerate_interfaces() -> List[str]:
    """枚举当前在位（PRESENT）的 WinUSB 接口设备路径。

    flag 必须为 0（PRESENT）：传 1（ALL）会混入已拔除设备的幽灵节点，
    其 CreateFile 会失败（实测）。
    """
    if _load() is None:
        return []
    cfg = windll.cfgmgr32
    g = _make_guid(WINUSB_INTERFACE_GUID)
    sz = c_ulong(0)
    cr = cfg.CM_Get_Device_Interface_List_SizeW(byref(sz), byref(g), None, 0)
    if cr != 0 or sz.value <= 1:
        return []
    buf = create_unicode_buffer(sz.value + 1)
    cr = cfg.CM_Get_Device_Interface_ListW(byref(g), None, buf,
                                           sz.value + 1, 0)
    if cr != 0:
        return []
    # 返回值为多字符串：各项以 NUL 分隔，整体以双 NUL 结尾
    raw = ctypes.wstring_at(buf, sz.value)
    return [s for s in raw.split("\x00") if s.strip()]


class WinUsbDevice:
    """单个 WinUSB 设备句柄（非线程安全，worker 线程独占使用）。"""

    def __init__(self):
        self._hfile: Optional[int] = None
        self._wh: Optional[c_void_p] = None
        self.ep_in = 0          # bulk IN 端点地址（bit7=1）
        self.ep_out = 0         # bulk OUT 端点地址
        self.max_packet_size = 0
        self.path = ""
        self._timeout_ms = 500  # 当前管道超时（毫秒），供 drain 恢复用

    @property
    def opened(self) -> bool:
        return self._wh is not None

    def open_path(self, path: str, timeout_ms: int = 500) -> None:
        dll = _load()
        if dll is None:
            raise native.NativeError(load_info())
        k32 = windll.kernel32
        h = k32.CreateFileW(path, _GENERIC_RW, _SHARE_RW, None,
                            _OPEN_EXISTING, _FILE_FLAG_OVERLAPPED, None)
        if not h or h == _INVALID_HANDLE:
            raise native.NativeError(
                f"打开 WinUSB 设备失败 err={k32.GetLastError()}")
        wh = c_void_p()
        if not dll.WinUsb_Initialize(h, byref(wh)):
            k32.CloseHandle(h)
            raise native.NativeError(
                f"WinUsb_Initialize 失败 err={k32.GetLastError()}")
        self._hfile, self._wh, self.path = h, wh, path
        try:
            self._setup_endpoints(dll)
            self._timeout_ms = int(timeout_ms)
            self.set_timeout(timeout_ms)
            self._drain_stale()
        except native.NativeError:
            self.close()
            raise

    def _drain_stale(self) -> None:
        """清空 IN 端点里上一次会话残留的响应。

        实测：设备被打开-关闭后，固件缓冲可能残留未读响应；下次打开
        若不先排空，首批读取会拿到陈旧数据（全 0x00）。用短超时反复读
        直到超时（说明缓冲已空），再恢复正常超时。
        """
        self.set_timeout(20)
        try:
            for _ in range(16):
                if not self.read(self.max_packet_size or 64):
                    break  # 超时返回空 → 缓冲已空
        finally:
            self.set_timeout(self._timeout_ms)

    def _setup_endpoints(self, dll) -> None:
        """查询接口 0 的 bulk IN/OUT 端点。"""
        ifd = _USB_INTERFACE_DESCRIPTOR()
        if not dll.WinUsb_QueryInterfaceSettings(self._wh, 0, byref(ifd)):
            raise native.NativeError("WinUsb_QueryInterfaceSettings 失败")
        self.ep_in = self.ep_out = 0
        self.max_packet_size = 0
        for i in range(ifd.bNumEndpoints):
            pi = _WINUSB_PIPE_INFORMATION()
            if not dll.WinUsb_QueryPipe(self._wh, 0, i, byref(pi)):
                continue
            if pi.PipeId & 0x80:
                self.ep_in = pi.PipeId
            else:
                self.ep_out = pi.PipeId
            self.max_packet_size = max(self.max_packet_size,
                                       pi.MaximumPacketSize)
        if not self.ep_in or not self.ep_out:
            raise native.NativeError("WinUSB 接口缺少 bulk IN/OUT 端点")

    def set_timeout(self, ms: int) -> None:
        """设置 IN/OUT 管道传输超时（毫秒）。"""
        dll = self._require()
        tv = c_ulong(int(ms) & 0xFFFFFFFF)
        for pid in (self.ep_in, self.ep_out):
            dll.WinUsb_SetPipePolicy(self._wh, pid, _PIPE_TRANSFER_TIMEOUT,
                                     4, byref(tv))

    def write(self, data: bytes) -> None:
        """bulk OUT 写一个完整包（调用方负责补零到协商包长）。"""
        dll = self._require()
        data = bytes(data)
        wr = c_ulong(0)
        ok = dll.WinUsb_WritePipe(self._wh, self.ep_out, data, len(data),
                                  byref(wr), None)
        if not ok:
            raise native.NativeError(
                f"WinUSB 写失败 err={windll.kernel32.GetLastError()}")

    def read(self, size: int) -> bytes:
        """bulk IN 读一个包；管道超时返回 b""（不抛异常）。"""
        dll = self._require()
        buf = (c_ubyte * max(1, int(size)))()
        rr = c_ulong(0)
        ok = dll.WinUsb_ReadPipe(self._wh, self.ep_in, buf, len(buf),
                                 byref(rr), None)
        if ok:
            return bytes(buf[:rr.value])
        err = windll.kernel32.GetLastError()
        if err == _ERR_TIMEOUT:
            return b""
        raise native.NativeError(f"WinUSB 读失败 err={err}")

    def close(self) -> None:
        if self._wh is not None:
            if _dll is not None:
                _dll.WinUsb_Free(self._wh)
            self._wh = None
        if self._hfile:
            windll.kernel32.CloseHandle(self._hfile)
            self._hfile = None

    def _require(self):
        dll = _load()
        if dll is None:
            raise native.NativeError(load_info())
        if self._wh is None:
            raise native.NativeError("WinUSB 设备未打开")
        return dll
