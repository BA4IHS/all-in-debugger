# coding: utf-8
"""CMSIS-DAP 协议 + SWD 传输（RTT 调试核心）。

接入方式：通过 hidapi.dll 直连 DAP 调试器的 USB HID 接口（CMSIS-DAP v1）。
不依赖任何调试厂商 DLL（Keil CMSIS_DAP.dll 是 32 位私有插件，无公开接口，
无法在 Python 中使用）。

分层：
- DapProbe：HID 打开 + CMSIS-DAP 命令封装（connect/transfer/reset 等）
- SwdTarget：基于 DAP_Transfer 的 SWD 端口读写（DP/AP 寄存器、内存访问）
"""
import time
from typing import List, Optional, Tuple

from app import native
from app import hid_binding

# ── CMSIS-DAP 命令 ID ────────────────────────────────────────────

DAP_INFO = 0x00
DAP_HOST_STATUS = 0x01
DAP_CONNECT = 0x02
DAP_DISCONNECT = 0x03
DAP_TRANSFER_CONFIGURE = 0x04
DAP_TRANSFER = 0x05
DAP_TRANSFER_BLOCK = 0x06
DAP_SWJ_PINS = 0x10
DAP_SWJ_CLOCK = 0x11
DAP_SWJ_SEQUENCE = 0x12
DAP_SWD_CONFIGURE = 0x13

DAP_INFO_PACKET_SIZE = 0xFE

# DAP_Connect 端口
DAP_PORT_SWD = 2
DAP_PORT_JTAG = 1

# SWD 请求位（bit0=start, bit1=APnDP, bit2=RnW, bit3-4=A[3:2],
# bit5=偶校验, bit7=park）
SWD_REQ_AP = 1 << 1
SWD_REQ_READ = 1 << 2
SWD_ACK_OK = 0x01
SWD_ACK_WAIT = 0x02
SWD_ACK_FAULT = 0x04

_DP_SELECT = 0x08
_DP_CTRL_STAT = 0x04
_AP_CSW = 0x00
_AP_TAR = 0x04
_AP_DRW = 0x0C

_CSW_32BIT = 0x02
_CSW_AUTO_INC = 0x10

# DAP_SWJ_Pins 引脚位
PIN_SWCLK = 1 << 0
PIN_SWDIO = 1 << 1
PIN_NRST = 1 << 7

class DapError(native.NativeError):
    pass


def available() -> bool:
    """DAP 直连依赖 hidapi.dll。"""
    return hid_binding.available()


def load_info() -> str:
    if hid_binding.available():
        return "hidapi.dll 已加载（DAP 调试器经 USB HID 直连，无需厂商 DLL）"
    return hid_binding.load_info()


def _parity(v: int) -> int:
    return bin(v).count("1") & 1


def enum_probes(verify: bool = False) -> List[dict]:
    """通过 HID 枚举 CMSIS-DAP 调试器候选（usage_page=0xFF00 + usage=0x0001
    或产品名含 cmsis-dap）。

    verify=True 时逐个候选发 DAP_Info 在线验证：触摸屏等 vendor HID
    会冒充 0xFF00/0x0001，仅凭枚举字段无法区分（打开失败/无 DAP 响应即排除）。
    """
    try:
        devs = hid_binding.enumerate_devices()
    except Exception:
        devs = []
    out = []
    for d in devs:
        prod = (d.get("product") or "").lower()
        # CMSIS-DAP v1 惯例：厂商页 0xFF00 + usage 0x0001
        if ((d.get("usage_page") == 0xFF00 and d.get("usage") == 0x0001)
                or "cmsis-dap" in prod or "cmsis_dap" in prod):
            if not verify or _verify_dap(d["path"]):
                out.append(d)
    return out


