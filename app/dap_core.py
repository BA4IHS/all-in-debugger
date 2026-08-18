# coding: utf-8
"""CMSIS-DAP 协议 + SWD 传输（RTT 调试核心）。

接入方式（双传输，命令集相同）：
- CMSIS-DAP v1：hidapi.dll 直连调试器 USB HID 接口（报告 ID 0 + 命令包）
- CMSIS-DAP v2：winusb.dll 直连调试器 WinUSB 厂商接口（bulk IN/OUT，
  无报告 ID 前缀）。部分 v2 固件（如某些 DAPLink 改版）的 HID v1 端点
  不响应命令，只能走 WinUSB 通道。
不依赖任何调试厂商 DLL（Keil CMSIS_DAP.dll 是 32 位私有插件，无公开接口，
无法在 Python 中使用）。

分层：
- DapProbe：HID/WinUSB 打开 + CMSIS-DAP 命令封装（connect/transfer/reset 等）
- SwdTarget：基于 DAP_Transfer 的 SWD 端口读写（DP/AP 寄存器、内存访问）
"""
import time
from typing import List, Optional, Tuple

from app import native
from app import hid_binding
from app import winusb_binding

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
# CMSIS-DAP 规范（官方固件 DAP.h）：0=自动/禁用，1=SWD，2=JTAG
DAP_PORT_SWD = 1
DAP_PORT_JTAG = 2

# DAP_Transfer 请求字节位定义（CMSIS-DAP 规范，官方固件 DAP.h 核实）：
# bit0=APnDP(0=DP,1=AP) bit1=RnW(0=写,1=读) bit2=A2 bit3=A3
# bit4=ValueMatch bit5=MatchMask bit7=Timestamp。
# SWD 线帧的 start/偶校验/stop/park 位由调试器固件生成，
# 主机请求字节绝不能包含（实测：包含会导致 NO_ACK/应答错乱）。
SWD_REQ_AP = 1 << 0
SWD_REQ_READ = 1 << 1
SWD_ACK_OK = 0x01
SWD_ACK_WAIT = 0x02
SWD_ACK_FAULT = 0x04

_DP_SELECT = 0x08
_DP_CTRL_STAT = 0x04
_AP_CSW = 0x00
_AP_TAR = 0x04
_AP_DRW = 0x0C

_CSW_32BIT = 0x02
# PPB 调试寄存器（DHCSR 0xE000EDF0 等）须特权 AHB-AP 访问，否则被
# 目标拒绝（AP 写 0xC ACK=0x4）。真机实测（STM32F103 + DAPLink HID）：
# CSW.SPROT(bit8) 被目标 AHB-AP 忽略——写 0x102 读回 0x01000042
# （bit8 丢失、bit24/bit6 恒 1），PPB 访问仍 FAULT；必须用
# HPROT(0x23000000, bit29/25/24)。0x23000052 为真机验证通过值
# （HPROT|32位|AUTO_INC|0x40，写 DHCSR halt ACK=OK），其中 bit4
# (0x10) 即 AUTO_INC，单字访问时无副作用。
_CSW_PRIV = 0x23000052
_CSW_AUTO_INC = 0x10

# ABORT 寄存器（DP 0x00 写）：置位各位清除对应 sticky 错误
# 0x1E = ORUNERRCLR|STICKYCMPCLR|STICKYERRCLR|WDATAERRCLR
_ABORT_CLEAR_STICKY = 0x1E

# DAPLink v0257 固件实测限制（_diag_dap15/16.py）：
# - 响应缓冲 64 字节 → DAP_Transfer 读最多 15 字/次（3+4*15=63≤64），
#   16 字（67B）固件溢出挂死（WinUSB 读 err=31，之后整机无响应）；
# - 请求缓冲限制 → DAP_Transfer 写最多 13 字/次（3+5*13=68B OK，
#   14 字 73B 失败）。超限必须分多次 transfer，不能依赖 255 上限。
_DAP_MAX_READ_WORDS = 15
_DAP_MAX_WRITE_WORDS = 13

# CMSIS-DAP v1 HID 报告数据长度（不含报告 ID 字节；含报告 ID 为 65 字节）。
# 实测 DAPLink v0257 的 DAP_Info(0xFE) 返回 packet size=16（逻辑包长），
# 但 HID 物理报告固定 64 字节数据：短读会截断大响应（DAP_Transfer 63B
# 只取前 17B）且剩余字节缓存在 hidapi 内部导致后续响应错乱。HID 传输
# 必须始终按 65 字节完整报告读写，忽略固件声称的 packet size。
_HID_REPORT_SIZE = 64