def _verify_dap(path: bytes) -> bool:
    """打开候选设备发 DAP_Info，仅当收到合法 DAP 响应才认定为调试器。"""
    hid = hid_binding.HidDevice()
    try:
        hid.open_path(path)
        payload = bytes([DAP_INFO, 0x00])
        payload += b"\x00" * (65 - len(payload))  # 报告 ID 0 + 64 字节包
        hid.write(payload)
        data = hid.read(65, timeout_ms=150)
        return bool(data) and data[1] == DAP_INFO
    except Exception:
        return False
    finally:
        hid.close()


class DapProbe:
    """一个 CMSIS-DAP 调试器（USB HID 直连，CMSIS-DAP v1）。"""

    def __init__(self):
        self._hid: Optional[hid_binding.HidDevice] = None
        self.packet_size = 64   # HID 报告默认 64，连接后按 DAP_Info 更新
        self.path = b""

    @property
    def opened(self) -> bool:
        return self._hid is not None and self._hid.opened

    def open(self, path: bytes = b"") -> None:
        """path 为空时打开第一个探测到的调试器。"""
        probes = enum_probes()
        if not probes:
            raise DapError("未找到 CMSIS-DAP 调试器（请确认已插入并安装驱动）")
        target = None
        if path:
            target = next((p for p in probes if p["path"] == path), None)
        if target is None:
            target = probes[0]
        hid = hid_binding.HidDevice()
        try:
            hid.open_path(target["path"])
        except native.NativeError as e:
            raise DapError(f"打开调试器失败：{e}")
        self._hid = hid
        self.path = target["path"]
        self._query_packet_size()

    def close(self) -> None:
        if self._hid is not None:
            self._hid.close()
            self._hid = None

    # ── 包收发 ─────────────────────────────────────────────────

    def exchange(self, req: bytes) -> bytes:
        if self._hid is None or not self._hid.opened:
            raise DapError("调试器未打开")
        payload = b"\x00" + req
        if len(payload) < self.packet_size + 1:
            payload += b"\x00" * (self.packet_size + 1 - len(payload))
        self._hid.write(payload)
        data = self._hid.read(self.packet_size + 1, timeout_ms=500)
        return bytes(data[1:]) if data else b""

    def _query_packet_size(self):
        try:
            rsp = self.exchange(bytes([DAP_INFO, DAP_INFO_PACKET_SIZE]))
            if len(rsp) >= 4 and rsp[0] == DAP_INFO:
                size = int.from_bytes(rsp[2:4], "little")
                if 16 <= size <= 4096:
                    self.packet_size = size
        except DapError:
            pass

    # ── CMSIS-DAP 命令 ─────────────────────────────────────────

    def connect(self, port: int = DAP_PORT_SWD) -> int:
        rsp = self.exchange(bytes([DAP_CONNECT, port]))
        if not rsp or rsp[0] != DAP_CONNECT or rsp[1] == 0:
            raise DapError("DAP_Connect 失败（检查接线/供电）")
        return rsp[1]

    def disconnect(self):
        try:
            self.exchange(bytes([DAP_DISCONNECT]))
        except DapError:
            pass

    def set_clock(self, hz: int):
        self.exchange(DAP_SWJ_CLOCK.to_bytes(1, "little")
                      + int(hz).to_bytes(4, "little"))

    def host_status(self, status_type: int, on: bool):
        self.exchange(bytes([DAP_HOST_STATUS, status_type, 1 if on else 0]))

    def swj_sequence(self, bits: bytes, bit_count: int):
        req = bytes([DAP_SWJ_SEQUENCE, bit_count & 0xFF]) + bits
        rsp = self.exchange(req)
        if not rsp or rsp[0] != DAP_SWJ_SEQUENCE or rsp[1] != 0:
            raise DapError("SWJ 序列执行失败")

    def line_reset(self):
        """SWD 线复位：50+ 个 1 + 空闲。"""
        self.swj_sequence(b"\xFF" * 8, 64)

    def swj_pins(self, value: int, select: int, wait_us: int = 1000):
        req = bytes([DAP_SWJ_PINS, value & 0xFF, select & 0xFF]) \
            + int(wait_us).to_bytes(4, "little")
        rsp = self.exchange(req)
        return rsp[1] if len(rsp) >= 2 else -1

    def reset_target(self):
        """拉低 nRESET 再释放（硬件复位，需连接 RESET 线）。"""
        self.swj_pins(0, PIN_NRST, 50_000)
        self.swj_pins(PIN_NRST, PIN_NRST, 50_000)

    def transfer_configure(self, idle_cycles=0, wait_retry=100, match_retry=0):
        req = bytes([DAP_TRANSFER_CONFIGURE, idle_cycles & 0xFF]) \
            + wait_retry.to_bytes(2, "little") \
            + match_retry.to_bytes(2, "little")
        self.exchange(req)

    def transfer(self, dap_index: int, reqs: List[Tuple[int, Optional[int]]]
                 ) -> Tuple[int, int, List[int]]:
        """DAP_Transfer：reqs = [(request_byte, write_value|None), ...]。

        返回 (response, count, read_values)。
        """
        body = bytearray([DAP_TRANSFER, dap_index & 0xFF, len(reqs)])
        for req, val in reqs:
            body.append(req & 0xFF)
            if val is not None:
                body += int(val).to_bytes(4, "little")
        rsp = self.exchange(bytes(body))
        if not rsp or rsp[0] != DAP_TRANSFER:
            raise DapError("DAP_Transfer 无响应")
        count = rsp[2]
        values = []
        off = 3
        for req, val in reqs:
            if val is None and off + 4 <= len(rsp):
                values.append(int.from_bytes(rsp[off:off + 4], "little"))
                off += 4
        return rsp[1], count, values

    def transfer_block_write(self, dap_index: int, request: int,
                             values: List[int]) -> int:
        n = len(values)
        body = bytearray([DAP_TRANSFER_BLOCK, dap_index & 0xFF]) \
            + n.to_bytes(2, "little") + bytes([request])
        for v in values:
            body += int(v).to_bytes(4, "little")
        rsp = self.exchange(bytes(body))
        if not rsp or rsp[0] != DAP_TRANSFER_BLOCK:
            raise DapError("DAP_TransferBlock 无响应")
        return rsp[1]  # ACK


class SwdTarget:
    """基于 DapProbe 的 SWD 目标访问（DP/AP/内存）。"""

    def __init__(self, probe: DapProbe, dap_index: int = 0):
        self.probe = probe
        self.dap_index = dap_index
        self._select_cache = -1

    # ── 底层 SWD 读写 ──────────────────────────────────────────

    def _swd_req(self, ap: bool, read: bool, addr: int) -> int:
        """构造 SWD 请求字节（寄存器字地址只取 A[3:2]，偶校验）。"""
        req = 0x81  # start + park
        if ap:
            req |= SWD_REQ_AP
        if read:
            req |= SWD_REQ_READ
        req |= ((addr >> 2) & 0x3) << 3
        p = _parity(((addr >> 2) & 0x3)
                    | (SWD_REQ_AP if ap else 0)
                    | (SWD_REQ_READ if read else 0))
        req |= p << 5
        return req

    def dp_read(self, addr: int) -> int:
        _, count, vals = self.probe.transfer(
            self.dap_index, [(self._swd_req(False, True, addr), None)])
        if count < 1:
            raise DapError(f"DP 读 {addr:#x} 失败")
        return vals[0]

    def dp_write(self, addr: int, value: int):
        resp, count, _ = self.probe.transfer(
            self.dap_index, [(self._swd_req(False, False, addr), value)])
        if resp != SWD_ACK_OK or count < 1:
            raise DapError(f"DP 写 {addr:#x} ACK={resp:#x}")

    def ap_read(self, addr: int, ap_sel: int = 0) -> int:
        self._set_ap(ap_sel)
        # AP 读需要两次 transfer：第一次启动，第二次取 RDBUFF
        _, _, _ = self.probe.transfer(
            self.dap_index, [(self._swd_req(True, True, addr), None)])
        _, count, vals = self.probe.transfer(
            self.dap_index, [(self._swd_req(False, True, 0x0C), None)])
        if count < 1:
            raise DapError(f"AP 读 {addr:#x} 失败")
        return vals[0]

    def ap_write(self, addr: int, value: int, ap_sel: int = 0):
        self._set_ap(ap_sel)
        resp, count, _ = self.probe.transfer(
            self.dap_index, [(self._swd_req(True, False, addr), value)])
        if resp != SWD_ACK_OK or count < 1:
            raise DapError(f"AP 写 {addr:#x} ACK={resp:#x}")

    def _set_ap(self, ap_sel: int):
        want = (ap_sel << 24)
        if self._select_cache != want:
            self.dp_write(_DP_SELECT, want)
            self._select_cache = want

    # ── 内存访问 ───────────────────────────────────────────────

    def _setup_mem(self, ap_sel: int = 0):
        self.ap_write(_AP_CSW, _CSW_32BIT, ap_sel)

    def read_mem32(self, addr: int, ap_sel: int = 0) -> int:
        self._setup_mem(ap_sel)
        self.ap_write(_AP_TAR, addr & 0xFFFFFFFC, ap_sel)
        return self.ap_read(_AP_DRW, ap_sel)

    def write_mem32(self, addr: int, value: int, ap_sel: int = 0):
        self._setup_mem(ap_sel)
        self.ap_write(_AP_TAR, addr & 0xFFFFFFFC, ap_sel)
        self.ap_write(_AP_DRW, int(value) & 0xFFFFFFFF, ap_sel)

    def read_mem_block(self, addr: int, count: int, ap_sel: int = 0) -> bytes:
        """按 32 位字读内存（TAR 自增，跨 1KB 边界分段）。"""
        self.ap_write(_AP_CSW, _CSW_32BIT | _CSW_AUTO_INC, ap_sel)
        out = bytearray()
        pos = addr & 0xFFFFFFFC
        remain = count
        while remain > 0:
            chunk = min(remain, 0x400 - (pos & 0x3FF))
            chunk -= chunk & 3
            if chunk <= 0:
                chunk = 4
            self.ap_write(_AP_TAR, pos, ap_sel)
            reqs = [(self._swd_req(True, True, _AP_DRW), None)
                    for _ in range(chunk // 4)]
            _, got, vals = self.probe.transfer(self.dap_index, reqs)
            if got < len(reqs):
                raise DapError(f"内存读 {pos:#x} 中断（got={got}）")
            for v in vals:
                out += v.to_bytes(4, "little")
            pos += chunk
            remain -= chunk
        self.ap_write(_AP_CSW, _CSW_32BIT, ap_sel)
        return bytes(out[:count])

    def write_mem_block(self, addr: int, data: bytes, ap_sel: int = 0):
        self.ap_write(_AP_CSW, _CSW_32BIT | _CSW_AUTO_INC, ap_sel)
        pad = data + b"\x00" * ((4 - len(data) & 3) & 3)
        pos = addr & 0xFFFFFFFC
        off = 0
        while off < len(pad):
            chunk = min(len(pad) - off, 0x400 - (pos & 0x3FF))
            chunk -= chunk & 3
            if chunk <= 0:
                chunk = 4
            self.ap_write(_AP_TAR, pos, ap_sel)
            reqs = [(self._swd_req(True, False, _AP_DRW),
                     int.from_bytes(pad[off + i:off + i + 4], "little"))
                    for i in range(0, chunk, 4)]
            resp, got, _ = self.probe.transfer(self.dap_index, reqs)
            if got < len(reqs) or resp != SWD_ACK_OK:
                raise DapError(f"内存写 {pos:#x} 失败（ACK={resp:#x}）")
            pos += chunk
            off += chunk
        self.ap_write(_AP_CSW, _CSW_32BIT, ap_sel)

    def read_idcode(self) -> int:
        self.probe.line_reset()
        self.dp_read(_DP_CTRL_STAT)  # 清 RDBUFF
        return self.dp_read(0x00)