# DP CTRL/STAT 电源位（ARM ADIv5 规范）
_CDBGPWRUPREQ = 0x40000000   # bit30 调试电源请求
_CSYSPWRUPREQ = 0x10000000   # bit28 系统电源请求
_CDBGPWRUPACK = 0x80000000   # bit31 调试电源确认
_CSYSPWRUPACK = 0x20000000   # bit29 系统电源确认
_POWERUP_REQUEST = _CDBGPWRUPREQ | _CSYSPWRUPREQ   # 0x50000000
_POWERUP_ACK = _CDBGPWRUPACK | _CSYSPWRUPACK       # 0xA0000000

# DAP_SWJ_Pins 引脚位
PIN_SWCLK = 1 << 0
PIN_SWDIO = 1 << 1
PIN_NRST = 1 << 7

class DapError(native.NativeError):
    pass


def available() -> bool:
    """DAP 直连依赖 hidapi.dll（v1）或系统 winusb.dll（v2），任一可用即可。"""
    return hid_binding.available() or winusb_binding.available()


def load_info() -> str:
    parts = []
    if hid_binding.available():
        parts.append("hidapi.dll 已加载（CMSIS-DAP v1 HID 直连）")
    else:
        parts.append(hid_binding.load_info())
    if winusb_binding.available():
        parts.append("winusb.dll 已加载（CMSIS-DAP v2 批量直连）")
    else:
        parts.append(winusb_binding.load_info())
    return "；".join(parts)


def enum_probes(verify: bool = False) -> List[dict]:
    """枚举 CMSIS-DAP 调试器候选（v1 HID + v2 WinUSB 双通道）。

    v1：HID 枚举 usage_page=0xFF00 + usage=0x0001 或产品名含 cmsis-dap。
    v2：WinUSB 接口无描述符级过滤手段（实测 DeviceDesc/FriendlyName 接口
    属性不存在），只能逐个打开后发 DAP_Info 在线验证；verify=False 时按
    产品名含 cmsis-dap 粗筛（路径中无法取产品名，故保守全部列出并标记）。

    verify=True 时逐个候选发 DAP_Info 在线验证：触摸屏等 vendor HID
    会冒充 0xFF00/0x0001，仅凭枚举字段无法区分（打开失败/无 DAP 响应即排除）。

    返回字典统一字段：path/vid/pid/product/transport（"hid"|"winusb"）。
    """
    out = []
    # ── v1：HID ──
    try:
        devs = hid_binding.enumerate_devices()
    except Exception:
        devs = []
    for d in devs:
        prod = (d.get("product") or "").lower()
        # CMSIS-DAP v1 惯例：厂商页 0xFF00 + usage 0x0001
        if ((d.get("usage_page") == 0xFF00 and d.get("usage") == 0x0001)
                or "cmsis-dap" in prod or "cmsis_dap" in prod):
            if not verify or _verify_dap_hid(d["path"]):
                item = dict(d)
                item["transport"] = "hid"
                out.append(item)
    # ── v2：WinUSB ──
    for path in winusb_binding.enumerate_interfaces():
        vid, pid = winusb_binding.parse_vid_pid(path)
        if verify:
            if not _verify_dap_winusb(path):
                continue
        item = {
            "path": path,  # str 类型，与 HID 的 bytes path 区分
            "vid": vid, "pid": pid,
            "product": "CMSIS-DAP v2",
            "transport": "winusb",
        }
        out.append(item)
    return out


def _verify_dap_hid(path: bytes) -> bool:
    """打开候选 HID 设备发 DAP_Info，仅当收到合法 DAP 响应才认定为调试器。"""
    hid = hid_binding.HidDevice()
    try:
        hid.open_path(path)
        payload = bytes([DAP_INFO, 0x00])
        payload += b"\x00" * (65 - len(payload))  # 报告 ID 0 + 64 字节包
        hid.write(payload)
        data = hid.read(65, timeout_ms=150)
        # hidapi 读回不含报告 ID，首字节即命令回显 0x00
        return bool(data) and data[0] == DAP_INFO
    except Exception:
        return False
    finally:
        hid.close()


def _verify_dap_winusb(path: str) -> bool:
    """打开候选 WinUSB 接口发 DAP_Info，验证是否为 CMSIS-DAP v2 调试器。

    WinUSB 接口无法在枚举阶段区分调试器与其他 WinUSB 设备，必须在线验证。
    """
    dev = winusb_binding.WinUsbDevice()
    try:
        dev.open_path(path, timeout_ms=150)
        req = bytes([DAP_INFO, 0x00])
        req += b"\x00" * (64 - len(req))
        dev.write(req)
        data = dev.read(64)
        return bool(data) and data[0] == DAP_INFO
    except Exception:
        return False
    finally:
        dev.close()


class DapProbe:
    """一个 CMSIS-DAP 调试器（v1 HID 或 v2 WinUSB，命令集相同）。

    传输由 path 类型区分：bytes → HID（v1），str → WinUSB（v2）。
    """

    def __init__(self):
        self._hid: Optional[hid_binding.HidDevice] = None
        self._winusb: Optional[winusb_binding.WinUsbDevice] = None
        self.packet_size = 64   # 默认 64，打开后按 DAP_Info(0xFE) 更新
        self.path = b""
        self.transport = "hid"  # "hid" | "winusb"

    @property
    def opened(self) -> bool:
        return ((self._hid is not None and self._hid.opened)
                or (self._winusb is not None and self._winusb.opened))

    def open(self, path=b"") -> None:
        """path 为空时打开第一个探测到的调试器。

        path 类型决定传输：bytes → HID v1，str → WinUSB v2。
        """
        probes = enum_probes()
        if not path and any(p.get("transport") == "winusb" for p in probes):
            # WinUSB 接口在枚举阶段无法区分调试器与其他 WinUSB 设备，
            # 自动选择时必须在线验证，避免打开非调试器设备
            probes = enum_probes(verify=True)
        if not probes:
            raise DapError("未找到 CMSIS-DAP 调试器（请确认已插入并安装驱动）")
        target = None
        if path:
            # HID path 为 bytes、WinUSB path 为 str；MCP 链路统一传 str，
            # 需兼容两种形式的比较
            target = next(
                (p for p in probes
                 if p["path"] == path
                 or (isinstance(path, str) and isinstance(p["path"], bytes)
                     and p["path"].decode("utf-8", "replace") == path)),
                None)
        if target is None:
            target = probes[0]
        if target.get("transport") == "winusb":
            self._open_winusb(target["path"])
        else:
            self._open_hid(target["path"])
        self.path = target["path"]
        self._query_packet_size()

    def _open_hid(self, path: bytes) -> None:
        hid = hid_binding.HidDevice()
        try:
            hid.open_path(path)
        except native.NativeError as e:
            raise DapError(f"打开调试器失败：{e}")
        self._hid = hid
        self.transport = "hid"

    def _open_winusb(self, path: str) -> None:
        dev = winusb_binding.WinUsbDevice()
        try:
            dev.open_path(path, timeout_ms=500)
        except native.NativeError as e:
            raise DapError(f"打开调试器失败：{e}")
        self._winusb = dev
        self.transport = "winusb"

    def close(self) -> None:
        # 关闭前通知固件断开端口，清理连接状态；
        # 未读响应残留由下次打开时的 drain 处理。
        # close 必须是纯清理操作：任何传输异常都不允许外泄
        # （否则 requestOpen 的 except DapError 分支会再次抛异常打崩线程）
        if self.opened:
            try:
                self.disconnect()
            except Exception:
                pass
        if self._hid is not None:
            self._hid.close()
            self._hid = None
        if self._winusb is not None:
            self._winusb.close()
            self._winusb = None

    # ── 包收发 ─────────────────────────────────────────────────

    def exchange(self, req: bytes) -> bytes:
        """发一个 CMSIS-DAP 命令包并收响应（不含报告 ID）。"""
        if self._winusb is not None and self._winusb.opened:
            return self._exchange_winusb(req)
        if self._hid is not None and self._hid.opened:
            return self._exchange_hid(req)
        raise DapError("调试器未打开")

    def _exchange_hid(self, req: bytes) -> bytes:
        # CMSIS-DAP v1 HID 报告固定 65 字节（报告 ID 0 + 64 数据）。
        # 不能按 self.packet_size 缩短：packet_size 取自固件 DAP_Info(0xFE)
        # （DAPLink 实测返回 16），短读会截断 DAP_Transfer 等大响应并在
        # hidapi 内部残留缓存，导致后续响应错乱；必须读写完整报告。
        payload = b"\x00" + req
        if len(payload) < _HID_REPORT_SIZE + 1:
            payload += b"\x00" * (_HID_REPORT_SIZE + 1 - len(payload))
        try:
            self._hid.write(payload)
            data = self._hid.read(_HID_REPORT_SIZE + 1, timeout_ms=500)
        except native.NativeError as e:
            # 传输层原始异常必须转 DapError：上层（requestOpen/轮询/复位）
            # 只捕获 DapError。原生错误（如连接失败后设备句柄失效的
            # WriteFile 0x1 ERROR_INVALID_FUNCTION）漏出会打崩 worker
            # 线程（PyQt6 槽内未捕获异常 → 线程终止，应用报错退出）
            raise DapError(f"HID 传输失败：{e}")
        # hidapi 读回完整报告（实测首字节即 DAP 命令回显，无报告 ID 前缀），
        # 与 WinUSB 短包响应同一数据格式，调用方按 rsp[0..n] 解析兼容
        return bytes(data) if data else b""

    def _exchange_winusb(self, req: bytes) -> bytes:
        # v2 批量通道无报告 ID 前缀，直接发命令包
        payload = bytes(req)
        if len(payload) < self.packet_size:
            payload += b"\x00" * (self.packet_size - len(payload))
        try:
            self._winusb.write(payload)
            # 大响应（如内存批量读）跨多个 bulk 包。实测（DAPLink v0257）：
            # WinUSB 驱动不会聚合多包，单次 ReadPipe 读大 buffer 会
            # err=31 失败；必须按 packet_size 逐包读，直到读回短包
            # （长度 < packet_size）即传输结束。响应长度恒为 3+4n，
            # 永非 64 的倍数，故必有短包收尾（不靠超时误判）。
            total = bytearray()
            for _ in range(64):     # 上限防护（最大响应 3+4*255≈1KB）
                chunk = self._winusb.read(self.packet_size)
                if not chunk:
                    break           # 管道超时返回空 → 传输结束
                total += chunk
                if len(chunk) < self.packet_size:
                    break           # 短包 → 传输结束
        except native.NativeError as e:
            raise DapError(f"WinUSB 传输失败：{e}")
        return bytes(total)

    def _query_packet_size(self):
        try:
            rsp = self.exchange(bytes([DAP_INFO, DAP_INFO_PACKET_SIZE]))
            if len(rsp) >= 4 and rsp[0] == DAP_INFO:
                size = int.from_bytes(rsp[2:4], "little")
                if 16 <= size <= 4096:
                    # HID 传输物理报告长度固定 64 字节（CMSIS-DAP v1），
                    # 固件 DAP_Info(0xFE) 返回值（DAPLink 实测 16）是逻辑
                    # 包长，用于 HID 会把大响应截断为 17 字节；必须忽略。
                    # WinUSB 为分包循环读（短包结束），不受影响。
                    if self.transport == "hid":
                        size = _HID_REPORT_SIZE
                    self.packet_size = size
        except DapError:
            pass

    # ── CMSIS-DAP 命令 ─────────────────────────────────────────

    def connect(self, port: int = DAP_PORT_SWD) -> int:
        rsp = self.exchange(bytes([DAP_CONNECT, port]))
        if not rsp or rsp[0] != DAP_CONNECT:
            raise DapError("DAP_Connect 无响应（调试器未应答，"
                           "检查驱动/固件或换 USB 口重试）")
        if rsp[1] == 0:
            # 传输层正常但端口未建立：目标芯片未接/未上电
            raise DapError("DAP_Connect 失败：调试器正常但未检测到目标芯片"
                           "（检查 SWD 接线/芯片供电）")
        return rsp[1]

    def disconnect(self):
        # 断开是尽力而为：设备已失效/句柄陈旧时交换本身会失败，
        # 一律吞掉，绝不能抛出（close 路径不允许抛异常）
        try:
            self.exchange(bytes([DAP_DISCONNECT]))
        except Exception:
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
        """SWD 线复位：50+ 个 1。"""
        self.swj_sequence(b"\xFF" * 8, 64)

    def swd_activate(self):
        """完整 SWD 激活序列（实测必需，仅线复位会得到 NO_ACK）。

        1) 64 个 1 线复位；
        2) 16 位 JTAG→SWD 选择码 0xE79E（LSB 先发，字节序 0x9E 0xE7）；
        3) 再次 64 个 1 线复位；
        4) 8 个 0 空闲周期。
        """
        self.swj_sequence(b"\xFF" * 8, 64)
        self.swj_sequence(b"\x9E\xE7", 16)
        self.swj_sequence(b"\xFF" * 8, 64)
        self.swj_sequence(b"\x00", 8)

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

        响应包格式（CMSIS-DAP 规范）：[0x05, Count, Response(ACK), 读数据...]。
        返回 (ack, count, read_values)。
        """
        body = bytearray([DAP_TRANSFER, dap_index & 0xFF, len(reqs)])
        for req, val in reqs:
            body.append(req & 0xFF)
            if val is not None:
                body += int(val).to_bytes(4, "little")
        rsp = self.exchange(bytes(body))
        if not rsp or rsp[0] != DAP_TRANSFER:
            raise DapError("DAP_Transfer 无响应")
        count = rsp[1]
        ack = rsp[2] & 0x07
        values = []
        off = 3
        for req, val in reqs:
            if val is None and off + 4 <= len(rsp):
                values.append(int.from_bytes(rsp[off:off + 4], "little"))
                off += 4
        return ack, count, values

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
        # 响应格式：[0x06, Count低, Count高, Response(ACK)]
        return rsp[3] if len(rsp) >= 4 else -1


class SwdTarget:
    """基于 DapProbe 的 SWD 目标访问（DP/AP/内存）。"""

    def __init__(self, probe: DapProbe, dap_index: int = 0):
        self.probe = probe
        self.dap_index = dap_index
        self._select_cache = -1

    # ── 底层 SWD 读写 ──────────────────────────────────────────

    def _swd_req(self, ap: bool, read: bool, addr: int) -> int:
        """构造 DAP_Transfer 请求字节（CMSIS-DAP 协议格式）。

        bit0=APnDP bit1=RnW bit2=A2 bit3=A3（寄存器字地址取 A[3:2]）。
        线帧 start/校验/park 由调试器固件生成，此处不参与。
        """
        req = ((addr >> 2) & 0x3) << 2
        if ap:
            req |= SWD_REQ_AP
        if read:
            req |= SWD_REQ_READ
        return req

    def dp_read(self, addr: int) -> int:
        # DP 读在同一次 transfer 内直接返回数据（真机实测：IDCODE 读请求
        # 的数据字即为 IDCODE；RDBUFF 读恒返回 0，仅作 DP 读参考）。
        ack, count, vals = self.probe.transfer(
            self.dap_index, [(self._swd_req(False, True, addr), None)])
        if ack != SWD_ACK_OK or count < 1 or not vals:
            raise DapError(f"DP 读 {addr:#x} 失败（ACK={ack:#x}）")
        return vals[0]

    def dp_write(self, addr: int, value: int):
        ack, count, _ = self.probe.transfer(
            self.dap_index, [(self._swd_req(False, False, addr), value)])
        if ack != SWD_ACK_OK or count < 1:
            raise DapError(f"DP 写 {addr:#x} ACK={ack:#x}")

    def ap_read(self, addr: int, ap_sel: int = 0) -> int:
        self._set_ap(ap_sel)
        # 真机实测（DAPLink v0257 WinUSB）：AP 读在同一次 transfer 内
        # 直接返回数据（固件内部已自动补 RDBUFF 收尾）；RDBUFF 读恒
        # 返回 0，旧的两阶段实现（发起+RDBUFF）导致 read_mem32 恒为 0。
        ack, count, vals = self.probe.transfer(
            self.dap_index, [(self._swd_req(True, True, addr), None)])
        if ack != SWD_ACK_OK or count < 1 or not vals:
            raise DapError(f"AP 读 {addr:#x} 失败（ACK={ack:#x}）")
        return vals[0]

    def ap_write(self, addr: int, value: int, ap_sel: int = 0):
        self._set_ap(ap_sel)
        ack, count, _ = self.probe.transfer(
            self.dap_index, [(self._swd_req(True, False, addr), value)])
        if ack != SWD_ACK_OK or count < 1:
            raise DapError(f"AP 写 {addr:#x} ACK={ack:#x}")

    def _set_ap(self, ap_sel: int):
        want = (ap_sel << 24)
        if self._select_cache != want:
            self.dp_write(_DP_SELECT, want)
            self._select_cache = want

    def power_up(self, timeout_ms: int = 2000) -> None:
        """请求调试/系统电源上电并等待 ACK（ARM ADIv5 连接必需步骤）。

        冷连接/拔插后 AP 电源域未上电（CTRL/STAT 的 CDBGPWRUPACK、
        CSYSPWRUPACK 均为 0），此时所有 AP 访问（含内存读写）立即
        FAULT 并置位 STICKYERR，导致 RTT 扫描静默失败。必须先写
        CTRL/STAT = CDBGPWRUPREQ|CSYSPWRUPREQ 请求上电，再轮询
        等待 ACK 置位（真机实测 DAPLink v0257 一次请求即上电）。
        """
        self.dp_write(_DP_CTRL_STAT, _POWERUP_REQUEST)
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            stat = self.dp_read(_DP_CTRL_STAT)
            if (stat & _POWERUP_ACK) == _POWERUP_ACK:
                return
            if time.monotonic() > deadline:
                raise DapError("调试电源上电超时（检查 SWD 接线/芯片供电）")

    # ── 内存访问 ───────────────────────────────────────────────

    def _setup_mem(self, ap_sel: int = 0):
        # 特权 CSW（HPROT）：PPB 调试寄存器（DHCSR/AIRCR）非特权访问
        # 会被拒；真机实测 SPROT(bit8) 无效，须用 HPROT(0x23000052)
        self.ap_write(_AP_CSW, _CSW_PRIV, ap_sel)

    def read_mem32(self, addr: int, ap_sel: int = 0) -> int:
        self._setup_mem(ap_sel)
        self.ap_write(_AP_TAR, addr & 0xFFFFFFFC, ap_sel)
        return self.ap_read(_AP_DRW, ap_sel)

    def write_mem32(self, addr: int, value: int, ap_sel: int = 0):
        self._setup_mem(ap_sel)
        self.ap_write(_AP_TAR, addr & 0xFFFFFFFC, ap_sel)
        self.ap_write(_AP_DRW, int(value) & 0xFFFFFFFF, ap_sel)

    def read_mem_block(self, addr: int, count: int, ap_sel: int = 0) -> bytes:
        """按 32 位字读内存，支持任意字节地址与长度（跨 1KB 边界分段）。

        非 4 对齐地址：先按字读覆盖 [addr, addr+count) 的最小字区间，
        再按字节切片返回。RTT 环形缓冲的读写指针是任意字节偏移
        （如 15 字节消息 → rd=0,15,30…），旧实现从对齐地址整块读后
        只截尾部、头部 (addr&3) 字节错位，真机表现为
        "SEGGER_RTT_TESSEGGER_RTT_TEST" 式错位拼接。
        真机实测（DAPLink v0257 WinUSB）：AP 读为立即返回语义——连发
        n 个 AP DRW 读请求，响应数据即 n 个目标字（固件内部自动补
        RDBUFF 收尾），无需追加 RDBUFF 读、无需跳过残留值。
        固件响应缓冲仅 64 字节，单次 DAP_Transfer 读请求数必须 ≤15
        （3+4*15=63B），超限（16 字=67B）固件溢出挂死；超长数据
        按 15 字分段，避免旧实现（255 字/次）的真机崩溃。
        """
        if count <= 0:
            return b""
        self.ap_write(_AP_CSW, _CSW_PRIV, ap_sel)
        out = bytearray()
        pos = addr & 0xFFFFFFFC                    # 起始字对齐（向下）
        end = addr + count
        remain = ((end + 3) & 0xFFFFFFFC) - pos    # 覆盖到末尾的完整字数
        while remain > 0:
            chunk = min(remain, 0x400 - (pos & 0x3FF))
            chunk -= chunk & 3
            if chunk <= 0:
                chunk = 4
            chunk = min(chunk, _DAP_MAX_READ_WORDS * 4)   # 固件响应缓冲上限
            n = chunk // 4
            self.ap_write(_AP_TAR, pos, ap_sel)
            reqs = [(self._swd_req(True, True, _AP_DRW), None)
                    for _ in range(n)]
            _, got, vals = self.probe.transfer(self.dap_index, reqs)
            if got < len(reqs):
                raise DapError(f"内存读 {pos:#x} 中断（got={got}）")
            for v in vals:
                out += v.to_bytes(4, "little")
            pos += chunk
            remain -= chunk
        self.ap_write(_AP_CSW, _CSW_PRIV, ap_sel)
        head = addr - (addr & 0xFFFFFFFC)
        return bytes(out[head:head + count])

    def write_mem_block(self, addr: int, data: bytes, ap_sel: int = 0):
        """按 32 位字写内存；任意字节地址/长度（跨 1KB 边界分段）。

        RTT DOWN 通道的写指针 wr 为任意字节偏移：直接整字写会破坏
        addr 前后相邻字节，首/尾不完整字必须先读回所在字、合并目标
        字节再写回（读-改-写）。
        """
        if not data:
            return
        payload = bytes(data)
        self.ap_write(_AP_CSW, _CSW_PRIV, ap_sel)
        pos = addr & 0xFFFFFFFC
        off = addr - pos                             # 首字内偏移 0..3
        idx = 0
        n = len(payload)
        # 首字（非对齐）：读-改-写，保护 addr 之前的字节
        if off:
            take = min(4 - off, n)
            hb = bytearray(self.read_mem32(pos, ap_sel).to_bytes(4, "little"))
            hb[off:off + take] = payload[:take]
            self.ap_write(_AP_CSW, _CSW_PRIV, ap_sel)
            self.ap_write(_AP_TAR, pos, ap_sel)
            self.ap_write(_AP_DRW, int.from_bytes(hb, "little"), ap_sel)
            pos += 4
            idx += take
        # 中间完整字块（4 的倍数）
        while n - idx >= 4:
            chunk = min(n - idx, 0x400 - (pos & 0x3FF))
            chunk -= chunk & 3
            if chunk <= 0:
                chunk = 4
            chunk = min(chunk, _DAP_MAX_WRITE_WORDS * 4)   # 固件请求缓冲上限
            self.ap_write(_AP_TAR, pos, ap_sel)
            reqs = [(self._swd_req(True, False, _AP_DRW),
                     int.from_bytes(payload[idx + i:idx + i + 4], "little"))
                    for i in range(0, chunk, 4)]
            resp, got, _ = self.probe.transfer(self.dap_index, reqs)
            if got < len(reqs) or resp != SWD_ACK_OK:
                raise DapError(f"内存写 {pos:#x} 失败（ACK={resp:#x}）")
            pos += chunk
            idx += chunk
        # 尾字（剩余 1-3 字节）：读-改-写，保护之后的字节
        if n - idx:
            hb = bytearray(self.read_mem32(pos, ap_sel).to_bytes(4, "little"))
            hb[:n - idx] = payload[idx:]
            self.ap_write(_AP_CSW, _CSW_PRIV, ap_sel)
            self.ap_write(_AP_TAR, pos, ap_sel)
            self.ap_write(_AP_DRW, int.from_bytes(hb, "little"), ap_sel)

    def read_idcode(self) -> int:
        """完整 SWD 连接初始化，返回 IDCODE。

        真机实测（DAPLink v0257 WinUSB）必需步骤：
        1) swd_activate：SWD 线激活（线复位 + JTAG→SWD 切换）；
        2) 读 IDCODE 确认连接；
        3) 写 ABORT=0x1E 清除历史 STICKYERR——不清则后续 AP 访问
           全部 FAULT（旧注释"激活后写 ABORT 导致 NO_ACK"已由真机
           复测推翻：先读 IDCODE 再写 ABORT 一切正常）；
        4) power_up：请求调试电源上电并等待 ACK——冷连接/拔插后
           CDBGPWRUPACK=0，不请求上电则 AP 内存访问全 FAULT；
        5) 上电过程可能再次置位 STICKYERR，再清一次。
        """
        self.probe.swd_activate()
        idcode = self.dp_read(0x00)
        self.dp_write(0x00, _ABORT_CLEAR_STICKY)   # 清历史 sticky
        self.power_up()                            # 调试电源上电
        self.dp_write(0x00, _ABORT_CLEAR_STICKY)   # 上电后再清一次
        return idcode
